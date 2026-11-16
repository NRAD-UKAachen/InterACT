"""FL SegResNet (Auto3DSeg) -- local counterpart DAG.

Runs ONE federated round on this instance's own private data and reports
the result back to the coordinator, unencrypted (see
dag_fl_segresnet_coordinator.py's _aggregate_round for why). Two round
types, chosen by `conf.round_type` (set by that DAG's _distribute_round):

  fingerprint (round 0 only)  Analyze only this site's own local data,
                              upload the resulting per-case numbers.
                              No model, no training.
  train (every round after)   Download the shared recipe (generated
                              once, centrally, from every site's merged
                              fingerprint -- see the coordinator DAG),
                              train locally against it for LOCAL_EPOCHS,
                              upload the resulting weights.

Both round types use the SAME upload endpoint
(federated_rounds.upload_round_result) -- it just saves whatever file
is POSTed; fetching the recipe has its own endpoint
(federated_rounds.recipe_file).

Shared volume layout -- everything for one federation lives under ONE
folder per site, named after the run's TRUE id (conf.base_run_id, the
same every round -- NOT conf.run_id, which is a fresh, per-round,
per-site Airflow job id like "<run_id>_r2_XE_Server", used only for
Airflow's own dag_run bookkeeping, never for file paths):

  /data/airflow/workflows/fl-segresnet/<base_run_id>/
    participant_metadata.json   <- THIS SITE's own run-wide record (status, current_round -- see
                                    _update_participant_metadata). Named differently from
                                    metadata.json on purpose: a host that is both coordinator
                                    AND a participant (training its own data too) has BOTH
                                    files side by side here without colliding.
    local/round_<n>/            <- THIS SITE's own work for round n -- everything below is
                                    private to this site, never touched by coordinator code
      global_model.pt           <- downloaded from `conf.global_model_url` (train rounds, round>1)
      recipe/segresnet_0/       <- downloaded + extracted from `conf.recipe_url` (train rounds)
      local-output/
        fingerprint.yaml        <- fingerprint round's output
        local_update.pt         <- train round's output (fl-segresnet-trainer container)
      metadata.json             <- this round's own local status (not the run-wide one above)

    -- if this host is ALSO the coordinator for this run, dag_fl_segresnet_coordinator.py's
       OWN files (metadata.json, round_<n>/uploads/, round_<n>/global_model_next.pt, ...) live
       in this SAME folder, alongside but never inside local/ -- see that file's own docstring.
"""
import json
import os
import tarfile
import time
from datetime import datetime, timedelta

import requests as http_requests
from airflow import DAG
from airflow.exceptions import AirflowSkipException
from airflow.operators.python import PythonOperator
from airflow.providers.docker.operators.docker import DockerOperator
from docker.types import DeviceRequest, Mount


def _required_env(name):
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} must be set in the root .env")
    return value


WORKFLOWS_HOST_DIR = _required_env("AIRFLOW_WORKFLOWS_HOST_DIR")
WORKFLOWS_DIR = "/data/airflow/workflows"
LOCAL_TRAINING_DATA_HOST_DIR = _required_env("FL_LOCAL_TRAINING_DATA_HOST_DIR")
PROXY_ENV = {
    k: os.environ[k] for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY") if k in os.environ
}


def _report_failure_to_coordinator(context):
    # DAG-level on_failure_callback: tells the coordinator right away
    # (federated_rounds.report_round_failure) instead of leaving it to
    # find out only after its own multi-hour upload timeout.
    p = context.get("params") or {}
    failure_callback_url = p.get("failure_callback_url", "")
    if not failure_callback_url:
        return

    ti = context.get("task_instance")
    exc = context.get("exception")
    failed_task = ti.task_id if ti else "unknown task"
    error_message = str(exc) if exc else "unknown failure"

    try:
        logs = getattr(exc, "logs", None)
        if logs:
            lines = [str(line) for line in logs]
            traceback_start = next(
                (i for i in range(len(lines) - 1, -1, -1)
                 if "Traceback (most recent call last)" in lines[i]),
                None,
            )
            tail = "\n".join(lines[traceback_start:] if traceback_start is not None else lines[-20:])
            error_message = f"{error_message}\n{tail}"
    except Exception:
        pass

    try:
        http_requests.post(
            failure_callback_url,
            headers={"Authorization": f"Bearer {p.get('callback_token', '')}"},
            json={"failed_task": failed_task, "error": error_message},
            timeout=30,
        )
    except Exception:
        pass


