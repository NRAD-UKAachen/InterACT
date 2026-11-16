"""Durable job registry + bounded worker pool for cohort-sized batches
(see Documentation/API_and_API_storage.md).

jobs.py's in-memory dict + single daemon thread is right for a multi-minute
single-study job, but wrong here: a batch can run for hours to days over
100-200 cases, so this module instead:

  - persists job + per-case state to a JSON file under
    API_storage/batch_jobs/<job_id>.json after every state change, so a
    container restart loses at most whatever each worker slot had in flight,
    not the whole batch;
  - runs cases through a small bounded worker pool, concurrency configurable
    via BATCH_JOB_CONCURRENCY (default 3 -- the reference host has 4 GPUs, one is
    left free for the interactive per-study pipeline in jobs.py), with each
    worker slot pinned to one GPU index so N concurrent tool containers
    actually spread across GPUs instead of every one of them seeing (and
    contending for/defaulting to) all four;
  - on API startup, reloads every job file: one still 'pending'/'running'
    means the process stopped mid-batch, so any case that was 'running' gets
    reset to 'pending' (its container died with the process) and the worker
    pool is relaunched over every case not already 'done'.

A batch job's *working directory* (unpacked input, scratch conversions, and
results) must live under the same persistent, bind-mounted root as the job's
JSON file -- NOT tempfile.mkdtemp()'s /tmp -- or a container restart would
wipe out the very state this module exists to preserve. See job_work_dir().

Tool-specific know-how (what a "case" needs, how to run it) does not belong
here: a caller (routes.py) registers a runner callable per job `kind` with
register_runner(); this module only owns persistence, scheduling, and GPU
slot assignment.
"""

import json
import logging
import os
import queue
import shutil
import threading
import time

from . import job_logging
from .jobs import cors_json
from .storage_paths import API_STORAGE_DIR

logger = logging.getLogger('interact.batch_jobs')

BATCH_JOBS_ROOT = f'{API_STORAGE_DIR}/batch_jobs'

CONCURRENCY = int(os.environ.get('BATCH_JOB_CONCURRENCY', '3'))

# Worker slots are spread over GPUs 0..NUM_GPUS-1 (slot_index % NUM_GPUS), which
# also keeps a misconfigured CONCURRENCY from handing a slot a device ordinal
# that doesn't exist. Same default as API/docker-compose.yml and .env.example:
# 1 is the only value safe on every host. On a multi-GPU host, set it below the
# real count to keep the last card free for jobs.py's interactive pipeline.
NUM_GPUS = int(os.environ.get('BATCH_JOB_NUM_GPUS', '1'))

# job_id -> job dict (mirrors the on-disk JSON; kept in memory so status
# reads don't hit disk). Guarded by _lock for all reads/writes/mutations of
# any job or its 'cases' sub-dict.
_jobs = {}
_lock = threading.Lock()

# kind -> fn(job, case_id, gpu_index). Populated by register_runner(), which
# the owning route module calls at import time.
_RUNNERS = {}

# kind -> fn(job), run once after every case in a batch job of that kind has
# finished. Populated by register_completion_hook() -- optional, e.g. for
# writing a summary manifest into the job's result tree once every case's
# final status is known.
_COMPLETION_HOOKS = {}


def register_runner(kind, fn):
    """Register how to run one case of a batch job of type `kind`.

    fn(job, case_id, gpu_index) must run that case to completion. Its return
    value/exception decide the case's recorded status:

      - raises -> 'failed', error = str(exception)
      - returns None (or anything not recognized below) -> 'done', no error
      - returns a status string ('done'/'partial'/'failed') -> that status,
        no error
      - returns (status, error) -> that status and error message
      - returns 'pending' -> the case goes back to 'pending', no error: the
        runner noticed halt_requested() between its sub-steps and gave up
        the rest of the case, so pause/stop settles it like a case that
        never started (and a resume reruns it)

    The (status, error) / status-string forms exist for a runner that itself
    runs several independent sub-steps against one case (e.g.
    multi-tool batches: several tools against one (patient, study), where
    one tool failing should not fail the ones that succeeded) and wants to
    report 'partial' rather than being forced into a plain success/failure.
    Such a runner should also check halt_requested() between sub-steps, or
    pausing waits for every remaining sub-step of every in-flight case.
    `job` is this module's job dict (id, kind, status, work_dir, params,
    cases); `gpu_index` is this worker slot's assigned GPU.
    """
    _RUNNERS[kind] = fn


