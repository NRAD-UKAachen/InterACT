"""Federated Learning -- starting and running training jobs ("Training
Rounds" tab), plus every machine-to-machine endpoint that's specific to a
run (as opposed to federated_instances.py's registration-time endpoints).

Endpoint <-> DAG contract (see airflow/dags/dag_fl_segresnet_coordinator.py
and dag_fl_segresnet_local.py -- these routes' URLs are exactly what those
DAGs' `conf` params point at):
  POST /federated/rounds/                       admin: start a job
  GET  /federated/rounds/<run_id>/round_<n>/global_model
                                                  instance-token: fetch this round's starting weights
  POST /federated/rounds/<run_id>/round_<n>/upload/<site_name>
                                                  instance-token: a site's round result
  GET  /federated/rounds/<run_id>/status.json    admin: poll
  POST /federated/rounds/<run_id>/cancel         admin: request a running job stop
  POST /federated/rounds/<run_id>/cancel-remote  instance-token: coordinator telling a remote site to cancel its own part
  POST /federated/rounds/<run_id>/delete         admin: delete one finished run's history
  POST /federated/rounds/clear-all               admin: delete every finished (non-active) run's history
  POST /federated/rounds/<run_id>/resend-final-model  admin: retry pushing the finished model to sites that didn't get it
  GET  /federated/rounds/<run_id>/deliverable/download  admin: download the finished model + recipe + inference script + requirements.txt as one zip
"""
import io
import json
import os
import shutil
import zipfile
from datetime import datetime

import requests
from flask import Blueprint, Response, g, jsonify, render_template, request

from app.utils.airflow_client import AirflowClient
from app.utils.federated_auth import require_instance_token
from app.utils.federated_models import FL_MODELS
from app.utils.federated_store import (
    get_instance,
    get_local_instance,
    get_self_call_token,
    list_instances,
)
from app.utils.roles import admin_required

federated_rounds_bp = Blueprint("federated_rounds", __name__, url_prefix="/federated/rounds")

WORKFLOWS_DIR = os.environ.get("AIRFLOW_WORKFLOWS_DIR", "/data/airflow/workflows")


def _model_run_dir(model, run_id):
    """Used when the model is already known (starting a new job -- the
    caller picked it explicitly)."""
    return os.path.join(WORKFLOWS_DIR, FL_MODELS[model]["dir_name"], run_id)


