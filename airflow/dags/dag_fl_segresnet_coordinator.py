"""FL SegResNet (Auto3DSeg) -- coordinator DAG.

One task, blocking for the whole job, plain unencrypted FedAvg (see
_aggregate_round's own comment below for why unencrypted), with one
structural twist compared to a plain round-based FL coordinator: round 0
is not a training round at all, it's a fingerprint round.

Why: Auto3DSeg normally analyzes whatever dataset it's given and decides
its own spacing/patch-size/etc -- fine for one site, useless for
federation, since every site would get a DIFFERENT shape and their
results couldn't be averaged. So every site first computes its own local
data "fingerprint" (small summary numbers, never raw images), and every
site's fingerprint is merged into one combined picture before deriving
the actual training shape -- split across this DAG, the local
counterpart and fl-segresnet-trainer.

Round numbering (params.num_rounds counts TRAINING rounds only, so an
admin asking for "10 rounds" gets 10 rounds of actual training, not 9 --
the fingerprint step is not one of their 10):
  round 0            fingerprint: every site uploads its own by-case
                      stats, coordinator merges + generates the shared
                      recipe (spacing/patch-size/intensity bounds +
                      the actual Auto3DSeg training scripts, downloaded
                      once here via BundleGen).
  round 1..num_rounds normal FedAvg training rounds against that shared,
                      frozen recipe from here on.

Shared volume layout:
  /data/airflow/workflows/fl-segresnet/<run_id>/
    fingerprints/<site_name>.yaml          <- round 0 uploads
    recipe/segresnet_0/{configs,scripts}/  <- generated once, round 0
    recipe.tar.gz                          <- what sites actually download
    round_<n>/
      global_model.pt                      <- round n's starting weights (n>1)
      uploads/<site_name>/uploaded/plaintext_update.pt
      global_model_next.pt                 <- round n's aggregated output
    metadata.json
"""
import json
import os
import time
from datetime import datetime, timedelta

import requests as http_requests
import torch
from airflow import DAG
from airflow.api.common.trigger_dag import trigger_dag
from airflow.operators.python import PythonOperator

# yaml/monai are imported locally inside _merge_fingerprints_and_generate_recipe,
# not here -- the Airflow scheduler's own image only has torch installed
# (added for the plain-FedAvg torch.save/load calls below), not monai, and
# this file is imported just to discover the DAG object even on rounds
# that never call that function.

WORKFLOWS_HOST_DIR = os.environ.get("AIRFLOW_WORKFLOWS_HOST_DIR", "")
if not WORKFLOWS_HOST_DIR:
    raise RuntimeError("AIRFLOW_WORKFLOWS_HOST_DIR must be set in the root .env")
WORKFLOWS_DIR = "/data/airflow/workflows"

POLL_INTERVAL_SECONDS = 30


class RunCancelled(Exception):
    """Raised from _wait_for_uploads when it finds the marker file
    federated_rounds.cancel_round writes, so the run exits through the
    normal except-block below (metadata.json correctly updated) instead
    of being killed from outside and left stuck at "running" forever."""
ROUND_TIMEOUT_SECONDS = 4 * 60 * 60

default_args = {
    "owner": "interact",
    "retries": 0,
    "execution_timeout": timedelta(days=3),
}

dag = DAG(
    dag_id="fl_segresnet_coordinator",
    default_args=default_args,
    description="Coordinator: fingerprint-then-FedAvg federated SegResNet (Auto3DSeg)",
    schedule_interval=None,
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=["federated-learning", "segresnet", "auto3dseg", "monai", "coordinator"],
    params={
        "run_id": "",
        "num_rounds": 10,  # TRAINING rounds -- see module docstring
        "modality": "CT",
        "local_epochs": 1,
        "remote_instances": [],
    },
    render_template_as_native_obj=True,
)


def _run_dir(run_id):
    return os.path.join(WORKFLOWS_DIR, "fl-segresnet", run_id)


def _check_remote_response(resp, inst_name, action):
    """raise_for_status() with the remote's own reason. Remote FL routes
    answer errors as {"error": ...} (e.g. "model ... is not allowed for
    <us> here"); raise_for_status() would discard that and leave only
    "403 Client Error: FORBIDDEN for url: ...", which is what reaches the
    Training Rounds page via metadata["error"]."""
    if resp.ok:
        return
    try:
        reason = resp.json().get("error")
    except ValueError:
        reason = None
    raise RuntimeError(
        f"{inst_name} refused to {action} (HTTP {resp.status_code}): "
        f"{reason or resp.reason}"
    )


