# app/utils/audit.py
"""
The PHI access audit trail: who viewed, searched, downloaded or edited what.

Everything that touches identifiable patient data through this app -- dcm4chee
studies and the iRODS zone alike -- records one line here. GDPR Art. 30 expects
a record of processing activities, and an access log that cannot name the person
behind a request does not provide one; that is why this lives in the GUI rather
than in dcm4chee or iRODS themselves. Neither of those services knows the
Keycloak identity: dcm4chee sits behind oauth2-proxy with no authentication of
its own, and the iRODS HTTP API only ever sees a bearer token. The Flask app is
the one place in the chain that holds the logged-in person's identity *and* sees
the object being accessed.

Format: one JSON object per line (JSON Lines), one file per UTC day, under
AUDIT_LOG_DIR. Append-only and never rewritten, so a compromised session can add
noise but cannot quietly remove its own trail. Chosen over a database because it
survives the app being down, is greppable from a shell during an incident, and
needs no schema migration when a new call site adds a field.

    {"ts": "2026-08-11T07:41:02.918Z", "actor": "jsmith", "actor_ip": "10.1.2.3",
     "system": "irods", "action": "download", "object_type": "data_object",
     "object": "/interactZone/home/jsmith/CT/1.2.840....dcm",
     "outcome": "success", "method": "GET", "path": "/irods/download/..."}

Deliberately NOT written to stdout: `docker logs` is unprotected and readable by
anyone in the docker group, and these lines carry patient identifiers by design
(see the warning in app/__init__.py about the old request-logging handler).

Recording must never break the operation being recorded -- every entry point
here swallows its own errors. An audit trail that can 500 a clinical request is
worse than one that occasionally misses a line, and the failure is reported once
to stderr rather than per call so a broken mount cannot flood the logs.
"""
import csv
import io
import json
import os
import re
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import has_request_context, g, request

AUDIT_LOG_DIR = Path(os.environ.get("AUDIT_LOG_DIR", "/app/audit_logs"))

# 0 keeps every file forever, which is the safe default for an audit trail:
# pruning is a decision for whoever owns the retention policy, not something
# this module should start doing on its own. Set it (e.g. 3650 for the ten
# years German clinical documentation is commonly kept) to enable pruning.
RETENTION_DAYS = int(os.environ.get("AUDIT_RETENTION_DAYS", "0"))

# Query/form parameter names whose *values* must never reach the log. The
# search criteria someone typed are exactly what an audit trail is for, so
# almost nothing is redacted here -- only credentials.
_REDACTED_KEYS = {
    "access_token", "token", "id_token", "refresh_token", "authorization",
    "password", "secret", "client_secret", "api_key", "apikey",
}

_lock = threading.Lock()
_handle = None          # currently open file object
_handle_day = None      # the UTC date _handle belongs to
_write_failed = False   # so a broken log directory reports itself once, not per call


def _utcnow():
    return datetime.now(timezone.utc)


def to_local(ts):
    """A stored timestamp as an aware datetime in this server's timezone.

    Records are written in UTC and stay that way on disk: an audit trail has
    to be unambiguous years later, across daylight-saving changes and whatever
    timezone the reader happens to be in. Only the presentation is localised
    (TZ in docker-compose.yml, Europe/Berlin here), because "08:45" in a table
    that a clinician is comparing against their own memory of the day has to
    be the time they experienced.

    Handles both trails' formats: the GUI writes "...T08:45:03.530Z", nginx
    writes "...T08:45:10+00:00".
    """
    if not ts:
        return None
    text = str(ts).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone()


def local_date(ts):
    """The calendar date a record falls on *locally* - which is not always
    its UTC date. Berlin runs UTC+1/+2, so anything logged before 01:00 or
    02:00 UTC belongs to the previous day as far as a local reader is
    concerned, and a date filter that ignored that would drop records the
    person can see sitting in the table."""
    local = to_local(ts)
    return local.date() if local else None


def _log_path(day):
    return AUDIT_LOG_DIR / f"phi-access-{day.isoformat()}.jsonl"


def _prune(day):
    """Drop day files older than RETENTION_DAYS. Called only on date rollover,
    so this costs one directory listing per day rather than one per write."""
    if RETENTION_DAYS <= 0:
        return
    cutoff = day - timedelta(days=RETENTION_DAYS)
    for path in AUDIT_LOG_DIR.glob("phi-access-*.jsonl"):
        try:
            stamp = datetime.strptime(path.stem, "phi-access-%Y-%m-%d").date()
        except ValueError:
            continue
        if stamp < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