def register_completion_hook(kind, fn):
    """Register an optional fn(job) to run after every case *currently*
    settled (whether 'done', 'partial', or 'failed') in a batch job of type
    `kind` has reached that status, and before the job itself flips to
    'done'/'paused'/'stopped' -- e.g. to write a manifest summarizing every
    settled case's outcome into the job's result tree, from the in-memory
    case records, without a second filesystem walk.

    Runs on every _drive_job exit path (see _run_completion_hook), not just
    a normal finish: a job settling into 'paused' or 'stopped' can still
    have 'pending' cases that never ran, so a hook should be written to cope
    with an incomplete case set -- it's producing the manifest/overview for
    whatever result a "download preliminary results" click would ZIP up
    right now, not necessarily a finished cohort. It reruns (and overwrites
    its own output) on every later exit path too, e.g. once a paused job is
    resumed and actually finishes.

    A hook that raises is logged and otherwise ignored: a bug in optional
    summary output must not leave an otherwise-finished batch stuck at
    'running' forever."""
    _COMPLETION_HOOKS[kind] = fn


def job_work_dir(job_id):
    """Persistent working directory for a batch job's input/scratch/result
    trees. Callers must stage files here (not /tmp) before creating the job,
    since this is what survives a container restart."""
    return os.path.join(BATCH_JOBS_ROOT, job_id)


def _job_path(job_id):
    return os.path.join(BATCH_JOBS_ROOT, f'{job_id}.json')


def _save_locked(job):
    """Write `job` to disk. Caller holds _lock. Write-then-rename so a reader
    (or a restart mid-write) never observes a half-written JSON file."""
    os.makedirs(BATCH_JOBS_ROOT, exist_ok=True)
    path = _job_path(job['id'])
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(job, f, indent=2)
    os.replace(tmp_path, path)


def start_batch_job(job_id, kind, cases, work_dir, params=None, owner=None):
    """Register a new batch job and launch its worker pool in the
    background.

    `job_id` is minted by the caller (it needs the id before this call, to
    stage files under job_work_dir(job_id) -- see that function). `cases` is
    {case_id: extra_dict}; extra_dict's keys land on that case's record for
    the registered runner to read back (e.g. a per-case input directory) --
    only id/status/error/tools/study_date are exposed by
    get_batch_job_status() (see its docstring for `tools`); anything else a
    caller stashes here (e.g. scratch work_dir paths) stays internal to the
    runner. `kind` must already have a runner registered via
    register_runner().

    `owner` is bookkeeping only, not access control -- this API has no auth
    of its own (see the module docstring above). It lets a trusted caller (the GUI, which does
    gate access via Keycloak) filter list_jobs()/get_batch_job_status() to
    "this user's jobs" for display; a job_id is still enough to poll/download
    a job regardless of owner, same as before this field existed.
    """
    now = time.time()
    job = {
        "id": job_id,
        "kind": kind,
        # pending -> running -> done, with two user-requested detours:
        # running -> pausing -> paused -> (resume) -> running, and
        # (pending|running|pausing|paused) -> stopping -> stopped. See
        # pause_job/resume_job/stop_job below.
        "status": "pending",
        "work_dir": work_dir,
        "params": params or {},
        "owner": owner,
        "created_at": now,
        "updated_at": now,
        # Checked by each worker slot in _run_worker_pool before it picks up
        # its next case (not preemptively -- a case already in flight always
        # runs to completion, see pause_job's docstring). Only ever flipped
        # by pause_job/resume_job/stop_job under _lock.
        "pause_requested": False,
        "stop_requested": False,
        "cases": {
            case_id: {"id": case_id, "status": "pending", "error": None, **extra}
            for case_id, extra in cases.items()
        },
    }
    with _lock:
        _jobs[job_id] = job
        _save_locked(job)
    _launch(job_id)
    return job_id


