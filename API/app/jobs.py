"""In-process background job registry.

The segmentation endpoints kick off a multi-minute pipeline. Rather than hold
the HTTP connection open for the whole run (which risks browser/proxy timeouts
and delays the frontend toast), the route starts the work in a background
thread and returns a job id immediately. The frontend then polls a status
endpoint and shows a green/red toast when the job reaches 'done'/'failed'.

This registry is intentionally simple: an in-memory dict guarded by a lock.
That is correct because the app runs as a single process (run.py ->
app.run(...)). If this is ever served by multiple workers (gunicorn -w >1),
move this state into a shared store (Redis, a DB, or a file) so every worker
sees the same jobs.

Every job logs to its own file under API_storage/logs/jobs/ (see
job_logging.py); unlike this registry, that file outlives the process.
"""

import json
import logging
import math
import threading
import time
import uuid

from flask import has_request_context, jsonify, request, send_file

from . import job_logging

logger = logging.getLogger('interact.jobs')

# job_id -> job dict. Guarded by _lock for all reads/writes.
_jobs = {}
_lock = threading.Lock()

# Completed jobs are kept this long so a slow poller can still read the result,
# then pruned to keep memory bounded.
_RETAIN_SECONDS = 60 * 60  # 1 hour

# Request bodies are recorded in the job's log meta for debugging; cap them so
# an unexpectedly large payload can't bloat every sidecar.
_MAX_BODY_CHARS = 5000


def _prune_locked():
    """Drop finished jobs older than the retention window. Caller holds _lock."""
    now = time.time()
    stale = [
        jid for jid, job in _jobs.items()
        if job["status"] in ("done", "failed")
        and now - job["updated_at"] > _RETAIN_SECONDS
    ]
    for jid in stale:
        del _jobs[jid]


def create_job(kind):
    """Register a new job in the 'pending' state and return its id."""
    job_id = uuid.uuid4().hex
    now = time.time()
    with _lock:
        _prune_locked()
        _jobs[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "pending",   # pending -> running -> done | failed
            "message": None,       # human-readable success text
            "error": None,         # human-readable failure text
            "log_file": job_logging.new_job_log_path(kind, job_id),  # relative to API_storage/logs
            "created_at": now,
            "updated_at": now,
        }
    return job_id


def _set_status(job_id, status, message=None, error=None):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["status"] = status
        if message is not None:
            job["message"] = message
        if error is not None:
            job["error"] = error
        job["updated_at"] = time.time()


def get_job(job_id):
    """Return a copy of the job dict, or None if unknown/expired."""
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def run_in_background(job_id, target, *args, log_meta=None, **kwargs):
    """Run target(*args, **kwargs) in a daemon thread, recording the outcome.

    target should return a success message string (or None). Any exception is
    captured into the job's 'error' field so the frontend can display it; the
    full traceback goes to the job's log file. `log_meta` is extra context
    (label, study, request) recorded alongside that file for the GUI's Job
    Logs page.
    """
    job = get_job(job_id)
    meta = {"job_id": job_id, "kind": job["kind"], **(log_meta or {})}

    def _runner():
        with job_logging.job_log(job["log_file"], **meta) as job_log:
            logger.info("Job %s (%s) started%s", job_id, job["kind"],
                        f": {meta['label']}" if meta.get("label") else "")
            if meta.get("route"):
                logger.info("Request: %s %s", meta.get("method"), meta["route"])
            if meta.get("query") or meta.get("request_body") is not None:
                logger.debug("Request parameters: query=%s body=%s",
                             meta.get("query"), meta.get("request_body"))
            _set_status(job_id, "running")
            try:
                message = target(*args, **kwargs) or "Completed."
                _set_status(job_id, "done", message=message)
                job_log.finish("done", message=message)
                logger.info("Job %s done: %s", job_id, message)
            except Exception as e:  # noqa: BLE001 - we want to surface any failure
                error = str(e) or e.__class__.__name__
                logger.exception("Job %s failed: %s", job_id, error)
                _set_status(job_id, "failed", error=error)
                job_log.finish("failed", error=error)

    thread = threading.Thread(target=_runner, name=f"job-{job_id}", daemon=True)
    thread.start()


# --- HTTP helpers for the background-job endpoints --------------------------
#
# These build the JSON responses the segmentation-style routes share, so a new
# AI tool only has to describe its work -- not re-wire jobs/threading/CORS.

def _json_safe(value):
    """`value` with every NaN/Infinity float replaced by None.

    Tools report a deliberately undefined stat as NaN (e.g. the Asymmetry_%
    of a region that is 0 mm3 on both sides, see fastsurfer_functions.
    _asymmetry_percent), and Python's JSON encoder writes that as a bare
    `NaN` -- not valid JSON, so the browser's resp.json() rejects the whole
    response and the batch page silently shows the job as all zeros."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def cors_json(payload, status=200):
    """JSON response with the CORS header the cross-origin web app needs."""
    resp = jsonify(_json_safe(payload))
    resp.status_code = status
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


def cors_file(file, download_name, mimetype):
    """send_file as an attachment, with the CORS + Content-Disposition headers
    the cross-origin web app needs to read the filename."""
    resp = send_file(file, as_attachment=True, download_name=download_name, mimetype=mimetype)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
    return resp


def logged_work(label, fn):
    """Wrap a worker so start/finish are logged with their duration.

    `label` is a short human string (e.g. "hdglio segmentation for study X");
    `fn` is a zero-arg callable returning the success message. The label is
    also kept on the returned callable, for start_bg_job's log meta.
    """
    def work():
        with job_logging.timed(label, log=logger):
            return fn()
    work.label = label
    return work


def _request_meta():
    """What the current request asked for, for the job's log meta. Every
    per-study route names its study parameter studyInstanceUID."""
    if not has_request_context():
        return {}
    view_args = request.view_args or {}
    meta = {
        "route": request.path,
        "method": request.method,
        "study_uid": view_args.get("studyInstanceUID"),
    }
    if request.args:
        meta["query"] = request.args.to_dict()
    body = request.get_json(silent=True)
    if body is not None:
        text = json.dumps(body, default=str)
        meta["request_body"] = body if len(text) <= _MAX_BODY_CHARS else text[:_MAX_BODY_CHARS] + "..."
    return meta


def start_bg_job(kind, work, message):
    """Boilerplate for a background-job endpoint.

    Registers a job, launches `work` in a background thread, and returns a
    202 Accepted with the job id and the shared status_url the frontend polls.

    To add a new AI tool: write a route that defines a `work()` callable
    (returning a success-message string) and hand it to this helper -- no need
    to duplicate the job-registry / threading / CORS / logging wiring.
    """
    job_id = create_job(kind=kind)
    run_in_background(job_id, work, log_meta={"label": getattr(work, "label", None), **_request_meta()})
    return cors_json({
        "job_id": job_id,
        "status": "pending",
        "message": message,
        "status_url": f"/job_status/{job_id}",
        "log_file": get_job(job_id)["log_file"],
    }, status=202)


def job_status_response(job_id):
    """Build the poll response for any background job.

    Always 200 when the job is known (the frontend derives the toast from the
    'status' field); unknown/expired ids return 404.
    """
    job = get_job(job_id)
    if job is None:
        return cors_json({
            "job_id": job_id,
            "status": "not_found",
            "error": "Unknown job id (it may have expired, or the server restarted).",
        }, status=404)

    return cors_json({
        "job_id": job["id"],
        "status": job["status"],   # pending | running | done | failed
        "message": job["message"],
        "error": job["error"],
        "log_file": job["log_file"],
    })
