"""Federated Learning -- local training data ("Training Data" tab).

This instance's own private folder of training cases (images/ + labels/,
matched by filename -- see airflow/dags/dag_fl_segresnet_local.py's
LOCAL_TRAINING_DATA_HOST_DIR). Never uploaded to
any other instance -- the trainer container mounts this folder directly,
and only the resulting (unencrypted) model weights and round-0 per-case
data statistics ever leave this host.

Upload mechanism (folder-drop) is adapted from batch.py's
_stage_upload_to_dir / _sanitize_relpath -- same file[]/relpath[] shape,
same path-sanitizing (a dropped folder's paths are untrusted input).
Deliberately NOT reusing batch.py's cleanup behavior, though: batch
uploads are scratch for one job and get rmtree'd afterward; this data is
meant to persist and be reused by every future round, so nothing here
ever deletes anything except an explicit DELETE from the admin.

Batches: every POST to /upload is one batch -- every case it saves is
tagged with who uploaded it and when (JSON store, same
load/save-with-lock/atomic-replace pattern as federated_store.py's
instance registry, just scoped to this one file rather than a shared
utils module since nothing else needs it). A case's batch_id is
last-writer-wins: re-uploading a case moves it into whichever batch did
that, so a case is never claimed by two batches' listings at once. Cases
that predate this feature (no batch_id on file) are grouped under a
synthetic "unassigned" bucket in the UI rather than silently dropped.

Enabled/disabled: a batch can be turned off (set_batch_enabled) without
deleting it -- its cases stay on disk and stay listed, they're just left
out of the next round. See _write_enabled_cases_file, which keeps
enabled_cases.json (in this same LOCAL_TRAINING_DATA_DIR) up to date
whenever a batch's state, or the case list itself, changes --
train_fl_round_segresnet.py's _build_datalist reads that file to decide
what to train on.
"""
import json
import os
import threading
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, render_template, request

from app.oidc_auth import oidc
from app.utils.roles import admin_required
from app.utils.storage_paths import API_STORAGE_DIR

federated_training_data_bp = Blueprint(
    "federated_training_data", __name__, url_prefix="/federated/training-data"
)


@federated_training_data_bp.errorhandler(Exception)
def _json_error(exc):
    return jsonify({"error": str(exc) or exc.__class__.__name__}), 500

# Mounted into this container by docker-context-gui/docker-compose.yml --
# see that file's comment on this line. MUST resolve to the same host
# directory the Airflow DockerOperator mounts as LOCAL_TRAINING_DATA_HOST_DIR
# (both containers reading/writing the same tree, same principle as
# storage_paths.py's API_STORAGE_DIR/WP4_DATASET_DIR).
LOCAL_TRAINING_DATA_DIR = os.environ.get("FL_LOCAL_TRAINING_DATA_DIR", "/app/fl_local_training_data")

ALLOWED_EXTENSIONS = (".nii.gz", ".nii")

BATCHES_PATH = os.environ.get(
    "FL_TRAINING_DATA_BATCHES_PATH",
    f"{API_STORAGE_DIR}/federated/training_data_batches.json",
)
_batches_lock = threading.Lock()


def _load_batches():
    if not os.path.isfile(BATCHES_PATH):
        return {"batches": {}, "case_owner": {}}
    with open(BATCHES_PATH) as f:
        return json.load(f)


def _save_batches(data):
    os.makedirs(os.path.dirname(BATCHES_PATH), exist_ok=True)
    tmp_path = BATCHES_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, BATCHES_PATH)  # atomic, matches federated_store.py's write pattern


def _current_username():
    return oidc.user_getfield("preferred_username") or oidc.user_getfield("email") or "unknown"


def _sanitize_relpath(relpath):
    """Same as batch.py's -- a dropped folder's per-file path is untrusted
    input, so strip any "." / ".." segment rather than let a crafted drop
    write outside the training-data directory."""
    return [p for p in relpath.replace("\\", "/").split("/") if p not in ("", ".", "..")]


def _classify(parts):
    """Where in images/ or labels/ (wherever that segment appears in the
    dropped path -- works whether the user dragged in `images/`+`labels/`
    directly, or a wrapper folder containing both) this file belongs.
    Returns (bucket, filename) or (None, None) if neither segment is
    present -- e.g. a stray README dropped alongside the real folders."""
    for i, part in enumerate(parts):
        if part in ("images", "labels") and i + 1 < len(parts):
            return part, parts[-1]
    return None, None