def _launch(job_id):
    thread = threading.Thread(target=_drive_job, args=(job_id,), name=f'batch-job-{job_id}', daemon=True)
    thread.start()


def _drive_job(job_id):
    """Run a job's worker pool, logging to logs/batch/<job_id>/job.log."""
    with job_logging.log_to(job_logging.batch_job_log_path(job_id)):
        try:
            _drive_job_logged(job_id)
        except Exception:
            logger.exception("Batch job %s crashed", job_id)
            raise
        finally:
            with _lock:
                job_logging.write_batch_meta(_jobs[job_id])
                status = _jobs[job_id]['status']
            logger.info("Batch job %s: driver exited with status '%s'", job_id, status)


def _drive_job_logged(job_id):
    with _lock:
        job = _jobs[job_id]
        job['status'] = 'running'
        job['updated_at'] = time.time()
        _save_locked(job)
        pending_case_ids = [cid for cid, c in job['cases'].items() if c['status'] != 'done']
        kind = job['kind']
        job_logging.write_batch_meta(job)
        params = job['params']
        owner = job.get('owner')

    logger.info("Batch job %s (%s) running %d of %d case(s), owner=%s, concurrency=%d over %d GPU(s)",
                job_id, kind, len(pending_case_ids), len(job['cases']), owner, CONCURRENCY, NUM_GPUS)
    logger.debug("Batch job %s parameters: %s", job_id, json.dumps(params, default=str))

    _run_worker_pool(job_id, pending_case_ids, _RUNNERS[kind])

    # _run_worker_pool has joined every worker thread by now, so no case can
    # still be 'running' -- either the queue drained normally, or a pause/stop
    # request was noticed between cases and the remaining ones are untouched
    # 'pending'. A pause/stop request that lost the race (every case had
    # already been claimed by the time a worker checked the flag) leaves
    # nothing 'pending' either way, so it's `still_pending`, not the flags
    # alone, that decides whether this settles as paused/stopped rather than
    # completing normally -- otherwise a job whose last case finished right
    # as pause/stop was clicked would get stuck reporting 'paused'/'stopped'
    # with every case already done.
    with _lock:
        job = _jobs[job_id]
        stop_requested = job['stop_requested']
        pause_requested = job['pause_requested']
        still_pending = any(c['status'] == 'pending' for c in job['cases'].values())

    if still_pending and stop_requested:
        with _lock:
            job = _jobs[job_id]
            for case in job['cases'].values():
                if case['status'] == 'pending':
                    case['status'] = 'stopped'
            job['status'] = 'stopped'
            job['updated_at'] = time.time()
            _save_locked(job)
        # Run below, outside the lock: a stopped job is settled for good, so
        # whatever finished before the stop is what "the result" will ever
        # be -- worth a manifest/overview the same as a normal finish (see
        # _run_completion_hook), just built over fewer cases.
        _run_completion_hook(kind, job_id)
        return

    if still_pending and pause_requested:
        with _lock:
            job = _jobs[job_id]
            job['status'] = 'paused'
            job['updated_at'] = time.time()
            _save_locked(job)
        # A paused job may still be resumed and finish normally later (this
        # hook reruns then and overwrites these files), but someone downloading
        # the result ZIP *while* paused should still get a manifest/overview
        # covering whatever has completed so far, not nothing.
        _run_completion_hook(kind, job_id)
        return

    _run_completion_hook(kind, job_id)

    with _lock:
        job = _jobs[job_id]
        job['status'] = 'done'
        job['updated_at'] = time.time()
        _save_locked(job)


def _run_completion_hook(kind, job_id):
    """Run `kind`'s registered completion hook (if any) against job_id's
    current, fully-settled case records -- shared by every _drive_job exit
    path (normal completion, and now also pause/stop settling onto a job
    that may still have 'pending' cases -- see register_completion_hook's
    docstring on what a hook is for). No worker thread is running at any of
    these call sites (_run_worker_pool has already joined every one of
    them), so reading the job's case records here needs no lock of its own
    -- nothing else writes to them concurrently."""
    hook = _COMPLETION_HOOKS.get(kind)
    if hook is None:
        return
    try:
        hook(_jobs[job_id])
    except Exception:  # noqa: BLE001 - a hook bug must not wedge the job at 'running'
        logger.exception("Completion hook for batch job %s (%s) failed", job_id, kind)


