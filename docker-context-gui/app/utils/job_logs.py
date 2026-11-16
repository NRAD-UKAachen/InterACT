# app/utils/job_logs.py
"""
Read side of the processing API's job logs, for the admin Job Logs page.

The API (API/app/job_logging.py) writes them under ${API_STORAGE_DIR}/logs/,
which this container mounts at the same path:

  api.log[.N]                                   every API log line, rotated
  jobs/<YYYY-MM-DD>/<HHMMSS>_<kind>_<id>.log    one per single-study job
  jobs/<YYYY-MM-DD>/<...>.meta.json             kind/label/study/status
  batch/<job_id>/job.log, cases/<case>.log      one per batch job / case
  batch/<job_id>/meta.json

Reading straight off the mount rather than through the API keeps these logs
out of the no-auth API entirely -- the only way to them is an admin session
here. Read-only: nothing in this module writes or deletes; retention is the
API's business (LOG_RETENTION_DAYS).
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

_STORAGE = (os.environ.get("API_STORAGE_DIR") or "").rstrip("/")
LOG_DIR = Path(_STORAGE) / "logs" if _STORAGE else None
BATCH_JOBS_ROOT = Path(_STORAGE) / "batch_jobs" if _STORAGE else None

# Batch job statuses that can still produce more log output.
ACTIVE_BATCH_STATUSES = {"pending", "running", "pausing", "paused", "stopping"}

# How much of a log the view page loads up front; the rest is a download.
INITIAL_BYTES = 1024 * 1024
# Per poll of the live tail.
TAIL_BYTES = 256 * 1024

_LOG_NAME = re.compile(r"\.log(\.\d+)?$")


def available():
    return LOG_DIR is not None and LOG_DIR.is_dir()


def resolve(rel):
    """Absolute path of a log file named relative to LOG_DIR, or None if it
    does not exist or would escape LOG_DIR (../, absolute paths, symlinks)."""
    if not available() or not rel:
        return None
    root = LOG_DIR.resolve()
    try:
        path = (root / rel).resolve()
    except (OSError, RuntimeError):
        return None
    if root not in path.parents or not _LOG_NAME.search(path.name) or not path.is_file():
        return None
    return path


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _iso_from_epoch(value):
    try:
        return datetime.fromtimestamp(float(value)).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def _stat(path):
    try:
        st = path.stat()
        return st.st_size, st.st_mtime
    except OSError:
        return None, None


def _single_entry(meta):
    rel = meta.get("log_file")
    size, mtime = _stat(LOG_DIR / rel) if rel else (None, None)
    return {
        "type": "single",
        "job_id": meta.get("job_id"),
        "kind": meta.get("kind"),
        "label": meta.get("label"),
        "study_uid": meta.get("study_uid"),
        "route": meta.get("route"),
        "status": meta.get("status"),
        "error": meta.get("error"),
        "message": meta.get("message"),
        "started_at": meta.get("started_at"),
        "finished_at": meta.get("finished_at"),
        "duration_s": meta.get("duration_s"),
        "log_file": rel,
        "size": size,
        "last_output": mtime,
        "owner": None,
        "tools": [],
        "cases": {},
    }


def _batch_entry(job_dir):
    job_id = job_dir.name
    meta = _read_json(job_dir / "meta.json") or {}
    live = _read_json(BATCH_JOBS_ROOT / f"{job_id}.json") if BATCH_JOBS_ROOT else None
    rel = meta.get("log_file") or f"batch/{job_id}/job.log"
    size, mtime = _stat(LOG_DIR / rel)

    # Case logs actually on disk, labelled with the real case id where the
    # meta can map the file name back to it.
    by_file = {Path(p).name: cid for cid, p in (meta.get("case_logs") or {}).items()}
    live_cases = (live or {}).get("cases") or {}
    cases = {}
    cases_dir = job_dir / "cases"
    if cases_dir.is_dir():
        for path in sorted(cases_dir.glob("*.log")):
            case_id = by_file.get(path.name, path.stem)
            live_case = live_cases.get(case_id) or {}
            case_size, _ = _stat(path)
            cases[case_id] = {
                "log_file": f"batch/{job_id}/cases/{path.name}",
                "status": live_case.get("status"),
                "error": live_case.get("error"),
                "size": case_size,
            }

    status = (live or {}).get("status") or meta.get("status")
    if live is None and status in ACTIVE_BATCH_STATUSES:
        # Job file deleted from the batch page: nothing more will be written.
        status = "deleted"

    return {
        "type": "batch",
        "job_id": job_id,
        "kind": meta.get("kind") or (live or {}).get("kind"),
        "label": ", ".join(meta.get("tools") or []) or None,
        "study_uid": None,
        "route": None,
        "status": status,
        "error": None,
        "message": None,
        "started_at": meta.get("started_at") or _iso_from_epoch((live or {}).get("created_at")),
        "finished_at": None,
        "duration_s": None,
        "log_file": rel,
        "size": size,
        "last_output": mtime,
        "owner": meta.get("owner") or (live or {}).get("owner"),
        "tools": meta.get("tools") or [],
        "cases": cases,
    }


def _started_date(entry):
    try:
        return datetime.fromisoformat(entry["started_at"]).date()
    except (TypeError, ValueError, KeyError):
        return None


def list_jobs(job_type=None, kind=None, status=None, since=None, until=None, contains=None, limit=500):
    """(entries, total, kinds): newest first, filtered, capped at `limit`.
    `kinds` covers every job before filtering, for the filter dropdown."""
    if not available():
        return [], 0, []

    entries = []
    jobs_dir = LOG_DIR / "jobs"
    if job_type in (None, "", "single") and jobs_dir.is_dir():
        for day_dir in jobs_dir.iterdir():
            try:
                day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if (since and day < since) or (until and day > until):
                continue
            for meta_path in day_dir.glob("*.meta.json"):
                meta = _read_json(meta_path)
                if meta and meta.get("log_file"):
                    entries.append(_single_entry(meta))

    batch_dir = LOG_DIR / "batch"
    if job_type in (None, "", "batch") and batch_dir.is_dir():
        for job_dir in batch_dir.iterdir():
            if job_dir.is_dir():
                entries.append(_batch_entry(job_dir))

    kinds = sorted({e["kind"] for e in entries if e["kind"]})

    needle = (contains or "").strip().lower()

    def keep(entry):
        if kind and entry["kind"] != kind:
            return False
        if status and entry["status"] != status:
            return False
        day = _started_date(entry)
        if day and ((since and day < since) or (until and day > until)):
            return False
        if needle:
            haystack = " ".join(str(v) for v in (
                entry["job_id"], entry["kind"], entry["label"], entry["study_uid"],
                entry["route"], entry["owner"], entry["error"], " ".join(entry["cases"]),
            ) if v).lower()
            if needle not in haystack:
                return False
        return True

    entries = [e for e in entries if keep(e)]
    entries.sort(key=lambda e: e["started_at"] or "", reverse=True)
    return entries[:limit], len(entries), kinds


def describe(rel):
    """The job a log file belongs to, as a list_jobs() entry (plus which part
    of it this file is), or a bare description for api.log."""
    parts = Path(rel).parts
    if parts and parts[0] == "jobs":
        meta = _read_json(LOG_DIR / (rel[: -len(".log")] + ".meta.json"))
        if meta:
            return {**_single_entry(meta), "part": "job"}
    elif len(parts) >= 3 and parts[0] == "batch":
        entry = _batch_entry(LOG_DIR / "batch" / parts[1])
        case_id = None
        if len(parts) == 4 and parts[2] == "cases":
            case_id = next((cid for cid, c in entry["cases"].items() if c["log_file"] == rel), Path(rel).stem)
        return {**entry, "part": "case" if case_id else "job", "case_id": case_id}
    size, mtime = _stat(LOG_DIR / rel)
    return {"type": "api", "job_id": None, "kind": None, "label": "API server log", "status": None,
            "log_file": rel, "size": size, "last_output": mtime, "cases": {}, "part": "api",
            "study_uid": None}


def is_active(entry):
    if entry["type"] == "single":
        return entry["status"] == "running"
    if entry["type"] == "batch":
        return entry["status"] in ACTIVE_BATCH_STATUSES
    return entry["type"] == "api"


def read_chunk(path, offset=None, max_bytes=TAIL_BYTES):
    """Text of `path` from byte `offset` (None: the last INITIAL_BYTES),
    ending on a line boundary so a half-written line is picked up whole by
    the next poll. Returns {text, offset, size, truncated}."""
    size = path.stat().st_size
    truncated = False
    if offset is None:
        max_bytes = INITIAL_BYTES
        offset = max(0, size - INITIAL_BYTES)
        truncated = offset > 0
    elif offset > size:  # rotated or rewritten since the last poll
        offset = 0

    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(max_bytes)

    if truncated:
        # Started mid-file: drop the partial first line.
        newline = data.find(b"\n")
        if newline != -1:
            offset += newline + 1
            data = data[newline + 1:]

    end = offset + len(data)
    if data and not data.endswith(b"\n"):
        last_newline = data.rfind(b"\n")
        if last_newline != -1:
            data = data[: last_newline + 1]
            end = offset + len(data)

    return {
        "text": data.decode("utf-8", errors="replace"),
        "offset": end,
        "size": size,
        "truncated": truncated,
    }


def api_logs():
    """api.log and its rotated siblings, newest first."""
    if not available():
        return []
    files = [p for p in LOG_DIR.glob("api.log*") if _LOG_NAME.search(p.name)]

    def order(p):
        suffix = p.name[len("api.log"):]
        return int(suffix[1:]) if suffix.startswith(".") and suffix[1:].isdigit() else 0

    return [{"log_file": p.name, "size": _stat(p)[0]} for p in sorted(files, key=order)]