def _list_cases():
    images_dir = os.path.join(LOCAL_TRAINING_DATA_DIR, "images")
    labels_dir = os.path.join(LOCAL_TRAINING_DATA_DIR, "labels")
    image_files = set(os.listdir(images_dir)) if os.path.isdir(images_dir) else set()
    label_files = set(os.listdir(labels_dir)) if os.path.isdir(labels_dir) else set()

    case_owner = _load_batches().get("case_owner", {})

    cases = []
    for name in sorted(image_files | label_files):
        cases.append({
            "case_id": name,
            "has_image": name in image_files,
            "has_label": name in label_files,
            "batch_id": case_owner.get(name),
        })
    return cases


def _list_batches():
    """Cases grouped by which upload created them, newest batch first.
    Anything with no batch_id on file (predates this feature, or a batch
    record somehow got lost) is grouped under one synthetic
    batch_id=None "unassigned" entry rather than dropped from the page.
    "unassigned" has no on/off switch -- see _enabled_case_ids -- so it
    always reports enabled=True."""
    cases = _list_cases()
    batch_meta = _load_batches().get("batches", {})

    grouped = {}
    for case in cases:
        grouped.setdefault(case["batch_id"], []).append(case)

    result = []
    for bid, meta in sorted(batch_meta.items(), key=lambda kv: kv[1].get("uploaded_at", ""), reverse=True):
        if bid in grouped:
            result.append({
                "batch_id": bid,
                "label": meta.get("label", "(unnamed)"),
                "uploaded_by": meta.get("uploaded_by", "unknown"),
                "uploaded_at": meta.get("uploaded_at", ""),
                "enabled": meta.get("enabled", True),
                "cases": grouped.pop(bid),
            })

    unassigned = grouped.pop(None, [])
    if unassigned:
        result.append({
            "batch_id": None,
            "uploaded_by": None,
            "uploaded_at": None,
            "enabled": True,
            "cases": unassigned,
        })
    return result


ENABLED_CASES_PATH = os.path.join(LOCAL_TRAINING_DATA_DIR, "enabled_cases.json")


def _write_enabled_cases_file():
    """Tells train_fl_round_segresnet.py's _build_datalist which case IDs
    to actually train on -- written into the SAME host folder the
    trainer container already mounts as LOCAL_DATA_DIR (no new mount, no
    new auth needed: this GUI already has read/write access to that
    folder for upload()/delete_case() above).

    A batch with no "enabled" key (predates this feature) defaults to
    enabled, so nothing already-training changes on upgrade. The
    "unassigned" bucket (batch_id=None) has no record to disable, so its
    cases are always included. If this file is ever missing entirely,
    _build_datalist falls back to training on everything -- same as
    before this feature existed."""
    data = _load_batches()
    batch_meta = data.get("batches", {})
    case_owner = data.get("case_owner", {})

    enabled_cases = [
        case_id for case_id, batch_id in case_owner.items()
        if batch_meta.get(batch_id, {}).get("enabled", True)
    ]
    # Cases with no case_owner entry at all (the "unassigned" bucket) --
    # every case on disk that case_owner doesn't already cover.
    images_dir = os.path.join(LOCAL_TRAINING_DATA_DIR, "images")
    labels_dir = os.path.join(LOCAL_TRAINING_DATA_DIR, "labels")
    on_disk = (set(os.listdir(images_dir)) if os.path.isdir(images_dir) else set()) | \
              (set(os.listdir(labels_dir)) if os.path.isdir(labels_dir) else set())
    enabled_cases += [c for c in on_disk if c not in case_owner]

    tmp_path = ENABLED_CASES_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump({"enabled_cases": sorted(set(enabled_cases))}, f, indent=2)
    os.replace(tmp_path, ENABLED_CASES_PATH)


@federated_training_data_bp.route("/")
@admin_required
def index():
    batches = _list_batches()
    return render_template(
        "federated_training_data.html",
        title="Federated Learning -- Training Data",
        batches=batches,
        case_count=sum(len(b["cases"]) for b in batches),
    )


