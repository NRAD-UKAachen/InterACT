"""Standalone smoke test for batch_jobs.py's durability + resume behavior
(see Documentation/API_and_API_storage.md).
Runs a fake, GPU-free job across two OS *processes* to simulate an API
container restart mid-batch: phase1 starts a batch job and hard-kills itself
(os._exit, skipping any cleanup/thread-join) partway through, with some
cases already 'done', one 'failed', and some still 'running'. phase2 is a
fresh process -- nothing in memory -- that must reload job state from disk,
skip the already-'done' cases, and rerun everything else to completion.

No Flask route, no docker/GPU involved -- this only exercises batch_jobs.py's
persistence/scheduling, not the fastsurfer_core integration (see routes.py's
batch_run_route/_run_batch_case for that; testing it end-to-end means
literally killing and restarting the ohif_api container mid-batch on a real
cohort).

Run from /app inside the ohif_api container, as two separate invocations
(from the repo root, so Compose can find the project):

    docker compose exec -e BATCH_JOB_CONCURRENCY=2 ohif_api python3 -m app.test_batch_jobs phase1
    docker compose exec -e BATCH_JOB_CONCURRENCY=2 ohif_api python3 -m app.test_batch_jobs phase2

phase2 prints a PASS/FAIL summary.
"""
import json
import os
import shutil
import sys
import time

from . import batch_jobs
from .storage_paths import API_STORAGE_DIR

TEST_ROOT = f'{API_STORAGE_DIR}/batch_jobs_test'
JOB_ID_FILE = os.path.join(TEST_ROOT, 'job_id.txt')
LOG_FILE = os.path.join(TEST_ROOT, 'runner_log.jsonl')
KIND = 'test_resume'

# case_id -> (seconds of simulated work, whether it always raises).
# A/B finish before phase1's kill; C always fails; D/E are still mid-"work"
# when phase1 is killed, so they were never marked 'done'.
CASE_PLAN = {
    'A': (1.0, False),
    'B': (1.0, False),
    'C': (0.1, True),
    'D': (5.0, False),
    'E': (5.0, False),
}


def _log(entry):
    os.makedirs(TEST_ROOT, exist_ok=True)
    with open(LOG_FILE, 'a') as f:
        f.write(json.dumps(entry) + '\n')


def fake_runner(job, case_id, gpu_index):
    phase = sys.argv[1]
    _log({"phase": phase, "case_id": case_id, "event": "start", "gpu_index": gpu_index, "t": time.time()})
    duration, should_fail = CASE_PLAN[case_id]
    time.sleep(duration)
    if should_fail:
        _log({"phase": phase, "case_id": case_id, "event": "raise", "t": time.time()})
        raise RuntimeError(f"case {case_id} deliberately fails")
    _log({"phase": phase, "case_id": case_id, "event": "success", "t": time.time()})


batch_jobs.register_runner(KIND, fake_runner)


def phase1():
    if os.path.exists(TEST_ROOT):
        shutil.rmtree(TEST_ROOT)
    os.makedirs(TEST_ROOT, exist_ok=True)

    # Clear out any stale job left behind by a previous, incomplete run of
    # this script (e.g. a phase1 that never got matched with a phase2) --
    # otherwise load_jobs_from_disk() in a later phase2 would resume it
    # alongside the new job and both would race on the same case ids and log
    # file.
    for name in os.listdir(batch_jobs.BATCH_JOBS_ROOT) if os.path.isdir(batch_jobs.BATCH_JOBS_ROOT) else []:
        if name.startswith("testresume"):
            shutil.rmtree(os.path.join(batch_jobs.BATCH_JOBS_ROOT, name), ignore_errors=True)
            if name.endswith(".json"):
                os.remove(os.path.join(batch_jobs.BATCH_JOBS_ROOT, name))

    job_id = f"testresume{int(time.time())}"
    work_dir = batch_jobs.job_work_dir(job_id)
    os.makedirs(work_dir, exist_ok=True)
    with open(JOB_ID_FILE, 'w') as f:
        f.write(job_id)

    cases = {cid: {} for cid in CASE_PLAN}
    batch_jobs.start_batch_job(job_id, kind=KIND, cases=cases, work_dir=work_dir)

    print(f"phase1: started job {job_id}, letting it run for 2s before a hard kill...")
    time.sleep(2.0)

    status = batch_jobs.get_batch_job_status(job_id)
    print(f"phase1: status at kill time:\n{json.dumps(status, indent=2)}")

    # Simulate the container being killed outright: no cleanup, no thread
    # joins -- exactly what `docker kill`/an OOM-kill/a host reboot does.
    os._exit(0)


def phase2():
    with open(JOB_ID_FILE) as f:
        job_id = f.read().strip()

    pre_status = batch_jobs.get_batch_job_status(job_id)
    assert pre_status is None, "fresh process should have nothing in memory before load_jobs_from_disk()"

    print("phase2: fresh process, loading job state from disk...")
    batch_jobs.load_jobs_from_disk()

    status = None
    for _ in range(60):
        status = batch_jobs.get_batch_job_status(job_id)
        if status['status'] == 'done':
            break
        time.sleep(0.5)
    else:
        raise TimeoutError("resumed job did not finish within 30s")

    print(f"phase2: final status:\n{json.dumps(status, indent=2)}")

    with open(LOG_FILE) as f:
        log_entries = [json.loads(line) for line in f]

    starts_by_case = {}
    for entry in log_entries:
        if entry["event"] == "start":
            starts_by_case.setdefault(entry["case_id"], []).append(entry["phase"])

    failures = []

    # A and B were already 'done' at kill time -- resume must skip them.
    for cid in ("A", "B"):
        if starts_by_case.get(cid) != ["phase1"]:
            failures.append(f"{cid}: expected exactly one start (phase1 only), got {starts_by_case.get(cid)}")

    # C (always fails), D and E (interrupted mid-run) were not 'done' at kill
    # time -- resume must rerun every one of them.
    for cid in ("C", "D", "E"):
        if starts_by_case.get(cid) != ["phase1", "phase2"]:
            failures.append(f"{cid}: expected phase1-then-phase2 start, got {starts_by_case.get(cid)}")

    expected_case_status = {"A": "done", "B": "done", "C": "failed", "D": "done", "E": "done"}
    actual_case_status = {c["id"]: c["status"] for c in status["per_case"]}
    if actual_case_status != expected_case_status:
        failures.append(f"case statuses: expected {expected_case_status}, got {actual_case_status}")

    if (status["total"], status["completed"], status["failed"], status["running"]) != (5, 4, 1, 0):
        failures.append(f"aggregate counts wrong: {status}")

    # A and B ran concurrently in phase1 -- their worker slots must have been
    # pinned to different GPU indices, not both defaulting to the same one.
    ab_gpu = {e["case_id"]: e["gpu_index"] for e in log_entries if e["event"] == "start" and e["case_id"] in ("A", "B")}
    if len(set(ab_gpu.values())) != 2:
        failures.append(f"expected A and B pinned to different GPU indices, got {ab_gpu}")

    if failures:
        print("FAIL:")
        for f_ in failures:
            print(f"  - {f_}")
        sys.exit(1)

    print("PASS: resumed job skipped already-completed cases, reran everything else "
          "exactly once to completion, and preserved per-slot GPU pinning.")


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else None
    if phase == "phase1":
        phase1()
    elif phase == "phase2":
        phase2()
    else:
        print("usage: python3 -m app.test_batch_jobs {phase1|phase2}")
        sys.exit(2)