def _find_run_dir(run_id):
    """Used everywhere else: these routes are called by run_id alone (see
    the DAG<->endpoint contract in this file's docstring -- none of those
    URLs carry a model segment), so locate which model's workflow
    directory this run actually lives under. Cheap: a handful of
    registered models, not thousands."""
    for cfg in FL_MODELS.values():
        candidate = os.path.join(WORKFLOWS_DIR, cfg["dir_name"], run_id)
        if os.path.isdir(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------
# Admin pages
# ---------------------------------------------------------------------
@federated_rounds_bp.route("/how-to")
@admin_required
def how_to():
    return render_template(
        "federated_requirements.html",
        title="How To",
    )


@federated_rounds_bp.route("/")
@admin_required
def index():
    jobs = []
    for model_key, cfg in FL_MODELS.items():
        base = os.path.join(WORKFLOWS_DIR, cfg["dir_name"])
        if not os.path.isdir(base):
            continue
        for run_id in os.listdir(base):
            meta_path = os.path.join(base, run_id, "metadata.json")
            is_coordinator = os.path.isfile(meta_path)
            if not is_coordinator:
                # No coordinator record here -- this host may still be a
                # plain PARTICIPANT in this run (see
                # dag_fl_segresnet_local.py's participant_metadata.json),
                # which is what lets a pure remote site's own Training
                # Rounds page show something instead of nothing.
                meta_path = os.path.join(base, run_id, "participant_metadata.json")
            if os.path.isfile(meta_path):
                with open(meta_path) as f:
                    job = json.load(f)
                job.setdefault("model", model_key)
                # For the template/JS to tell apart "this is the run I'm
                # coordinating" (Cancel actually stops the whole
                # federation) from "I'm just a participant" (Cancel only
                # stops my own part -- see cancel_round/dag_fl_segresnet_
                # local.py's checkpoints).
                job["is_coordinator"] = is_coordinator
                jobs.append(job)
    # started_at only gets written once the coordinator's DAG actually
    # begins running (dag_fl_segresnet_coordinator.py's
    # _run_federated_training) -- a run can sit at status "submitted"
    # for a while before that, with no started_at yet, sorting it to the
    # bottom instead of the top until Airflow catches up. submitted_at
    # is written immediately, at creation, for every run (coordinator or
    # participant), so falling back to it keeps a brand-new run pinned
    # at the top from the moment it's clicked.
    jobs.sort(key=lambda j: j.get("started_at") or j.get("submitted_at", ""), reverse=True)
    return render_template(
        "federated_rounds.html",
        title="Federated Learning -- Training Rounds",
        jobs=jobs,
        instances=[i for i in list_instances() if i["role"] == "remote"],
        fl_models=FL_MODELS,
    )


@federated_rounds_bp.route("/", methods=["POST"])
@admin_required
def start_round():
    body = request.get_json(force=True)

    run_id = body.get("run_id") or f"fl_{datetime.utcnow():%Y%m%d_%H%M%S}"
    model = body.get("model", "segresnet_auto3dseg")
    if model not in FL_MODELS:
        return jsonify({"error": f"unknown model '{model}' (known: {list(FL_MODELS)})"}), 400
    model_cfg = FL_MODELS[model]

    # Checked here, before anything is written or triggered: a run with 0
    # rounds or no sites used to start anyway and then fail partway through
    # (after the whole fingerprint round, for 0 rounds) with an error that
    # said nothing about the actual cause.
    remote_instance_ids = body.get("remote_instance_ids") or []
    train_local_too = bool(body.get("train_local_too"))
    try:
        num_rounds = int(body.get("num_rounds", 10))
        local_epochs = int(body.get("local_epochs", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "number of rounds and local epochs must be whole numbers"}), 400
    if num_rounds < 1:
        return jsonify({"error": f"number of rounds must be at least 1, got {num_rounds}"}), 400
    if local_epochs < 1:
        return jsonify({"error": f"local epochs per round must be at least 1, got {local_epochs}"}), 400
    if not remote_instance_ids and not train_local_too:
        return jsonify({"error": "select at least one remote instance, or train on this site's own data"}), 400
    modality = body.get("modality", "CT")
    if modality not in ("CT", "MRI"):
        return jsonify({"error": f"modality must be CT or MRI, got '{modality}'"}), 400

    remotes = []
    for iid in remote_instance_ids:
        inst = get_instance(iid)
        if not inst or inst["role"] != "remote":
            return jsonify({"error": f"unknown remote instance id {iid}"}), 400
        if model_cfg["local_dag_id"] not in inst.get("allowed_dags", []):
            return jsonify({
                "error": f"{inst['name']} has not opted '{model_cfg['local_dag_id']}' into "
                         f"its allowed_dags -- ask that site's admin to enable it first"
            }), 400
        remotes.append(inst)

    local_inst = get_local_instance()

    run_dir = _model_run_dir(model, run_id)
    os.makedirs(run_dir, exist_ok=True)

    # No callback_token to build here anymore: each remote's own Flask
    # (incoming_job) now injects the right one itself, from what IT
    # already has on file for calling us back -- see that function's
    # docstring. We just need outgoing_token to make the initial call.
    remote_instances_conf = [
        {
            "name": inst["name"],
            "incoming_job_url": f"{inst['base_url']}/federated/instances/incoming/job",
            "incoming_final_model_url": f"{inst['base_url']}/federated/rounds/{run_id}/final-model",
            "incoming_cancel_url": f"{inst['base_url']}/federated/rounds/{run_id}/cancel-remote",
            "outgoing_token": inst["outgoing_token"],
        }
        for inst in remotes
    ]
    if train_local_too:
        # This site trains too, without registering itself as its own
        # remote instance (no token pairing needed -- see
        # dag_fl_segresnet_coordinator.py's _distribute_round, which
        # triggers fl_segresnet_local directly instead of over HTTP for
        # an entry marked "local"). get_self_call_token() authenticates
        # its own callback to this same Flask app.
        remote_instances_conf.append({
            "name": local_inst["name"],
            "local": True,
            "outgoing_token": get_self_call_token(),
        })

    metadata = {
        "run_id": run_id, "model": model, "status": "submitted", "num_rounds": num_rounds,
        "current_round": 0,
        "sites": [r["name"] for r in remotes] + ([local_inst["name"]] if train_local_too else []),
        "submitted_at": datetime.utcnow().isoformat(),
    }
    with open(os.path.join(run_dir, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2)

    conf = {
        "run_id": run_id, "num_rounds": num_rounds,
        "local_epochs": local_epochs, "modality": modality,
        "remote_instances": remote_instances_conf,
    }

    client = AirflowClient()
    client.trigger_dag(model_cfg["coordinator_dag_id"], conf=conf, dag_run_id=run_id)
    return jsonify(metadata), 202


@federated_rounds_bp.route("/<run_id>/status.json")
@admin_required
def status_json(run_id):
    """Polled every few seconds by the Training Rounds page's own
    JS (pollOnce) to live-update a row's status without a full reload --
    same metadata.json-or-participant_metadata.json fallback as index(),
    or a pure participant site's page would 404 on every single poll and
    just sit on whatever status it had at page load, forever, even once
    the real status (e.g. cancelled) has already landed on disk."""
    run_dir = _find_run_dir(run_id)
    if run_dir is None:
        return jsonify({"error": "unknown run_id"}), 404
    meta_path = os.path.join(run_dir, "metadata.json")
    if not os.path.isfile(meta_path):
        meta_path = os.path.join(run_dir, "participant_metadata.json")
    if not os.path.isfile(meta_path):
        return jsonify({"error": "unknown run_id"}), 404
    with open(meta_path) as f:
        return jsonify(json.load(f))


def _write_cancel_marker(run_id):
    """Shared by cancel_round (a human clicking Cancel on THIS site's own
    page) and cancel_round_remote (the coordinator telling a REMOTE site
    to cancel its own part -- see dag_fl_segresnet_coordinator.py's
    _run_federated_training) -- same marker, same effect, either way:
    the coordinator's own _wait_for_uploads loop, or a participant's own
    dag_fl_segresnet_local.py checkpoints, pick it up within one poll
    cycle / at the next checkpoint. Returns (ok, error_response_or_None).
    """
    run_dir = _find_run_dir(run_id)
    if run_dir is None:
        return False, (jsonify({"error": "unknown run_id"}), 404)
    meta_path = os.path.join(run_dir, "metadata.json")
    if not os.path.isfile(meta_path):
        meta_path = os.path.join(run_dir, "participant_metadata.json")
    if os.path.isfile(meta_path):
        with open(meta_path) as f:
            status = json.load(f).get("status")
        if status not in ("submitted", "running"):
            return False, (jsonify({"error": f"run is already {status}, nothing to cancel"}), 400)
    with open(os.path.join(run_dir, "_cancel_requested"), "w") as f:
        f.write(datetime.utcnow().isoformat())
    return True, None


@federated_rounds_bp.route("/<run_id>/cancel", methods=["POST"])
@admin_required
def cancel_round(run_id):
    """Cooperative cancellation: writes a marker that the coordinator
    DAG's own _wait_for_uploads loop checks every poll cycle (~30s) and
    reacts to by exiting through its normal failure-handling path, so
    metadata.json correctly ends up "cancelled" instead of stuck at
    "running" forever (which is what happens if you just kill the
    Airflow task from outside instead).

    Doesn't force-stop a site's already-in-flight local training
    container -- if that upload eventually arrives, it just lands in a
    run directory nothing reads from anymore.

    This only ever cancels what's running HERE. If you're the
    coordinator, that's the whole federation (see _run_federated_training,
    which also calls cancel_round_remote on every other participating
    site once it sees this marker). If you're just a participant, it's
    only your own part -- see federated_rounds.html's confirm dialog,
    which already explains that difference depending on which page
    you're on."""
    ok, error_response = _write_cancel_marker(run_id)
    if not ok:
        return error_response
    return jsonify({"status": "cancel requested"}), 202


@federated_rounds_bp.route("/<run_id>/cancel-remote", methods=["POST"])
@require_instance_token
def cancel_round_remote(run_id):
    """Instance-token counterpart to cancel_round, above -- lets the
    COORDINATOR tell a REMOTE participant to cancel, which nothing could
    do before this existed (cancel_round is admin-only, i.e. only
    callable by a logged-in human on that same site, never by another
    instance). Without this, cancelling a run only ever stopped the
    coordinator's own side; every remote site kept training, uploading,
    and showing "running" forever with no idea the run was cancelled."""
    ok, error_response = _write_cancel_marker(run_id)
    if not ok:
        return error_response
    return jsonify({"status": "cancel requested"}), 202


@federated_rounds_bp.route("/<run_id>/delete", methods=["POST"])
@admin_required
def delete_round(run_id):
    """Deletes one FINISHED run's workflow directory (success, failed, or
    cancelled). A submitted/running run is refused -- cancel it first."""
    run_dir = _find_run_dir(run_id)
    if run_dir is None:
        return jsonify({"error": "unknown run_id"}), 404
    meta_path = os.path.join(run_dir, "metadata.json")
    if not os.path.isfile(meta_path):
        meta_path = os.path.join(run_dir, "participant_metadata.json")
    if os.path.isfile(meta_path):
        with open(meta_path) as f:
            status = json.load(f).get("status")
        if status in ("submitted", "running"):
            return jsonify({"error": f"run is still {status}; cancel it first"}), 400
    shutil.rmtree(run_dir, ignore_errors=True)
    return jsonify({"removed": run_id}), 200


@federated_rounds_bp.route("/clear-all", methods=["POST"])
@admin_required
def clear_all():
    """Deletes every FINISHED run's workflow directory (success, failed,
    or cancelled) across every registered model -- both runs this
    instance coordinated (metadata.json) and runs it only participated
    in (participant_metadata.json, see dag_fl_segresnet_local.py). A
    folder with NEITHER file isn't a run at all -- it's a site's own
    internal per-round scratch dir (e.g. "<run_id>_r3_XE_Server") -- and
    is always left alone here regardless of anything inside it.

    Anything still submitted/running is skipped, never deleted -- pulling
    a directory out from under an active coordinator (or an active local
    training task) would crash it uncleanly rather than cancel it (use
    cancel_round for that)."""
    removed, skipped_active = [], []
    for cfg in FL_MODELS.values():
        base = os.path.join(WORKFLOWS_DIR, cfg["dir_name"])
        if not os.path.isdir(base):
            continue
        for run_id in os.listdir(base):
            run_path = os.path.join(base, run_id)
            meta_path = os.path.join(run_path, "metadata.json")
            if not os.path.isfile(meta_path):
                meta_path = os.path.join(run_path, "participant_metadata.json")
            if not os.path.isfile(meta_path):
                continue  # not a run dir at all (e.g. a site's own local-round scratch dir), leave it alone
            with open(meta_path) as f:
                status = json.load(f).get("status")
            if status in ("submitted", "running"):
                skipped_active.append(run_id)
                continue
            shutil.rmtree(run_path, ignore_errors=True)
            removed.append(run_id)
    return jsonify({"removed": removed, "skipped_active": skipped_active}), 200


# ---------------------------------------------------------------------
# Instance-to-instance (bearer token)
# ---------------------------------------------------------------------
@federated_rounds_bp.route("/<run_id>/recipe")
@require_instance_token
def recipe_file(run_id):
    """Serves the segresnet_auto3dseg model's shared training recipe
    (configs + Auto3DSeg's own training scripts, tarred) -- generated
    once by dag_fl_segresnet_coordinator.py's round 0 from every site's
    merged data fingerprint, and downloaded by every site before each
    training round."""
    run_dir = _find_run_dir(run_id)
    if run_dir is None:
        return jsonify({"error": "unknown run_id"}), 404
    path = os.path.join(run_dir, "recipe.tar.gz")
    if not os.path.isfile(path):
        return jsonify({"error": "recipe not generated yet for this run_id"}), 404
    with open(path, "rb") as f:
        return Response(f.read(), mimetype="application/gzip")


@federated_rounds_bp.route("/<run_id>/deliverable/download")
@admin_required
def download_deliverable(run_id):
    """Human-facing download of everything needed to use this run's
    finished model on its own -- the model, the recipe it was trained
    with, a standalone inference script, and that script's pinned
    requirements.txt -- bundled into one zip rather than four separate
    downloads, so the model is never handed out without the recipe that
    explains how to preprocess for it (the model's weights alone can't
    tell you that -- see the inference script's own docstring).

    Checks both shapes a finished run's model can be in on THIS
    instance: final_model.pt directly in the run dir (this instance was
    a PARTICIPANT and received it via receive_final_model), or
    metadata.json's final_model_path (this instance WAS the coordinator
    and produced it itself). The other three files are only ever
    written together, at round 0 (see train_fl_round_segresnet.py's
    run_merge) -- if the model exists, they do too.

    Built fresh on each request instead of stored on disk -- everything
    but the model itself is tiny, so there's no real cost to zipping on
    demand rather than caching a copy that could go stale."""
    run_dir = _find_run_dir(run_id)
    if run_dir is None:
        return jsonify({"error": "unknown run_id"}), 404

    model_path = os.path.join(run_dir, "final_model.pt")
    if not os.path.isfile(model_path):
        meta_path = os.path.join(run_dir, "metadata.json")
        if os.path.isfile(meta_path):
            with open(meta_path) as f:
                candidate = json.load(f).get("final_model_path")
            if candidate and os.path.isfile(candidate):
                model_path = candidate

    if not os.path.isfile(model_path):
        return jsonify({"error": "final model not available for this run -- not finished yet, or not received from the coordinator yet"}), 404

    members = [
        (model_path, "final_model.pt"),
        (os.path.join(run_dir, "recipe.tar.gz"), "recipe.tar.gz"),
        (os.path.join(run_dir, "inference_script.py"), "inference_script.py"),
        (os.path.join(run_dir, "requirements.txt"), "requirements.txt"),
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for src_path, arcname in members:
            if os.path.isfile(src_path):
                zf.write(src_path, arcname=arcname)
    buffer.seek(0)
    return Response(
        buffer.read(),
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{run_id}_deliverable.zip"'},
    )


@federated_rounds_bp.route("/<run_id>/resend-final-model", methods=["POST"])
@admin_required
def resend_final_model(run_id):
    """Retries the coordinator's end-of-run push of the finished model to
    every site that didn't get it (metadata.json's final_model_undelivered,
    written by dag_fl_segresnet_coordinator.py's _run_federated_training
    once its own retries are used up). Until a site receives it, that
    site's own Training Rounds page stays at "running" with no download
    link -- this is the way out once the site is reachable again."""
    run_dir = _find_run_dir(run_id)
    meta_path = os.path.join(run_dir, "metadata.json") if run_dir else None
    if not meta_path or not os.path.isfile(meta_path):
        return jsonify({"error": "unknown run_id, or this instance did not coordinate it"}), 404
    with open(meta_path) as f:
        metadata = json.load(f)

    undelivered = metadata.get("final_model_undelivered") or {}
    if not undelivered:
        return jsonify({"error": "every site already has the final model"}), 400
    model_path = metadata.get("final_model_path") or ""
    if not os.path.isfile(model_path):
        return jsonify({"error": "final model file is no longer on this instance"}), 404

    remotes = {i["name"]: i for i in list_instances() if i["role"] == "remote"}
    delivered, still_undelivered = [], {}
    for name in undelivered:
        inst = remotes.get(name)
        if inst is None:
            still_undelivered[name] = "no longer registered under Instances"
            continue
        try:
            with open(model_path, "rb") as f:
                resp = requests.post(
                    f"{inst['base_url']}/federated/rounds/{run_id}/final-model",
                    headers={"Authorization": f"Bearer {inst['outgoing_token']}"},
                    files={"final_model": ("final_model.pt", f)},
                    timeout=300,
                )
            resp.raise_for_status()
            delivered.append(name)
        except Exception as exc:
            still_undelivered[name] = str(exc)

    if still_undelivered:
        metadata["final_model_undelivered"] = still_undelivered
    else:
        metadata.pop("final_model_undelivered", None)
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)
    return jsonify({"delivered": delivered, "still_undelivered": still_undelivered}), 200


@federated_rounds_bp.route("/<run_id>/round_<int:round_n>/global_model")
@require_instance_token
def global_model(run_id, round_n):
    run_dir = _find_run_dir(run_id)
    if run_dir is None:
        return jsonify({"error": "no global model for this round yet"}), 404
    path = os.path.join(run_dir, f"round_{round_n}", "global_model.pt")
    if not os.path.isfile(path):
        return jsonify({"error": "no global model for this round yet"}), 404
    with open(path, "rb") as f:
        return Response(f.read(), mimetype="application/octet-stream")


@federated_rounds_bp.route("/<run_id>/round_<int:round_n>/upload/<site_name>", methods=["POST"])
@require_instance_token
def upload_round_result(run_id, round_n, site_name):
    if g.calling_instance["name"] != site_name:
        return jsonify({
            "error": f"token belongs to '{g.calling_instance['name']}', "
                     f"cannot upload as '{site_name}'"
        }), 403

    run_dir = _find_run_dir(run_id)
    if run_dir is None:
        return jsonify({"error": "unknown run_id"}), 404
    site_dir = os.path.join(run_dir, f"round_{round_n}", "uploads", site_name)
    upload_dir = os.path.join(site_dir, "uploaded")
    os.makedirs(upload_dir, exist_ok=True)

    saved = []
    for name, file_storage in request.files.items():
        file_storage.save(os.path.join(upload_dir, name))
        saved.append(name)

    with open(os.path.join(site_dir, "_done"), "w") as f:
        f.write(datetime.utcnow().isoformat())

    return jsonify({"received": saved}), 201


@federated_rounds_bp.route("/<run_id>/round_<int:round_n>/failed/<site_name>", methods=["POST"])
@require_instance_token
def report_round_failure(run_id, round_n, site_name):
    """A participant's local DAG reporting its own failure immediately
    (see dag_fl_*_local.py's _report_failure_to_coordinator, a DAG-level
    on_failure_callback), instead of the coordinator only finding out
    after its own multi-hour _wait_for_uploads timeout. Writes a _failed
    marker next to where _done would normally land -- _wait_for_uploads
    checks for both and raises immediately, with this real error, on
    seeing this one."""
    if g.calling_instance["name"] != site_name:
        return jsonify({
            "error": f"token belongs to '{g.calling_instance['name']}', "
                     f"cannot report failure as '{site_name}'"
        }), 403

    run_dir = _find_run_dir(run_id)
    if run_dir is None:
        return jsonify({"error": "unknown run_id"}), 404
    site_dir = os.path.join(run_dir, f"round_{round_n}", "uploads", site_name)
    os.makedirs(site_dir, exist_ok=True)

    body = request.get_json(force=True, silent=True) or {}
    failure_info = {
        "failed_task": body.get("failed_task", "unknown task"),
        "error": body.get("error", "unknown error"),
        "reported_at": datetime.utcnow().isoformat(),
    }
    with open(os.path.join(site_dir, "_failed"), "w") as f:
        json.dump(failure_info, f, indent=2)

    return jsonify({"received": True}), 201


@federated_rounds_bp.route("/<run_id>/final-model", methods=["POST"])
@require_instance_token
def receive_final_model(run_id):
    """The coordinator pushing the fully-aggregated model back to a
    participant once the whole federation job is done -- opposite
    direction from upload_round_result (participant to coordinator, once
    per round) and a one-time push rather than per-round. See
    dag_fl_*_coordinator.py's _distribute_final_model."""
    run_dir = _find_run_dir(run_id)
    if run_dir is None:
        return jsonify({"error": "unknown run_id"}), 404
    request.files["final_model"].save(os.path.join(run_dir, "final_model.pt"))

    # If this host was purely a PARTICIPANT for this run (no coordinator
    # record here -- see index()'s same fallback), mark its own record
    # finished too, so its Training Rounds page shows "success" and a
    # download link instead of sitting at "running" forever.
    coord_meta_path = os.path.join(run_dir, "metadata.json")
    participant_meta_path = os.path.join(run_dir, "participant_metadata.json")
    if not os.path.isfile(coord_meta_path) and os.path.isfile(participant_meta_path):
        with open(participant_meta_path) as f:
            metadata = json.load(f)
        metadata["status"] = "success"
        metadata["completed_at"] = datetime.utcnow().isoformat()
        with open(participant_meta_path, "w") as f:
            json.dump(metadata, f, indent=2)

    return jsonify({"received": "final_model.pt"}), 201