def _open_for(day):
    """The append handle for `day`, reopening across midnight.

    Kept open rather than reopened per record: an iRODS batch stage-in writes
    one line per DICOM slice, and a fresh open() for each of thousands of
    files is a real cost on a network-backed mount.
    """
    global _handle, _handle_day

    if _handle is not None and _handle_day == day:
        return _handle

    if _handle is not None:
        try:
            _handle.close()
        except OSError:
            pass
        _handle = None

    # 0700/0600: these files name patients and the people who looked at them.
    AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(AUDIT_LOG_DIR, 0o700)
    except OSError:
        pass

    path = _log_path(day)
    is_new = not path.exists()
    _handle = open(path, "a", encoding="utf-8")
    _handle_day = day
    if is_new:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _prune(day)
    return _handle


def _sanitize(mapping):
    """Flatten a query string / form into "k=v" pairs, minus any credential."""
    if not mapping:
        return None
    parts = []
    for key in mapping:
        if key.lower() in _REDACTED_KEYS:
            parts.append(f"{key}=<redacted>")
            continue
        values = mapping.getlist(key) if hasattr(mapping, "getlist") else [mapping[key]]
        for value in values:
            text = str(value)
            if len(text) > 200:
                text = text[:200] + "..."
            parts.append(f"{key}={text}")
    return "&".join(parts) if parts else None


def current_actor():
    """The logged-in person's identity, as far as this request knows it.

    Returns a dict, never None: an unauthenticated request that still reached
    PHI is precisely the thing an audit trail must not silently drop, so it is
    recorded with actor "anonymous" instead.
    """
    if not has_request_context():
        return {"actor": "system", "actor_email": None, "actor_sub": None, "actor_ip": None}

    cached = getattr(g, "_audit_actor", None)
    if cached is not None:
        return cached

    actor = {
        "actor": "anonymous",
        "actor_email": None,
        "actor_sub": None,
        # X-Forwarded-For is only trustworthy behind the TLS proxy, where
        # ProxyFix has already rewritten remote_addr from it (see
        # app/__init__.py). Reading the raw header here instead would let any
        # client forge the source address in its own audit entries.
        "actor_ip": request.remote_addr,
    }

    try:
        from app.oidc_auth import oidc

        if oidc.user_loggedin:
            actor["actor"] = oidc.user_getfield("preferred_username") or "unknown"
            actor["actor_email"] = oidc.user_getfield("email")
            actor["actor_sub"] = oidc.user_getfield("sub")
    except Exception:
        # A malformed or expired session must not stop the record being written.
        pass

    g._audit_actor = actor
    return actor


def _request_id():
    """One id per HTTP request, so the many object-level lines a single
    download or batch stage-in produces can be collapsed back together."""
    if not has_request_context():
        return None
    rid = getattr(g, "_audit_request_id", None)
    if rid is None:
        rid = uuid.uuid4().hex[:16]
        g._audit_request_id = rid
    return rid


def record(system, action, *, object=None, object_type=None, outcome="success",
           actor=None, detail=None, patient_id=None, study_uid=None,
           series_uid=None, criteria=None, count=None, bytes=None, reason=None):
    """Append one PHI-access event.

    `system`      "dcm4chee" | "irods" | "api"
    `action`      "view" | "search" | "list" | "download" | "export" | "upload"
                  | "modify" | "delete" | "permission_change" | "account_admin"
    `outcome`     "success" | "denied" | "error"
    `actor`       override for a call with no Flask request context to read the
                  identity from -- a worker thread, typically. See
                  routes/batch.py's stage-in, which resolves the username on the
                  request thread and hands it down to its pool.

    Returns nothing and raises nothing.
    """
    global _write_failed

    try:
        identity = current_actor()
        if actor:
            identity = dict(identity, actor=actor)

        now = _utcnow()
        entry = {
            "ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "actor": identity["actor"],
            "actor_email": identity["actor_email"],
            "actor_ip": identity["actor_ip"],
            "system": system,
            "action": action,
            "object_type": object_type,
            "object": object,
            "outcome": outcome,
        }

        # Optional fields are omitted rather than written as null, so a line
        # stays readable in a terminal and grep -c on a key means something.
        for key, value in (
            ("patient_id", patient_id),
            ("study_uid", study_uid),
            ("series_uid", series_uid),
            ("criteria", criteria),
            ("count", count),
            ("bytes", bytes),
            ("reason", reason),
            ("detail", detail),
        ):
            if value is not None:
                entry[key] = value

        if has_request_context():
            entry["method"] = request.method
            entry["path"] = request.path
            entry["request_id"] = _request_id()

        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"

        with _lock:
            handle = _open_for(now.date())
            handle.write(line)
            handle.flush()
            _write_failed = False
    except Exception as exc:  # noqa: BLE001 - auditing must never break a request
        if not _write_failed:
            _write_failed = True
            print(f"[AUDIT] could not write audit record: {exc}", file=sys.stderr)


