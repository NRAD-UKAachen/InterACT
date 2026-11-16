# app/routes/audit_log.py
"""
Admin view onto the PHI access trail written by app/utils/audit.py.

An audit trail nobody can read is a compliance artefact, not a control. The
questions it has to answer in practice are "who opened this patient's study",
"what did this person access last month" and "give me a record I can hand to a
regulator", so this page filters by actor, subsystem, action and free text, and
exports the filtered set as CSV.

Read-only by design: there is no route here that edits or deletes a record.
Rotation and retention are the log module's business (AUDIT_RETENTION_DAYS), so
a web session can never shorten the trail.

Viewing the trail is itself audited, and gated: an admin has to state why
before any record is shown, and that reason is written into the trail beside
the access. Access records name patients and name colleagues, so reading them
is a privileged act in its own right - "who has been looking at who has been
looking" is a question a data protection officer will eventually ask, and
without the reason the answer is a list of timestamps that explains nothing.
"""
import os
from datetime import date, datetime, timedelta, timezone

from flask import (
    Blueprint, Response, flash, redirect, render_template, request, session,
    url_for,
)

from app.utils import audit
from app.utils.roles import admin_required

audit_bp = Blueprint("audit_log", __name__, url_prefix="/audit")

# How long a stated reason covers before it is asked for again. Long enough
# that filtering, paging and exporting during one investigation is not an
# interrogation; short enough that an unattended session does not leave the
# trail readable all afternoon under a reason given at 09:00.
REASON_TTL_MINUTES = int(os.environ.get("AUDIT_REASON_TTL_MINUTES", "30"))

# Enough to be a sentence, not a keystroke. "x", "test" and "." are what a
# free-text box collects when nothing stops them, and a trail full of those
# records the prompt rather than the purpose.
MIN_REASON_LENGTH = 10

_SESSION_KEY = "audit_access_reason"


def _active_reason():
    """The reason in force for this session, or None if absent or expired."""
    stored = session.get(_SESSION_KEY)
    if not isinstance(stored, dict):
        return None
    try:
        granted = datetime.fromisoformat(stored["at"])
    except (KeyError, TypeError, ValueError):
        return None
    if datetime.now(timezone.utc) - granted > timedelta(minutes=REASON_TTL_MINUTES):
        return None
    return stored


