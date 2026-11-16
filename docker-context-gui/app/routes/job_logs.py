# app/routes/job_logs.py
"""
Admin view onto the processing API's job logs (see app/utils/job_logs.py for
where they live and API/app/job_logging.py for what writes them).

Every single-study job and batch case gets its own log file there, so "why
did this segmentation fail" no longer means asking someone with shell access
to dig through `docker logs`. Admin only: logs name studies and patients
(batch case ids are patient/study). Opening or downloading one is recorded
in the PHI access trail; the live tail's polls are not, since they only
continue a view that already was.
"""
from datetime import date

from flask import Blueprint, abort, jsonify, render_template, request, send_file

from app.utils import audit, job_logs
from app.utils.roles import admin_required

job_logs_bp = Blueprint("job_logs", __name__, url_prefix="/job-logs")

PAGE_LIMIT = 500
STATUSES = ("running", "done", "failed", "pending", "paused", "stopped", "deleted")


def _date_arg(name):
    try:
        return date.fromisoformat(request.args.get(name, ""))
    except ValueError:
        return None


def _resolve_or_404(rel):
    path = job_logs.resolve(rel)
    if path is None:
        abort(404)
    return path


def _audit_detail(entry):
    if entry["type"] == "api":
        return "opened the API server log"
    what = f"{entry.get('kind') or 'job'} {entry.get('job_id')}"
    if entry.get("case_id"):
        what += f", case {entry['case_id']}"
    return f"job log for {what}"


def _case_patient(entry):
    """Batch case ids are <patient_id>/<study_name>."""
    case_id = entry.get("case_id") or ""
    return case_id.split("/", 1)[0] if "/" in case_id else None


@job_logs_bp.route("/")
@admin_required
def index():
    filters = {
        "type": request.args.get("type") or None,
        "kind": request.args.get("kind") or None,
        "status": request.args.get("status") or None,
        "since": _date_arg("since"),
        "until": _date_arg("until"),
        "contains": (request.args.get("q") or "").strip() or None,
    }
    entries, total, kinds = job_logs.list_jobs(
        job_type=filters["type"], kind=filters["kind"], status=filters["status"],
        since=filters["since"], until=filters["until"], contains=filters["contains"],
        limit=PAGE_LIMIT,
    )

    audit.record_request(
        "api", "list", object_type="job_log", count=len(entries),
        detail=f"listed job logs ({total} matching)",
    )

    return render_template(
        "job_logs.html",
        title="Job Logs",
        entries=entries,
        total=total,
        shown=len(entries),
        truncated=total > len(entries),
        kinds=kinds,
        statuses=STATUSES,
        filters=filters,
        available=job_logs.available(),
        log_dir=str(job_logs.LOG_DIR),
        api_logs=job_logs.api_logs(),
        embedded=request.args.get("embedded") or None,
    )


@job_logs_bp.route("/view/<path:rel>")
@admin_required
def view(rel):
    path = _resolve_or_404(rel)
    entry = job_logs.describe(rel)
    chunk = job_logs.read_chunk(path)

    audit.record(
        "api", "view", object=rel, object_type="job_log",
        study_uid=entry.get("study_uid"), patient_id=_case_patient(entry),
        detail=_audit_detail(entry),
    )

    return render_template(
        "job_log_view.html",
        title="Job Log",
        rel=rel,
        entry=entry,
        chunk=chunk,
        active=job_logs.is_active(entry),
        embedded=request.args.get("embedded") or None,
    )


@job_logs_bp.route("/tail/<path:rel>")
@admin_required
def tail(rel):
    path = _resolve_or_404(rel)
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        offset = 0
    chunk = job_logs.read_chunk(path, offset)
    entry = job_logs.describe(rel)
    case_status = (entry.get("cases", {}).get(entry.get("case_id")) or {}).get("status") if entry.get("case_id") else None
    return jsonify({
        **chunk,
        "status": entry.get("status"),
        "case_status": case_status,
        "active": job_logs.is_active(entry),
    })


@job_logs_bp.route("/download/<path:rel>")
@admin_required
def download(rel):
    path = _resolve_or_404(rel)
    entry = job_logs.describe(rel)
    audit.record(
        "api", "download", object=rel, object_type="job_log",
        study_uid=entry.get("study_uid"), patient_id=_case_patient(entry),
        bytes=entry.get("size"), detail=_audit_detail(entry),
    )
    return send_file(path, as_attachment=True, download_name=path.name, mimetype="text/plain")