def record_request(system, action, **kwargs):
    """record() with this request's query string attached as `criteria`.

    The convenience matters for search: "who searched" is only meaningful with
    what they searched for, and for QIDO-RS/GenQuery that lives in the query
    string.
    """
    if has_request_context() and "criteria" not in kwargs:
        kwargs["criteria"] = _sanitize(request.args)
    record(system, action, **kwargs)


# --------------------------------------------------------------------------
# Reading the trail back (see routes/audit_log.py)
# --------------------------------------------------------------------------

def available_days():
    """Dates that have a log file, newest first."""
    days = []
    for path in AUDIT_LOG_DIR.glob("phi-access-*.jsonl"):
        try:
            days.append(datetime.strptime(path.stem, "phi-access-%Y-%m-%d").date())
        except ValueError:
            continue
    return sorted(days, reverse=True)


def _matches(entry, line, actor, system, action, needle, since=None, until=None):
    if actor and entry.get("actor") != actor:
        return False
    if system and entry.get("system") != system:
        return False
    if action and entry.get("action") != action:
        return False
    if needle and needle not in line.lower():
        return False
    # Date bounds are compared against the *local* date, matching what the
    # table shows - see local_date().
    if since or until:
        day = local_date(entry.get("ts"))
        if day is None:
            return False
        if since and day < since:
            return False
        if until and day > until:
            return False
    return True