default_args = {
    "owner": "interact",
    "retries": 1,
    "retry_delay": timedelta(seconds=60),
    "execution_timeout": timedelta(hours=2),
    "on_failure_callback": _report_failure_to_coordinator,
}

dag = DAG(
    dag_id="fl_segresnet_local",
    default_args=default_args,
    description="One federated round of local SegResNet (Auto3DSeg) fingerprinting or training",
    schedule_interval=None,
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=["federated-learning", "segresnet", "auto3dseg", "monai"],
    params={
        "run_id": "",
        "base_run_id": "",   # the TRUE run id -- see dag_fl_segresnet_coordinator.py's _distribute_round
        "round": 0,
        "round_type": "fingerprint",   # "fingerprint" or "train"
        "modality": "CT",
        "recipe_url": "",              # empty on the fingerprint round
        "global_model_url": "",        # empty on round 1 (first training round)
        "callback_url": "",
        "failure_callback_url": "",
        "callback_token": "",
        "local_epochs": 1,
        "num_rounds": 0,     # total planned rounds -- for this site's own progress display only
        "sites": [],         # every participating site's name -- for this site's own display only
    },
    render_template_as_native_obj=True,
)


def _round_dir(base_run_id, round_n):
    # Nested under local/ so this site's own round work can never collide
    # with the coordinator's OWN round_<n>/ (uploads/, global_model_next.pt,
    # ...) when this host is both coordinator and a participant in the
    # same run -- see the module docstring.
    return os.path.join(WORKFLOWS_DIR, "fl-segresnet", base_run_id, "local", f"round_{round_n}")


def _run_dir(run_id):
    return os.path.join(WORKFLOWS_DIR, "fl-segresnet", run_id)


def _update_participant_metadata(run_dir, run_id, p, **updates):
    """Maintains THIS site's own top-level run record, separate from
    metadata.json (which only the coordinator's _run_federated_training
    writes -- see dag_fl_segresnet_coordinator.py). Written under
    participant_metadata.json instead of metadata.json so a host that is
    both coordinator AND a participant for the same run (self-registered
    as its own remote instance) never has two DAGs racing to write the
    same file.

    Without this, a pure participant site (never the coordinator for a
    given run) never had a top-level <run_id> folder at all -- only its
    own round_<n> subfolders -- which meant federated_rounds.index()
    never listed the run on that site's own Training Rounds page, and
    the coordinator's end-of-run final-model push (receive_final_model)
    had nowhere to save into and silently failed.
    """
    path = os.path.join(run_dir, "participant_metadata.json")
    if os.path.isfile(path):
        with open(path) as f:
            metadata = json.load(f)
    else:
        metadata = {
            "run_id": run_id, "model": "segresnet_auto3dseg", "status": "submitted",
            "current_round": 0, "sites": p.get("sites", []),
            "submitted_at": datetime.utcnow().isoformat(),
        }
    metadata["num_rounds"] = p.get("num_rounds") or metadata.get("num_rounds", 0)
    metadata.setdefault("started_at", datetime.utcnow().isoformat())
    metadata.update(updates)
    with open(path, "w") as f:
        json.dump(metadata, f, indent=2)


def _report_cancellation_to_coordinator(p, task_id):
    """Tells the coordinator promptly that THIS site backed out on its
    own -- reuses the SAME _failed-marker channel a genuine crash already
    uses (_report_failure_to_coordinator / federated_rounds.
    report_round_failure / the coordinator's own _wait_for_uploads),
    rather than inventing a parallel one, so the coordinator finds out
    within one poll cycle (~30s) instead of only noticing a missing site
    after its full multi-hour timeout -- and with an honest reason
    ("cancelled by admin on this site") instead of a generic one."""
    failure_callback_url = p.get("failure_callback_url", "")
    if not failure_callback_url:
        return
    try:
        http_requests.post(
            failure_callback_url,
            headers={"Authorization": f"Bearer {p.get('callback_token', '')}"},
            json={"failed_task": task_id, "error": "cancelled by admin on this site"},
            timeout=30,
        )
    except Exception:
        pass