def _set_case_status_locked(job_id, case_id, status, error=None):
    job = _jobs[job_id]
    case = job['cases'][case_id]
    case['status'] = status
    case['error'] = error
    job['updated_at'] = time.time()
    _save_locked(job)


def update_case(job_id, case_id, **fields):
    """Merge `fields` into a case's record and persist -- for a runner to
    report fine-grained progress beyond the coarse status/error
    _run_worker_pool itself sets before/after calling it (e.g. the
    multi-tool runner calling this after each tool finishes, so a case
    that's still 'running' already shows which of its selected tools are
    done/failed/pending, and a container restart mid-case doesn't lose that
    detail either). Safe to call from inside a runner: distinct worker slots
    only ever touch their own case_id, so the only contention this guards
    against is a concurrent status read (GET /batch/status/<job_id>)."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None or case_id not in job['cases']:
            return
        job['cases'][case_id].update(fields)
        job['updated_at'] = time.time()
        _save_locked(job)


def halt_requested(job_id):
    """Whether pause/stop has been requested for job_id -- for a runner with
    several sub-steps per case to check between them and return 'pending'
    (see register_runner), so pausing doesn't wait for the whole case."""
    with _lock:
        job = _jobs.get(job_id)
        return job is not None and (job['pause_requested'] or job['stop_requested'])