def read_events(days=7, actor=None, system=None, action=None, contains=None,
                limit=2000, since=None, until=None):
    """Newest-first events across both trails, filtered.

    Merges this app's own day files with the nginx trail (see
    read_nginx_events): OHIF's traffic never reaches Flask, so without that
    second source a page claiming to be "the PHI access log" would silently
    omit the single most common thing anyone does - open a study in the
    viewer.

    Reads whole day files rather than seeking: a day holds thousands of lines,
    not millions, and reading them in full keeps this honest about a file that
    was appended to while we read. `limit` caps what is returned, and the total
    number of matches is reported separately so the caller can say "showing
    500 of 12043" rather than silently truncating.
    """
    needle = (contains or "").lower() or None
    matched = 0
    out = []

    # Day files are named by their UTC date, but the filter the reader typed
    # is a local one, so widen the file selection by a day at each end and let
    # the per-entry local-date check in _matches() decide. Without this, a row
    # displayed as "11 Aug 00:30" (10 Aug 22:30 UTC) disappears when filtering
    # from 11 Aug - it lives in the previous day's file.
    file_since = since - timedelta(days=1) if since else None
    file_until = until + timedelta(days=1) if until else None
    candidates = [
        day for day in available_days()
        if (file_since is None or day >= file_since)
        and (file_until is None or day <= file_until)
    ]
    if days:
        candidates = candidates[:days]

    for day in candidates:
        try:
            with open(_log_path(day), "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue

        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue

            if not _matches(entry, line, actor, system, action, needle, since, until):
                continue

            matched += 1
            if len(out) < limit:
                out.append(entry)

    # The nginx trail spans all dates in one file, so it is filtered by date
    # here rather than by picking files.
    oldest = candidates[-1] if candidates and days else None
    for entry in read_nginx_events(since=file_since or oldest, until=file_until):
        line = json.dumps(entry, default=str)
        if not _matches(entry, line, actor, system, action, needle, since, until):
            continue
        matched += 1
        out.append(entry)

    out.sort(key=lambda entry: entry.get("ts") or "", reverse=True)
    return out[:limit], matched


# The nginx PHI trail (viewers/…/Nginx-Dcm4chee/config/nginx.conf), mounted
# read-only into this container. Everything OHIF does against dcm4chee is
# recorded there and nowhere else, because that traffic never reaches Flask.
NGINX_AUDIT_LOG = Path(
    os.environ.get("NGINX_AUDIT_LOG", "/app/nginx_audit/phi-audit.log")
)

# How much of the tail to read. The file is one line per HTTP request against
# the archive and is not rotated by default, so it grows without bound; a page
# load must not turn into reading a gigabyte.
NGINX_READ_BYTES = int(os.environ.get("NGINX_AUDIT_READ_BYTES", str(64 * 1024 * 1024)))

# Requests that are the UI2 app loading itself, not patient data.
_NGINX_ASSET_SUFFIXES = (
    ".js", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".html",
)

_NGINX_STUDY_RE = re.compile(r"/studies/([0-9.]+)")
_NGINX_SERIES_RE = re.compile(r"/series/([0-9.]+)")

# Opening one study in OHIF is hundreds of frame requests. Each is a genuine
# access and stays in the raw file, but listing them individually would bury
# every other event on the page, so contiguous reads of the same study by the
# same person are collapsed into one row carrying the request count. The
# bucket is a wall-clock window rather than a session: nothing in an access
# log marks where "viewing a study" starts and stops.
NGINX_COLLAPSE_MINUTES = int(os.environ.get("NGINX_AUDIT_COLLAPSE_MINUTES", "15"))

# Query parameters that say how to render a result set, not what was searched
# for. What defines a search, for audit purposes, is the patient criteria -
# PatientID, PatientName, StudyDate, AccessionNumber and friends, which are
# deliberately NOT in this list and are recorded in full.
_QIDO_PAGINATION_PARAMS = {
    "limit", "offset", "includefield", "fuzzymatching", "orderby",
    "includedefaults", "_",
}


def _search_signature(criteria):
    """The patient-identifying part of a QIDO query string."""
    if not criteria:
        return ""
    kept = []
    for pair in criteria.split("&"):
        name = pair.split("=", 1)[0].lower()
        if name and name not in _QIDO_PAGINATION_PARAMS:
            kept.append(pair)
    return "&".join(sorted(kept))


def _same_typing_run(previous, current):
    """True when two searches are one person typing, not two searches.

    OHIF's worklist filter queries the archive on every keystroke, so
    searching for "Test" hits dcm4chee four times, as PatientName=T, =Te,
    =Tes, =Test. Recording four searches would be literally true and
    practically wrong - a reviewer asking "who searched for this patient"
    wants the search, not the typing.

    Detected by one signature being a prefix of the other, which also catches
    backspacing ("Test" -> "Tes"). Both must be non-empty: an empty signature
    is the unfiltered worklist load, and letting that absorb a subsequent
    named search would hide the search entirely.
    """
    if previous == current:
        return True
    if not previous or not current:
        return False
    return previous.startswith(current) or current.startswith(previous)


def _nginx_action(method, uri):
    """(action, object_type) for an nginx-logged request, or None to skip.

    Mirrors routes/dcm4chee_proxy._classify - the same URL space, seen from
    the other side of the same archive.
    """
    lowered = uri.lower()
    if lowered.endswith(_NGINX_ASSET_SUFFIXES):
        return None
    if "/dcm4chee-arc/" not in lowered and "/pacs" not in lowered:
        return None

    has_study = "/studies/" in lowered or lowered.endswith("/studies")
    if method == "DELETE":
        return "delete", "study" if has_study else "object"
    if method in ("POST", "PUT", "PATCH"):
        if "/reject/" in lowered or "/expire/" in lowered:
            return "modify", "study"
        if "/export/" in lowered:
            return "export", "study"
        if lowered.rstrip("/").endswith("/studies"):
            return "upload", "study"
        return "modify", "study" if has_study else "object"
    if "/wado" in lowered:
        return "view", "instance"
    if _NGINX_STUDY_RE.search(uri):
        return "view", "instance" if "/instances/" in lowered else "study"
    if lowered.rstrip("/").endswith(("/studies", "/series", "/instances")):
        return "search", "study"
    return None


def read_nginx_events(since=None, until=None):
    """The nginx trail, normalised into the same shape as our own records."""
    try:
        size = NGINX_AUDIT_LOG.stat().st_size
        with open(NGINX_AUDIT_LOG, "r", encoding="utf-8", errors="replace") as fh:
            if size > NGINX_READ_BYTES:
                fh.seek(size - NGINX_READ_BYTES)
                fh.readline()  # drop the partial line the seek landed inside
            lines = fh.readlines()
    except OSError:
        # Not mounted, or not readable. The GUI's own trail still renders;
        # routes/audit_log.py tells the reader this source is missing.
        return []

    collapsed = {}
    out = []

    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue

        uri = raw.get("object") or ""
        classified = _nginx_action(raw.get("method", "GET"), uri)
        if classified is None:
            continue
        act, object_type = classified

        ts = raw.get("ts") or ""
        day = ts[:10]
        if since and day and day < since.isoformat():
            continue
        if until and day and day > until.isoformat():
            continue

        status = raw.get("status") or 0
        study = _NGINX_STUDY_RE.search(uri)
        series = _NGINX_SERIES_RE.search(uri)

        entry = {
            "ts": ts,
            "actor": raw.get("actor") or "anonymous",
            "actor_email": raw.get("actor_email") or None,
            # The proxy's own address is noise; the person's address is in
            # forwarded_for. Prefer it, and say so rather than silently
            # showing a container IP as if it were the user's.
            "actor_ip": raw.get("forwarded_for") or raw.get("actor_ip"),
            "system": "dcm4chee",
            "action": act,
            "object_type": object_type,
            "object": uri,
            "outcome": ("denied" if status in (401, 403)
                        else "error" if status >= 400 else "success"),
            "method": raw.get("method"),
            "source": "nginx",
        }
        if study:
            entry["study_uid"] = study.group(1)
        if series:
            entry["series_uid"] = series.group(1)
        if raw.get("criteria"):
            entry["criteria"] = raw["criteria"]

        # Collapse the two things OHIF does over and over for one human
        # action: fetching a study's images (hundreds of frame requests), and
        # re-running the study-list query (the worklist polls). Never
        # collapse a delete, an upload or a rejection - each of those is a
        # distinct act and must appear once per occurrence.
        collapse_key = None
        signature = None
        if act == "view" and study:
            collapse_key = ("view", study.group(1))
        elif act == "search":
            signature = _search_signature(raw.get("criteria"))
            # Deliberately not keyed on the signature: consecutive searches
            # are merged by _same_typing_run below, which needs to compare
            # against whatever the previous one in this bucket was.
            collapse_key = ("search", uri)

        if collapse_key is not None:
            try:
                minute = datetime.strptime(ts[:16], "%Y-%m-%dT%H:%M")
            except ValueError:
                minute = None
            if minute is not None:
                bucket = int(minute.timestamp() // (NGINX_COLLAPSE_MINUTES * 60))
                key = (entry["actor"],) + collapse_key + (bucket,)
                existing = collapsed.get(key)

                if existing is not None and act == "search" and \
                        not _same_typing_run(existing["_signature"], signature):
                    # A genuinely different search in the same window: close
                    # the previous row off and start a new one.
                    existing = None

                if existing is None:
                    if act == "view":
                        entry["object"] = f"/studies/{study.group(1)}"
                        entry["object_type"] = "study"
                        entry["detail"] = "opened in the OHIF viewer"
                    else:
                        entry["_signature"] = signature
                        entry["detail"] = "study list query"
                    entry["count"] = 1
                    entry["bytes"] = raw.get("bytes") or 0
                    collapsed[key] = entry
                    out.append(entry)
                else:
                    existing["count"] += 1
                    existing["bytes"] = (existing.get("bytes") or 0) + (raw.get("bytes") or 0)
                    if act == "search" and len(signature) > len(existing["_signature"]):
                        # Keep the completed search term, not the first
                        # keystroke of it.
                        existing["_signature"] = signature
                        existing["criteria"] = raw.get("criteria")
                    # Keep the newest timestamp so the row sorts where the
                    # activity actually ended.
                    if ts > existing["ts"]:
                        existing["ts"] = ts
                continue

        out.append(entry)

    for entry in out:
        entry.pop("_signature", None)  # bookkeeping, not part of the record
    return out


CSV_COLUMNS = [
    "ts", "ts_local", "actor", "actor_email", "actor_ip", "system", "action",
    "object_type", "object", "outcome", "patient_id", "study_uid",
    "series_uid", "criteria", "count", "bytes", "reason", "detail", "method",
    "path", "request_id", "source",
]


def to_csv(events):
    """Render events as CSV, for handing a regulator or a patient's Art. 15
    request something that opens in a spreadsheet.

    Carries both timestamps: `ts` is the UTC record as stored, `ts_local` the
    same instant in server time. Exporting only one of them invites the
    recipient to misread it - UTC alone gets compared against local wall-clock
    memories, local alone loses the offset it was rendered at.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for entry in events:
        local = to_local(entry.get("ts"))
        writer.writerow(
            dict(entry, ts_local=local.strftime("%Y-%m-%d %H:%M:%S %Z") if local else "")
        )
    return buffer.getvalue()