def _validate_and_fetch(**context):
    p = context["params"]
    run_id = p["run_id"] or context["dag_run"].run_id
    round_n = p["round"]
    # base_run_id is the SAME across every round of one federation;
    # run_id above changes every round (it's suffixed "_r<N>_<site>") --
    # participant_metadata.json needs the former so this site's own top-
    # level run record persists round to round instead of a fresh one
    # appearing each time. Falls back to run_id for any in-flight run
    # that started before this field existed.
    base_run_id = p.get("base_run_id") or run_id

    run_dir = _run_dir(base_run_id)
    os.makedirs(run_dir, exist_ok=True)
    if os.path.isfile(os.path.join(run_dir, "_cancel_requested")):
        # Admin clicked Cancel on THIS site's own Training Rounds page
        # (federated_rounds.cancel_round -- same marker-file mechanism
        # the coordinator's own _wait_for_uploads polls for). Checked
        # here, before any real work starts, and again in
        # _upload_to_coordinator before a result ships -- there is no
        # way to interrupt run_fl_training once it's already training
        # (same tradeoff the coordinator itself accepts for its own
        # long-running work), so a cancel clicked mid-training still
        # lets that round finish, it just never gets uploaded or counted.
        _update_participant_metadata(run_dir, base_run_id, p, status="cancelled")
        _report_cancellation_to_coordinator(p, "validate_and_fetch")
        raise AirflowSkipException(f"round {round_n}: cancelled by admin before starting")
    _update_participant_metadata(run_dir, base_run_id, p, status="running")

    round_dir = _round_dir(base_run_id, round_n)
    os.makedirs(round_dir, exist_ok=True)
    os.makedirs(os.path.join(round_dir, "local-output"), exist_ok=True)
    auth = {"Authorization": f"Bearer {p['callback_token']}"}

    recipe_dir = ""
    global_model_path = "NONE"

    if p["round_type"] == "train":
        resp = http_requests.get(p["recipe_url"], headers=auth, timeout=120)
        resp.raise_for_status()
        recipe_tarball = os.path.join(round_dir, "recipe.tar.gz")
        with open(recipe_tarball, "wb") as f:
            f.write(resp.content)
        recipe_extract_dir = os.path.join(round_dir, "recipe")
        with tarfile.open(recipe_tarball) as tar:
            tar.extractall(recipe_extract_dir)
        recipe_dir = os.path.join(recipe_extract_dir, "segresnet_0")

        if p["global_model_url"]:
            resp = http_requests.get(p["global_model_url"], headers=auth, timeout=120)
            resp.raise_for_status()
            global_model_path = os.path.join(round_dir, "global_model.pt")
            with open(global_model_path, "wb") as f:
                f.write(resp.content)

    metadata = {
        "run_id": run_id, "round": round_n, "round_type": p["round_type"], "status": "training",
        "started_at": datetime.utcnow().isoformat(),
    }
    with open(os.path.join(round_dir, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2)

    return {"round_dir": round_dir, "recipe_dir": recipe_dir, "global_model_path": global_model_path}


validate_and_fetch = PythonOperator(
    task_id="validate_and_fetch",
    python_callable=_validate_and_fetch,
    dag=dag,
)


run_fl_training = DockerOperator(
    task_id="run_fl_training",
    image="local-only/fl-segresnet-trainer:0.1.0",
    api_version="auto",
    auto_remove="success",
    docker_url="unix:///var/run/docker.sock",
    mount_tmp_dir=False,
    device_requests=[DeviceRequest(count=1, capabilities=[["gpu"]])],
    # SegResNet's data-driven crop size (224x224x144 on the spleen test
    # data) runs ~8x more voxels through DataLoader workers at once than
    # a fixed 96x96x96 crop -- the 4GB that's enough for that caused a
    # "Bus error" (out of shared memory) here. Generous, not tuned. Auto3DSeg
    # also keeps its dataset cache in shared memory; the trainer caps that
    # cache to whatever this is set to (train_fl_round_segresnet.py).
    shm_size=64 * 1024 ** 3,
    mounts=[
        Mount(source=WORKFLOWS_HOST_DIR, target="/data/airflow/workflows", type="bind"),
        Mount(source=LOCAL_TRAINING_DATA_HOST_DIR, target="/data/fl_local_training", type="bind", read_only=True),
    ],
    environment={
        "ROUND_TYPE": "{{ params.round_type }}",
        "LOCAL_DATA_DIR": "/data/fl_local_training",
        "OUTPUT_DIR": "/data/airflow/workflows/fl-segresnet/{{ params.base_run_id or params.run_id or dag_run.run_id }}/local/round_{{ params.round }}/local-output",
        "RECIPE_DIR": "{{ ti.xcom_pull(task_ids='validate_and_fetch')['recipe_dir'] }}",
        "GLOBAL_MODEL_PATH": "{{ ti.xcom_pull(task_ids='validate_and_fetch')['global_model_path'] }}",
        "LOCAL_EPOCHS": "{{ params.local_epochs }}",
        "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
        **PROXY_ENV,
    },
    execution_timeout=timedelta(hours=1, minutes=30),
    dag=dag,
)


UPLOAD_MAX_ATTEMPTS = 3
UPLOAD_RETRY_DELAY_SECONDS = 5


def _upload_to_coordinator(**context):
    p = context["params"]
    run_id = p["run_id"] or context["dag_run"].run_id
    base_run_id = p.get("base_run_id") or run_id
    run_dir = _run_dir(base_run_id)
    if os.path.isfile(os.path.join(run_dir, "_cancel_requested")):
        # Cancelled while run_fl_training was already running -- too late
        # to stop that, but not too late to keep its result from ever
        # being uploaded or counted. See the matching check in
        # _validate_and_fetch for the fuller explanation.
        _update_participant_metadata(run_dir, base_run_id, p, status="cancelled")
        _report_cancellation_to_coordinator(p, "upload_to_coordinator")
        raise AirflowSkipException(f"round {p['round']}: cancelled by admin before upload")

    round_dir = _round_dir(base_run_id, p["round"])
    output_dir = os.path.join(round_dir, "local-output")

    if p["round_type"] == "fingerprint":
        upload_name = "fingerprint.yaml"
        n_cases = None
    else:
        upload_name = "plaintext_update.pt"  # the name dag_fl_segresnet_coordinator.py's _aggregate_round reads
        import torch
        state = torch.load(os.path.join(output_dir, "local_update.pt"), map_location="cpu", weights_only=False)
        n_cases = state.get("n_cases")

    source_path = os.path.join(output_dir, "fingerprint.yaml" if p["round_type"] == "fingerprint" else "local_update.pt")

    for attempt in range(1, UPLOAD_MAX_ATTEMPTS + 1):
        with open(source_path, "rb") as fh:
            try:
                resp = http_requests.post(
                    p["callback_url"],
                    headers={"Authorization": f"Bearer {p['callback_token']}"},
                    data={"run_id": run_id, "round": p["round"], "n_cases": n_cases},
                    files={upload_name: fh},
                    timeout=300,
                )
                resp.raise_for_status()
                break
            except http_requests.exceptions.RequestException as exc:
                detail = ""
                if getattr(exc, "response", None) is not None:
                    detail = exc.response.text
                print(f"upload attempt {attempt}/{UPLOAD_MAX_ATTEMPTS} failed: {exc} {detail}")
                if attempt == UPLOAD_MAX_ATTEMPTS:
                    raise
                time.sleep(UPLOAD_RETRY_DELAY_SECONDS)

    metadata_path = os.path.join(round_dir, "metadata.json")
    with open(metadata_path) as f:
        metadata = json.load(f)
    metadata["status"] = "uploaded"
    metadata["completed_at"] = datetime.utcnow().isoformat()
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)

    # Only "train" rounds advance this site's own progress counter --
    # matches the coordinator's own semantics (dag_fl_segresnet_coordinator.py
    # never counts round 0's fingerprint round towards num_rounds either).
    participant_updates = {"status": "running"}
    if p["round_type"] == "train":
        participant_updates["current_round"] = p["round"]
    _update_participant_metadata(_run_dir(base_run_id), base_run_id, p, **participant_updates)


upload_to_coordinator = PythonOperator(
    task_id="upload_to_coordinator",
    python_callable=_upload_to_coordinator,
    dag=dag,
)


validate_and_fetch >> run_fl_training >> upload_to_coordinator