def _run_worker_pool(job_id, case_ids, runner):
    """Run `case_ids` through min(CONCURRENCY, len(case_ids)) worker threads,
    each pulling from a shared queue and pinned to one GPU index for its
    whole lifetime (slot_index % NUM_GPUS) -- so N concurrent containers
    spread across GPUs instead of all defaulting to the same one."""
    if not case_ids:
        return

    work_queue = queue.Queue()
    for case_id in case_ids:
        work_queue.put(case_id)

    def worker(slot_index):
        # Worker threads don't inherit the driver's log routing, so each slot
        # routes to job.log itself, plus the case's own log while it runs one.
        with job_logging.log_to(job_logging.batch_job_log_path(job_id)):
            _worker_loop(slot_index)

    def _worker_loop(slot_index):
        gpu_index = slot_index % NUM_GPUS
        while True:
            with _lock:
                # A case already in flight always runs to completion (see
                # pause_job's docstring: nothing here can safely interrupt a
                # blocking subprocess/container call mid-case) -- this only
                # stops a slot from picking up its *next* case once asked to.
                if _jobs[job_id]['pause_requested'] or _jobs[job_id]['stop_requested']:
                    logger.info("Slot %d: pause/stop requested, not taking another case", slot_index)
                    return
            try:
                case_id = work_queue.get_nowait()
            except queue.Empty:
                return

            with job_logging.log_to(job_logging.batch_case_log_path(job_id, case_id)):
                _run_one_case(case_id, gpu_index)

    def _run_one_case(case_id, gpu_index):
        with _lock:
            _set_case_status_locked(job_id, case_id, 'running')
            job = _jobs[job_id]

        logger.info("===== Case %s: started on GPU %s =====", case_id, gpu_index)
        started = time.monotonic()
        try:
            outcome = runner(job, case_id, gpu_index)
            # See register_runner()'s docstring for the accepted return
            # shapes -- plain success (None) is by far the common case,
            # so it's checked first rather than paying isinstance calls
            # for it.
            if outcome is None:
                status, error = 'done', None
            elif isinstance(outcome, tuple):
                status, error = outcome
            elif outcome in ('done', 'partial', 'failed', 'pending'):
                status, error = outcome, None
            else:
                status, error = 'done', None
        except Exception as e:  # noqa: BLE001 - record any failure and keep the pool going
            logger.exception("Case %s crashed", case_id)
            status, error = 'failed', str(e) or e.__class__.__name__

        logger.log(logging.INFO if status in ('done', 'pending') else logging.WARNING,
                   "===== Case %s: %s after %.1f s%s =====", case_id, status,
                   time.monotonic() - started, f" ({error})" if error else "")
        with _lock:
            _set_case_status_locked(job_id, case_id, status, error=error)

    num_workers = min(CONCURRENCY, len(case_ids))
    threads = [
        threading.Thread(target=worker, args=(slot,), name=f'batch-{job_id}-slot{slot}', daemon=True)
        for slot in range(num_workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def pause_job(job_id):
    """Request a running job pause once its in-flight cases finish (the
    GUI's per-job pause button).

    Nothing here can safely interrupt a case already handed to a runner --
    most of them block on a subprocess/container call with no cancellation
    hook (see this module's docstring on register_runner) -- so pausing only
    stops each worker slot from picking up its *next* case, and a runner
    that checks halt_requested() between its own sub-steps (e.g. one tool of
    a multi-tool case) from starting its next one. The job sits in
    'pausing' until every slot notices (worst case: however long the slowest
    in-flight sub-step takes) and _drive_job settles it into 'paused'.

    Returns 'pausing' (request accepted), 'not_found', or 'invalid' (job
    isn't 'running' -- there is nothing to pause)."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return "not_found"
        if job['status'] != 'running':
            return "invalid"
        job['pause_requested'] = True
        job['status'] = 'pausing'
        job['updated_at'] = time.time()
        _save_locked(job)
    return "pausing"


def resume_job(job_id):
    """Resume a paused job: clears the pause request and relaunches the
    worker pool over every case not already 'done' (same set _drive_job
    would pick up after a container restart -- see load_jobs_from_disk --
    so a case left 'failed' when the job was paused gets retried too).

    Returns 'resumed', 'not_found', or 'invalid' (job isn't 'paused')."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return "not_found"
        if job['status'] != 'paused':
            return "invalid"
        job['pause_requested'] = False
        job['status'] = 'running'
        job['updated_at'] = time.time()
        _save_locked(job)
    _launch(job_id)
    return "resumed"


def stop_job(job_id):
    """Request a job stop for good (the GUI's per-job stop button, distinct
    from delete_job: this ends the run but keeps its record/results for
    whatever did complete). Like pause_job, an in-flight case is left to
    finish rather than interrupted -- only cases that hadn't started yet are
    affected, and they're marked 'stopped' rather than left 'pending' so the
    per-case table doesn't imply they're still queued.

    A 'paused' job has no worker threads left to notice the request (its
    pool already drained, and no _drive_job thread is left running either --
    it already returned once the job settled into 'paused'), so this settles
    it to 'stopped' -- including running its kind's completion hook, same as
    _drive_job's own settle paths -- immediately instead of waiting for
    anything else to do it. A 'pending'/'running'/'pausing' job instead just
    records the request here; _drive_job's still-live worker pool notices it
    and settles (and runs the hook) itself, same as pause_job.

    Returns 'stopped' (already settled), 'stopping' (in flight, request
    accepted), 'not_found', or 'invalid' (job already 'stopped'/'done')."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return "not_found"
        status = job['status']
        if status not in ('pending', 'running', 'pausing', 'paused'):
            return "invalid"
        job['stop_requested'] = True
        if status == 'paused':
            for case in job['cases'].values():
                if case['status'] == 'pending':
                    case['status'] = 'stopped'
            job['status'] = 'stopped'
            outcome = "stopped"
        else:
            job['status'] = 'stopping'
            outcome = "stopping"
        job['updated_at'] = time.time()
        _save_locked(job)
        kind = job['kind']
    if outcome == "stopped":
        _run_completion_hook(kind, job_id)
    return outcome


def get_batch_job_status(job_id):
    """Return the status shape for a batch job, or None if unknown.

    {total, completed, failed, partial, running, pending,
     created_at, updated_at, per_case: [{id, status, error, tools, study_date}]}

    `created_at`/`updated_at` (unix seconds, same clock as list_jobs) let the
    GUI show how long a job has been running and, once it reaches a terminal
    status ('done'/'stopped'), how long it took in total -- updated_at is the
    timestamp of that last transition -- without a second round trip to
    GET /batch/jobs for the one job it's already polling.

    `tools` (present when a runner reports it -- see the multi-tool
    batches, batch_jobs.update_case) is {entry_id: {status, error,
    output_files, ...}} for a case that runs more than one (tool, mode)
    entry -- entry_id and any other keys in that inner dict are entirely up
    to the runner (e.g. routes.py's _run_batch_case also stores 'tool'/
    'mode' there, since one tool can appear under several entry_ids -- run
    with different modes, such as several BraTS algorithms -- and the GUI
    needs a way to tell them apart). A case's own `status` is 'partial'
    rather than 'done'/'failed' when some of its entries succeeded and some
    didn't. `study_date` (routes.py's batch start route, from
    batch_io.compute_study_dates) is the study's DICOM StudyDate as an ISO
    string, or None for a NIfTI-only or otherwise undated study. Internal
    per-case bookkeeping a runner may have stashed (e.g.
    tool_inputs' scratch work_dir paths) is deliberately
    not included here -- same reasoning as `owner` elsewhere in this module:
    this is what a caller (the GUI) displays, not the caller's job to filter.
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        job_id_, kind, status = job['id'], job['kind'], job['status']
        owner, params = job.get('owner'), job['params']
        created_at, updated_at = job['created_at'], job['updated_at']
        cases = list(job['cases'].values())

    total = len(cases)
    completed = sum(1 for c in cases if c['status'] == 'done')
    failed = sum(1 for c in cases if c['status'] == 'failed')
    partial = sum(1 for c in cases if c['status'] == 'partial')
    running = sum(1 for c in cases if c['status'] == 'running')
    # A case never started before a stop settled the job (see stop_job) is
    # its own terminal status, not 'pending' -- it must be subtracted out
    # here too, or it would silently reappear as "pending" work below.
    stopped = sum(1 for c in cases if c['status'] == 'stopped')
    return {
        "job_id": job_id_,
        "kind": kind,
        # pending | running | pausing | paused | stopping | stopped | done
        "status": status,
        "owner": owner,
        "params": params,
        "created_at": created_at,
        "updated_at": updated_at,
        "total": total,
        "completed": completed,
        "failed": failed,
        "partial": partial,
        "running": running,
        "stopped": stopped,
        "pending": total - completed - failed - partial - running - stopped,
        "per_case": [
            {
                "id": c['id'], "status": c['status'], "error": c['error'],
                "tools": c.get('tools'), "study_date": c.get('study_date'),
            }
            for c in cases
        ],
    }


def batch_job_status_response(job_id):
    """Build the poll response for GET /batch/status/<job_id>."""
    status = get_batch_job_status(job_id)
    if status is None:
        return cors_json({
            "job_id": job_id,
            "status": "not_found",
            "error": "Unknown batch job id (it may never have existed, or its job file was removed).",
        }, status=404)
    return cors_json(status)


def delete_job(job_id):
    """Drop a job's record and its on-disk state (job file + work_dir), for
    the GUI's "clear history" / per-job delete buttons. Owner-checking is
    the caller's job (see push_to_irods's docstring in the GUI blueprint for
    why: this API has no auth of its own, so a caller that *does* know who's
    asking - the GUI - has to gate this before calling it).

    Refuses to delete a job with any worker thread still able to touch it --
    'pending'/'running'/'pausing'/'stopping' -- since those threads look the
    job back up by id under _lock on every case completion
    (_set_case_status_locked), so pulling it out of _jobs out from under them
    would KeyError a worker thread, and rmtree-ing work_dir while a tool
    container is still writing into it would corrupt that run for no
    benefit. 'paused' and 'stopped' are safe to delete despite not being
    'done': both mean the worker pool has already fully drained (see
    pause_job/stop_job), same as a finished job, just with cases left
    incomplete.

    Returns "deleted", "running" (found but still in flight, left alone),
    or "not_found".
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return "not_found"
        if job['status'] not in ('done', 'paused', 'stopped'):
            return "running"
        del _jobs[job_id]
    try:
        os.remove(_job_path(job_id))
    except OSError:
        pass
    shutil.rmtree(job['work_dir'], ignore_errors=True)
    return "deleted"


def list_jobs(owner=None):
    """Summary list of jobs (no per-case detail -- see get_batch_job_status()
    for that), newest first. Filtered to `owner` when given.

    This is the same bookkeeping-not-access-control caveat as start_batch_job's
    `owner` param: it lets a trusted caller (the GUI) show "your jobs" without
    a second per-user store, but doesn't itself restrict who can call
    get_batch_job_status()/download a result for a job_id obtained elsewhere.
    """
    with _lock:
        jobs = list(_jobs.values())
    if owner is not None:
        jobs = [j for j in jobs if j.get('owner') == owner]
    jobs.sort(key=lambda j: j['created_at'], reverse=True)
    return [
        {
            "job_id": j['id'],
            "kind": j['kind'],
            "status": j['status'],
            "params": j['params'],
            "created_at": j['created_at'],
            "updated_at": j['updated_at'],
            "total": len(j['cases']),
            "completed": sum(1 for c in j['cases'].values() if c['status'] == 'done'),
            "failed": sum(1 for c in j['cases'].values() if c['status'] == 'failed'),
            "partial": sum(1 for c in j['cases'].values() if c['status'] == 'partial'),
            "running": sum(1 for c in j['cases'].values() if c['status'] == 'running'),
        }
        for j in jobs
    ]


def batch_jobs_list_response(owner=None):
    """Build the response for GET /batch/jobs."""
    return cors_json({"jobs": list_jobs(owner=owner)})


def _load_all_job_files():
    if not os.path.isdir(BATCH_JOBS_ROOT):
        return []
    jobs = []
    for name in sorted(os.listdir(BATCH_JOBS_ROOT)):
        if not name.endswith('.json'):
            continue
        path = os.path.join(BATCH_JOBS_ROOT, name)
        try:
            with open(path) as f:
                jobs.append(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"batch_jobs: skipping unreadable job file {path}: {e}")
    return jobs


def load_jobs_from_disk():
    """Call once at API startup (after every register_runner() call has run,
    i.e. after routes.py -- and any other route module defining batch jobs --
    has been imported).

    Reloads every persisted job into the in-memory registry so status queries
    work immediately after a restart, and relaunches the worker pool for any
    job still 'pending'/'running' -- meaning the process stopped mid-batch --
    over every case not already 'done'. A 'running' case is reset to
    'pending' first, since its container died along with the process that
    started it and won't finish/update its own status on its own.

    A job caught mid-pause/-stop ('pausing'/'stopping') when the process
    stopped is settled the same way stop_job/the post-pool half of
    _drive_job would, but without relaunching a worker pool for it -- the
    person who asked to pause/stop it should not have that request silently
    turn back into "running" just because the process happened to restart
    first.
    """
    for job in _load_all_job_files():
        # Jobs persisted before pause/resume/stop existed carry neither flag.
        job.setdefault('pause_requested', False)
        job.setdefault('stop_requested', False)
        with _lock:
            _jobs[job['id']] = job

        if job['status'] in ('pausing', 'stopping'):
            for case in job['cases'].values():
                if case['status'] == 'running':
                    case['status'] = 'pending'
                    case['error'] = None
                if job['status'] == 'stopping' and case['status'] == 'pending':
                    case['status'] = 'stopped'
            job['status'] = 'stopped' if job['status'] == 'stopping' else 'paused'
            with _lock:
                _save_locked(job)
            continue

        if job['status'] not in ('pending', 'running'):
            continue
        if job['kind'] not in _RUNNERS:
            logger.warning(f"batch_jobs: no runner registered for job {job['id']} (kind={job['kind']}); "
                  f"leaving it as-is until one is.")
            continue

        remaining = 0
        for case in job['cases'].values():
            if case['status'] == 'running':
                case['status'] = 'pending'
                case['error'] = None
            if case['status'] != 'done':
                remaining += 1
        with _lock:
            _save_locked(job)

        logger.info(f"batch_jobs: resuming job {job['id']} ({job['kind']}) with {remaining} case(s) left")
        _launch(job['id'])