@federated_training_data_bp.route("/", methods=["POST"])
@admin_required
def upload():
    batch_label = (request.form.get("batch_label") or "").strip()
    if not batch_label:
        return jsonify({"error": "batch_label is required"}), 400

    uploaded_files = request.files.getlist("file")
    relpaths = request.form.getlist("relpath")
    if len(relpaths) != len(uploaded_files):
        return jsonify({"error": "file/relpath count mismatch"}), 400

    saved, skipped = [], []
    saved_case_ids = set()
    for uploaded, relpath in zip(uploaded_files, relpaths):
        if not uploaded or not uploaded.filename:
            continue
        parts = _sanitize_relpath(relpath)
        if not parts:
            continue

        bucket, filename = _classify(parts)
        if bucket is None:
            skipped.append({"path": relpath, "reason": "not under an images/ or labels/ folder"})
            continue
        if not filename.endswith(ALLOWED_EXTENSIONS):
            skipped.append({"path": relpath, "reason": "not a .nii/.nii.gz file"})
            continue

        dest_dir = os.path.join(LOCAL_TRAINING_DATA_DIR, bucket)
        os.makedirs(dest_dir, exist_ok=True)
        uploaded.save(os.path.join(dest_dir, filename))
        saved.append(f"{bucket}/{filename}")
        saved_case_ids.add(filename)

    if saved_case_ids:
        with _batches_lock:
            data = _load_batches()
            batch_id = uuid.uuid4().hex
            data.setdefault("batches", {})[batch_id] = {
                "label": batch_label,
                "uploaded_by": _current_username(),
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
            case_owner = data.setdefault("case_owner", {})
            for case_id in saved_case_ids:
                case_owner[case_id] = batch_id
            _save_batches(data)
        _write_enabled_cases_file()

    return jsonify({"saved": saved, "skipped": skipped, "batches": _list_batches()})


@federated_training_data_bp.route("/<case_id>", methods=["DELETE"])
@admin_required
def delete_case(case_id):
    # Plain string converter (no slashes accepted at all), not <path:...>
    # -- case ids are always bare filenames, and this also guarantees no
    # routing collision with /batch/<batch_id> below.
    case_id = os.path.basename(case_id)  # defense in depth, even though admin-only
    removed = []
    for bucket in ("images", "labels"):
        path = os.path.join(LOCAL_TRAINING_DATA_DIR, bucket, case_id)
        if os.path.isfile(path):
            os.remove(path)
            removed.append(bucket)
    if not removed:
        return jsonify({"error": f"'{case_id}' not found in images/ or labels/"}), 404

    with _batches_lock:
        data = _load_batches()
        data.get("case_owner", {}).pop(case_id, None)
        _save_batches(data)
    _write_enabled_cases_file()

    return jsonify({"removed_from": removed})


@federated_training_data_bp.route("/batch/<batch_id>", methods=["DELETE"])
@admin_required
def delete_batch(batch_id):
    with _batches_lock:
        data = _load_batches()
        case_owner = data.get("case_owner", {})
        case_ids = [cid for cid, bid in case_owner.items() if bid == batch_id]

        removed = []
        for case_id in case_ids:
            for bucket in ("images", "labels"):
                path = os.path.join(LOCAL_TRAINING_DATA_DIR, bucket, case_id)
                if os.path.isfile(path):
                    os.remove(path)
                    removed.append(f"{bucket}/{case_id}")
            case_owner.pop(case_id, None)

        had_batch_record = data.get("batches", {}).pop(batch_id, None) is not None
        _save_batches(data)
    _write_enabled_cases_file()

    if not case_ids and not had_batch_record:
        return jsonify({"error": f"batch '{batch_id}' not found or already empty"}), 404
    return jsonify({"removed": removed})


@federated_training_data_bp.route("/batch/<batch_id>/enabled", methods=["POST"])
@admin_required
def set_batch_enabled(batch_id):
    """Turns a batch on/off for training WITHOUT deleting it -- unlike
    delete_batch, the files stay on disk and the batch stays listed, it's
    just left out of the next round's datalist (see
    _write_enabled_cases_file / train_fl_round_segresnet.py's
    _build_datalist). Body: {"enabled": bool}."""
    enabled = bool((request.get_json(force=True) or {}).get("enabled", True))
    with _batches_lock:
        data = _load_batches()
        if batch_id not in data.get("batches", {}):
            return jsonify({"error": f"batch '{batch_id}' not found"}), 404
        data["batches"][batch_id]["enabled"] = enabled
        _save_batches(data)
    _write_enabled_cases_file()
    return jsonify({"batch_id": batch_id, "enabled": enabled})