def _reason_expires_in(stored):
    granted = datetime.fromisoformat(stored["at"])
    remaining = timedelta(minutes=REASON_TTL_MINUTES) - (
        datetime.now(timezone.utc) - granted
    )
    return max(0, int(remaining.total_seconds() // 60))

# Kept in sync with the vocabulary app/utils/audit.py records, so the filter
# dropdowns offer what the log actually contains rather than a guess.
SYSTEMS = ["dcm4chee", "irods", "api", "audit"]
ACTIONS = [
    "view", "search", "list", "download", "export", "upload", "modify",
    "delete", "permission_change", "account_admin",
]

# Enough to scroll through a busy day without turning one page load into a
# multi-megabyte HTML table. The unfiltered total is shown alongside so the
# cap is visible rather than silently misleading.
PAGE_LIMIT = 1000


def _parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _filters():
    """The filter set shared by the page and its CSV export."""
    return {
        "actor": (request.args.get("actor") or "").strip() or None,
        "system": (request.args.get("system") or "").strip() or None,
        "action": (request.args.get("action") or "").strip() or None,
        "contains": (request.args.get("q") or "").strip() or None,
        "since": _parse_date(request.args.get("since")),
        "until": _parse_date(request.args.get("until")),
    }


def _days(filters):
    """How many day files to scan. An explicit date range overrides the
    rolling window, so "everything in March" doesn't come back empty just
    because March is more than `days` ago."""
    if filters["since"] or filters["until"]:
        return None
    try:
        return max(1, min(int(request.args.get("days", 7)), 3650))
    except (TypeError, ValueError):
        return 7


def _ask_for_reason(next_url):
    """The gate page. Rendered instead of any records."""
    return render_template(
        "audit_reason.html",
        title="PHI Access Log",
        next_url=next_url,
        min_length=MIN_REASON_LENGTH,
        ttl_minutes=REASON_TTL_MINUTES,
        embedded=request.args.get("embedded") or None,
    )


@audit_bp.route("/reason", methods=["POST"])
@admin_required
def state_reason():
    """Record why this person is about to read the trail, and let them in.

    The reason is written to the trail *here*, before anything is displayed,
    so a stated reason exists even if the person closes the tab immediately
    afterwards.
    """
    reason = " ".join((request.form.get("reason") or "").split())
    # Relative and same-blueprint only: this comes back as a form field, and
    # an absolute URL here would make the gate an open redirect.
    next_url = request.form.get("next") or url_for("audit_log.index")
    if not next_url.startswith("/audit"):
        next_url = url_for("audit_log.index")

    if len(reason) < MIN_REASON_LENGTH:
        audit.record(
            "audit", "view", object_type="audit_log", outcome="denied",
            detail="attempted to open the access trail without stating a reason",
        )
        flash(
            f"Enter a reason of at least {MIN_REASON_LENGTH} characters — it is "
            "written into the audit trail alongside your access.",
            "error",
        )
        return _ask_for_reason(next_url)

    session[_SESSION_KEY] = {
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    }

    audit.record(
        "audit", "view", object_type="audit_log", reason=reason,
        detail=f"opened the access trail; reason valid for {REASON_TTL_MINUTES} min",
    )
    return redirect(next_url)


@audit_bp.route("/end-session", methods=["POST"])
@admin_required
def end_session():
    """Drop the stated reason, so the next view has to give a fresh one."""
    session.pop(_SESSION_KEY, None)
    audit.record(
        "audit", "view", object_type="audit_log",
        detail="closed the access trail session",
    )
    flash("Audit log session ended.", "success")
    embedded = request.form.get("embedded") or None
    return redirect(url_for("audit_log.index", embedded=embedded))


@audit_bp.route("/")
@admin_required
def index():
    stored = _active_reason()
    if stored is None:
        return _ask_for_reason(request.full_path.rstrip("?"))

    filters = _filters()
    days = _days(filters)

    events, total = audit.read_events(days=days, limit=PAGE_LIMIT, **filters)

    audit.record(
        "audit", "view", object_type="audit_log", count=len(events),
        reason=stored["reason"],
        detail=f"reviewed the access trail ({total} matching records)",
    )

    actors = sorted({event.get("actor") for event in events if event.get("actor")})

    return render_template(
        "audit_log.html",
        title="PHI Access Log",
        events=events,
        total=total,
        shown=len(events),
        truncated=total > len(events),
        actors=actors,
        systems=SYSTEMS,
        actions=ACTIONS,
        days=days,
        filters=filters,
        available_days=audit.available_days(),
        log_dir=str(audit.AUDIT_LOG_DIR),
        retention_days=audit.RETENTION_DAYS,
        # Surfaced on the page: if the nginx trail is not mounted, the table
        # is missing every study opened in OHIF, and that has to be visible
        # rather than looking like nobody viewed anything.
        nginx_log=str(audit.NGINX_AUDIT_LOG),
        nginx_available=audit.NGINX_AUDIT_LOG.exists(),
        collapse_minutes=audit.NGINX_COLLAPSE_MINUTES,
        access_reason=stored["reason"],
        reason_expires_in=_reason_expires_in(stored),
        embedded=request.args.get("embedded") or None,
    )


@audit_bp.route("/export.csv")
@admin_required
def export_csv():
    """The filtered trail as CSV.

    Exports up to 100k rows rather than PAGE_LIMIT: this is the artefact that
    gets handed over for an Art. 15 request or a data-protection review, and
    truncating it to a screenful would make it wrong in a way the recipient
    could not see.
    """
    stored = _active_reason()
    if stored is None:
        # Taking a copy of the trail off the platform without a stated reason
        # is exactly what the gate is for, so this is not a route that gets
        # to skip it for convenience.
        return _ask_for_reason(url_for("audit_log.index"))

    filters = _filters()
    events, total = audit.read_events(days=_days(filters), limit=100_000, **filters)

    audit.record(
        "audit", "export", object_type="audit_log", count=len(events),
        reason=stored["reason"],
        detail=f"exported the access trail as CSV ({total} matching records)",
    )

    stamp = date.today().isoformat()
    return Response(
        audit.to_csv(events),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="phi-access-{stamp}.csv"'
        },
    )