def _distribute_round(run_id, round_n, round_type, remote_instances, modality, local_epochs, coordinator_upload_base_url, recipe_ready, num_rounds):
    global_model_url = ""
    if round_type == "train" and round_n > 1:
        global_model_url = f"{coordinator_upload_base_url}/{run_id}/round_{round_n}/global_model"
    recipe_url = f"{coordinator_upload_base_url}/{run_id}/recipe" if recipe_ready else ""

    for inst in remote_instances:
        conf = {
            "run_id": f"{run_id}_r{round_n}_{inst['name']}",
            # The TRUE run id, distinct from the per-round one above --
            # dag_fl_segresnet_local.py's participant_metadata.json needs
            # this specifically, so a site's own top-level run record
            # persists across every round instead of starting a fresh
            # one each time (conf["run_id"] changes every single round;
            # this never does).
            "base_run_id": run_id,
            "round": round_n,
            "round_type": round_type,
            "modality": modality,
            "recipe_url": recipe_url,
            "global_model_url": global_model_url,
            "callback_url": f"{coordinator_upload_base_url}/{run_id}/round_{round_n}/upload/{inst['name']}",
            "failure_callback_url": f"{coordinator_upload_base_url}/{run_id}/round_{round_n}/failed/{inst['name']}",
            "local_epochs": local_epochs,
            # So each site's own Training Rounds page can show real
            # progress ("N/num_rounds") instead of just "it ran" -- see
            # dag_fl_segresnet_local.py's participant_metadata.json.
            "num_rounds": num_rounds,
            "sites": [i["name"] for i in remote_instances],
        }
        if inst.get("local"):
            # This site training its own data too (see
            # federated_rounds.start_round's train_local_too) -- run
            # fl_segresnet_local directly through Airflow itself instead
            # of the HTTP+token round-trip real remotes need, since there
            # is no separate remote instance here to call. callback_token
            # is set explicitly here because there is no incoming_job
            # call happening to inject it -- outgoing_token on this entry
            # is already the self-call token (get_self_call_token()),
            # which upload_round_result and every other instance-token
            # route already accept.
            conf["callback_token"] = inst["outgoing_token"]
            trigger_dag(dag_id="fl_segresnet_local", run_id=conf["run_id"], conf=conf)
        else:
            resp = http_requests.post(
                inst["incoming_job_url"],
                headers={"Authorization": f"Bearer {inst['outgoing_token']}"},
                json={"dag_id": "fl_segresnet_local", "conf": conf},
                timeout=30,
            )
            _check_remote_response(resp, inst["name"], f"start round {round_n}")
        print(f"round {round_n} ({round_type}): distributed to {inst['name']}")


def _wait_for_uploads(run_dir, round_n, remote_instances):
    # Polls every remote_instances entry for a _done or _failed marker
    # each cycle, checking cancellation first (see cancel_round in
    # federated_rounds.py) -- raises immediately on the first failure or
    # cancellation found, rather than waiting out the full timeout below
    # for something already known to have gone wrong.
    upload_dir = os.path.join(run_dir, f"round_{round_n}", "uploads")
    deadline = time.time() + ROUND_TIMEOUT_SECONDS
    pending = {inst["name"] for inst in remote_instances}

    while pending and time.time() < deadline:
        if os.path.isfile(os.path.join(run_dir, "_cancel_requested")):
            raise RunCancelled(f"round {round_n}: cancelled by admin")
        for name in list(pending):
            site_dir = os.path.join(upload_dir, name)
            failed_marker = os.path.join(site_dir, "_failed")
            if os.path.isfile(failed_marker):
                with open(failed_marker) as f:
                    failure_info = json.load(f)
                raise RuntimeError(
                    f"round {round_n}: {name} reported failure in "
                    f"{failure_info.get('failed_task', 'unknown task')}: "
                    f"{failure_info.get('error', 'unknown error')}"
                )
            if os.path.isfile(os.path.join(site_dir, "_done")):
                pending.discard(name)
                print(f"round {round_n}: {name} uploaded ({len(remote_instances) - len(pending)}/{len(remote_instances)})")
        if pending:
            time.sleep(POLL_INTERVAL_SECONDS)

    if pending:
        raise TimeoutError(f"round {round_n}: timed out waiting for {pending} after {ROUND_TIMEOUT_SECONDS}s")


def _merge_fingerprints_and_generate_recipe(run_dir, round_0_dir, remote_instances, modality):
    """Round 0's aggregation step: merge every site's by-case fingerprint
    into one combined list, then generate the shared training recipe
    from it (configs AND Auto3DSeg's own scripts, since sites reuse that
    code as-is rather than vendoring a copy per site). Runs as a
    `docker run` of fl-segresnet-trainer (ROUND_TYPE=merge) rather than
    directly in this process -- the Airflow scheduler's own image
    doesn't have monai installed, see the module-level comment on this
    file's imports."""
    import docker

    proxy_env = {k: os.environ[k] for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY") if k in os.environ}
    client = docker.from_env()
    logs = client.containers.run(
        image="local-only/fl-segresnet-trainer:0.1.0",
        environment={
            "ROUND_TYPE": "merge",
            "ROUND_0_UPLOAD_DIR": os.path.join(round_0_dir, "uploads"),
            "SITE_NAMES": ",".join(inst["name"] for inst in remote_instances),
            "MODALITY": modality,
            "RECIPE_DIR": os.path.join(run_dir, "recipe"),
            "RECIPE_TARBALL_PATH": os.path.join(run_dir, "recipe.tar.gz"),
            **proxy_env,
        },
        volumes={WORKFLOWS_HOST_DIR: {"bind": "/data/airflow/workflows", "mode": "rw"}},
        remove=True,
        stdout=True,
        stderr=True,
    )
    print(logs.decode(errors="replace"))
    print(f"generated shared recipe at {os.path.join(run_dir, 'recipe', 'segresnet_0')}")


def _aggregate_round(run_dir, round_n, remote_instances):
    # Plain FedAvg, unweighted -- no encryption: both current sites already
    # trust each other, and CKKS homomorphic aggregation (this project's
    # earlier approach, used before SegResNet existed) turned out to
    # corrupt uploads at the chunk counts a real model's parameter count
    # produced, with no clean fix found -- see git history for the
    # details. Every model built since, including this one, has stayed
    # with plain FedAvg rather than reintroduce that risk.
    upload_dir = os.path.join(run_dir, f"round_{round_n}", "uploads")
    state_dicts = []
    for inst in remote_instances:
        site_dir = os.path.join(upload_dir, inst["name"], "uploaded")
        state = torch.load(os.path.join(site_dir, "plaintext_update.pt"), map_location="cpu", weights_only=False)
        state_dicts.append(state["model"])

    averaged_state_dict = {
        key: sum(sd[key] for sd in state_dicts) / len(state_dicts)
        for key in state_dicts[0]
    }
    next_model_path = os.path.join(run_dir, f"round_{round_n}", "global_model_next.pt")
    torch.save({"model": averaged_state_dict}, next_model_path)
    return next_model_path


def _notify_remotes_cancelled(run_id, remote_instances):
    """Tells every REAL remote (never the local/self-training entry --
    that one already sees the same _cancel_requested marker directly, no
    network hop needed) to cancel its own part too, via the instance-
    token route federated_rounds.cancel_round_remote. Best-effort, same
    as _distribute_final_model below: a site that can't be reached just
    keeps working until its own timeout, same as before this existed --
    this only makes the common case (a reachable site) find out in
    seconds instead of hours."""
    for inst in remote_instances:
        if inst.get("local"):
            continue
        try:
            resp = http_requests.post(
                inst["incoming_cancel_url"],
                headers={"Authorization": f"Bearer {inst['outgoing_token']}"},
                timeout=30,
            )
            _check_remote_response(resp, inst["name"], "accept the cancellation")
            print(f"cancellation sent to {inst['name']}")
        except Exception as exc:
            print(f"WARNING: failed to send cancellation to {inst['name']}: {exc}")


FINAL_MODEL_MAX_ATTEMPTS = 3
FINAL_MODEL_RETRY_DELAY_SECONDS = 30


def _distribute_final_model(run_id, final_model_path, remote_instances):
    """Pushes the finished model to every real remote. Returns
    {site_name: error} for each site that still didn't get it after
    FINAL_MODEL_MAX_ATTEMPTS -- a site that never receives it stays at
    "running" on its own Training Rounds page with no download link, so
    the caller records these instead of only logging them (see
    federated_rounds.resend_final_model for the admin's way to retry)."""
    undelivered = {}
    for inst in remote_instances:
        if inst.get("local"):
            continue  # already has it -- this instance produced it, as coordinator
        for attempt in range(1, FINAL_MODEL_MAX_ATTEMPTS + 1):
            try:
                with open(final_model_path, "rb") as f:
                    resp = http_requests.post(
                        inst["incoming_final_model_url"],
                        headers={"Authorization": f"Bearer {inst['outgoing_token']}"},
                        files={"final_model": ("final_model.pt", f)},
                        timeout=300,
                    )
                    _check_remote_response(resp, inst["name"], "accept the final model")
                print(f"final model sent to {inst['name']}")
                break
            except Exception as exc:
                print(f"WARNING: final model to {inst['name']}, attempt {attempt}/{FINAL_MODEL_MAX_ATTEMPTS} failed: {exc}")
                if attempt == FINAL_MODEL_MAX_ATTEMPTS:
                    undelivered[inst["name"]] = str(exc)
                else:
                    time.sleep(FINAL_MODEL_RETRY_DELAY_SECONDS)
    return undelivered


def _run_federated_training(**context):
    p = context["params"]
    run_id = p["run_id"] or context["dag_run"].run_id
    run_dir = _run_dir(run_id)
    os.makedirs(run_dir, exist_ok=True)

    coordinator_upload_base_url = os.environ.get(
        "FL_COORDINATOR_UPLOAD_BASE_URL",
        "http://docker-context-gui:5000/federated/rounds",
    )

    # Preserve whatever start_round already wrote (sites, model,
    # submitted_at) instead of replacing it -- this used to silently drop
    # those fields the moment the run left "submitted", which is why the
    # Training Rounds table would show a blank Sites column for anything
    # past that point.
    metadata_path = os.path.join(run_dir, "metadata.json")
    if os.path.isfile(metadata_path):
        with open(metadata_path) as f:
            metadata = json.load(f)
    else:
        metadata = {"run_id": run_id}
    metadata.update({
        "status": "running",
        "started_at": datetime.utcnow().isoformat(),
        "num_rounds": p["num_rounds"],
        "current_round": 0,
    })
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)

    try:
        # --- Round 0: fingerprint, not training ---
        round_0_dir = os.path.join(run_dir, "round_0")
        os.makedirs(os.path.join(round_0_dir, "uploads"), exist_ok=True)
        _distribute_round(run_id, 0, "fingerprint", p["remote_instances"], p["modality"], p["local_epochs"], coordinator_upload_base_url, recipe_ready=False, num_rounds=p["num_rounds"])
        _wait_for_uploads(run_dir, 0, p["remote_instances"])
        _merge_fingerprints_and_generate_recipe(run_dir, round_0_dir, p["remote_instances"], p["modality"])
        print("round 0 (fingerprint) complete")

        # --- Rounds 1..num_rounds: normal FedAvg training ---
        for round_n in range(1, p["num_rounds"] + 1):
            round_dir = os.path.join(run_dir, f"round_{round_n}")
            os.makedirs(os.path.join(round_dir, "uploads"), exist_ok=True)

            if round_n > 1:
                prev_next = os.path.join(run_dir, f"round_{round_n - 1}", "global_model_next.pt")
                os.link(prev_next, os.path.join(round_dir, "global_model.pt"))

            _distribute_round(run_id, round_n, "train", p["remote_instances"], p["modality"], p["local_epochs"], coordinator_upload_base_url, recipe_ready=True, num_rounds=p["num_rounds"])
            _wait_for_uploads(run_dir, round_n, p["remote_instances"])
            _aggregate_round(run_dir, round_n, p["remote_instances"])

            metadata["current_round"] = round_n
            with open(os.path.join(run_dir, "metadata.json"), "w") as f:
                json.dump(metadata, f, indent=2)
            print(f"round {round_n} complete ({round_n}/{p['num_rounds']})")
    except Exception as exc:
        metadata["status"] = "cancelled" if isinstance(exc, RunCancelled) else "failed"
        metadata["error"] = str(exc) or exc.__class__.__name__
        metadata["failed_at"] = datetime.utcnow().isoformat()
        with open(os.path.join(run_dir, "metadata.json"), "w") as f:
            json.dump(metadata, f, indent=2)
        if isinstance(exc, RunCancelled):
            _notify_remotes_cancelled(run_id, p["remote_instances"])
        raise

    final_model = os.path.join(run_dir, f"round_{p['num_rounds']}", "global_model_next.pt")
    undelivered = _distribute_final_model(run_id, final_model, p["remote_instances"])

    # Still "success": training finished and the model exists here. What
    # didn't happen is recorded separately, for the Training Rounds page
    # to show and offer to retry.
    metadata["status"] = "success"
    metadata["completed_at"] = datetime.utcnow().isoformat()
    metadata["final_model_path"] = final_model
    if undelivered:
        metadata["final_model_undelivered"] = undelivered
    with open(os.path.join(run_dir, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2)


run_federated_training = PythonOperator(
    task_id="run_federated_training",
    python_callable=_run_federated_training,
    dag=dag,
)
