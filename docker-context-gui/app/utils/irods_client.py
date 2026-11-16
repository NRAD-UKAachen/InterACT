# app/utils/irods_client.py
"""
Single chokepoint for talking to iRODS, via the official iRODS HTTP API.

The GUI never holds an iRODS credential of any kind. Every call forwards the
logged-in person's *own* Keycloak access token as a Bearer token; the HTTP API
introspects it against Keycloak, maps the "preferred_username" claim to an iRODS
account, and carries out the request as that user. iRODS therefore enforces that
person's real ACLs - see Documentation/iRODS.md.

This replaced an earlier design where the GUI authenticated as the "rods"
rodsadmin and opened proxy sessions with client_user=. That required shipping the
zone's master password to the web app; forwarding the user's own token does not.
Consequence: admin operations below need the *caller* to be a rodsadmin in iRODS,
not just an Admin in Keycloak.

Being the single chokepoint is also why the PHI audit trail is written here
rather than in the routes: every listing, read, write, delete and ACL change
goes through one of the functions below, including the ones a background worker
makes with no HTTP request of its own, so there is no route to forget to
instrument. See app/utils/audit.py.
"""
import os
from concurrent.futures import ThreadPoolExecutor

import requests

from app.oidc_auth import oidc
from app.utils import audit

IRODS_HTTP_API_URL = os.environ.get(
    "IRODS_HTTP_API_URL", "http://irods-http-api:9000/irods-http-api/0.6.0"
).rstrip("/")
IRODS_ZONE = os.environ.get("IRODS_ZONE", "interactZone")

# Long enough for big uploads over a slow link, short enough that a wedged
# HTTP API surfaces as an error page rather than a hung worker.
REQUEST_TIMEOUT = (5, 300)

# A batch cohort stage-in (see docker-context-gui/app/routes/batch.py) makes
# many small requests concurrently - one per DICOM slice - so this needs a
# real connection pool rather than requests' bare module-level functions,
# which open (and TLS-negotiate, where applicable) a fresh connection per
# call. pool_maxsize covers the HTTP API's own request-handling thread count
# (IRODS/http_api/config.json.template's http_server.requests.threads: 8 --
# see that repo's README on why it and the read-buffer size were raised above
# the shipped defaults) plus slack for other concurrent users of this module
# (the file browser, uploads, ...).
_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32)
_session.mount("http://", _adapter)
_session.mount("https://", _adapter)

# How many data objects write_data_objects() pushes at once. Creating a data
# object costs ~25-30 ms in iRODS itself (catalog inserts, replica and ACL
# rows) no matter how few bytes it holds, and that cost is almost entirely
# waiting - measured on this deployment, 200 x 150 KiB objects took 4.9 s
# written one at a time and 5.2 s written 8-way concurrently, i.e. 41 vs 306
# objects/second. Serial writes were the entire reason uploads felt slow.
#
# Keep this at or below http_server.requests.threads in
# IRODS/http_api/config.json.template (16): a write occupies one request
# thread of the HTTP API for its whole duration, so more writers than threads
# only queues them there, and leaves nothing to serve the browser's own page
# loads while an upload runs.
WRITE_CONCURRENCY = int(os.environ.get("IRODS_WRITE_CONCURRENCY", "8"))

PERMISSION_LEVELS = ["read", "write", "own", "null"]

# iRODS status codes we care enough about to turn into distinct exceptions.
# iRODS deliberately reports "you may not look at this" as though the thing does
# not exist, so several of these collapse onto the same user-facing meaning.
_CAT_NO_ACCESS_PERMISSION = -818000
_CAT_UNKNOWN_COLLECTION = -814000
_CAT_UNKNOWN_FILE = -817000
_NOT_A_COLLECTION = -170000
_OBJ_PATH_DOES_NOT_EXIST = -358000
_CAT_NAME_EXISTS_AS_COLLECTION = -809000
_CAT_NAME_EXISTS_AS_DATAOBJ = -804000
_CAT_INVALID_USER = -827000
_CAT_SQL_ERR = -806000

_NOT_FOUND_CODES = {
    _CAT_UNKNOWN_COLLECTION,
    _CAT_UNKNOWN_FILE,
    _NOT_A_COLLECTION,
    _OBJ_PATH_DOES_NOT_EXIST,
}
_EXISTS_CODES = {_CAT_NAME_EXISTS_AS_COLLECTION, _CAT_NAME_EXISTS_AS_DATAOBJ}


class IrodsError(Exception):
    """Any failure talking to iRODS. `code` is the iRODS status code if known."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class IrodsUnavailable(IrodsError):
    """The HTTP API could not be reached at all."""


class IrodsNoAccount(IrodsError):
    """Keycloak authenticated the person, but they have no iRODS account.

    The HTTP API cannot distinguish this from a bad token - both are a 401 - so
    treat any 401 as "not provisioned" and say so in a way an end user can act on.
    """


class IrodsPermissionDenied(IrodsError):
    pass


class IrodsNotFound(IrodsError):
    pass


class IrodsAlreadyExists(IrodsError):
    pass


def home_collection(username):
    return f"/{IRODS_ZONE}/home/{username}"


def current_username():
    return oidc.user_getfield("preferred_username")


def current_access_token():
    """The logged-in person's access token, for a caller that needs to carry
    it into a context this module can't reach on its own (a background
    thread - see docker-context-gui/app/routes/batch.py's stage-in, which
    downloads many files via a worker pool with no Flask request/session
    active). Must be called from within a request; hand the result to the
    `token=` kwarg on read_data_object/write_data_object etc. from there."""
    return oidc.get_access_token()


def _auth_header(token=None):
    token = token if token is not None else oidc.get_access_token()
    if not token:
        raise IrodsNoAccount("You are not logged in.")
    return {"Authorization": f"Bearer {token}"}


def _check_irods_response(payload):
    """Raise for a non-zero iRODS status code inside a 200 response body.

    The HTTP API answers 200 for "the request was well formed and I asked iRODS",
    then reports the iRODS-level outcome in the body, so a 200 alone means nothing.
    """
    if not isinstance(payload, dict):
        return payload
    response = payload.get("irods_response") or {}
    code = response.get("status_code", 0)
    if code == 0:
        return payload

    message = response.get("status_message") or f"iRODS error {code}"
    if code == _CAT_NO_ACCESS_PERMISSION:
        raise IrodsPermissionDenied("You don't have permission to do that.", code)
    if code in _NOT_FOUND_CODES:
        raise IrodsNotFound("That collection or file does not exist, or you can't see it.", code)
    if code in _EXISTS_CODES:
        raise IrodsAlreadyExists("Something with that name already exists here.", code)
    if code == _CAT_INVALID_USER:
        raise IrodsNotFound("No such iRODS user.", code)
    raise IrodsError(message, code)


def _request(method, endpoint, *, params=None, data=None, stream=False, token=None, extra_headers=None, **kwargs):
    url = f"{IRODS_HTTP_API_URL}/{endpoint}"
    headers = _auth_header(token)
    if extra_headers:
        headers.update(extra_headers)
    try:
        response = _session.request(
            method,
            url,
            headers=headers,
            params=params,
            data=data,
            timeout=REQUEST_TIMEOUT,
            stream=stream,
            **kwargs,
        )
    except requests.exceptions.RequestException as exc:
        raise IrodsUnavailable("Could not reach the iRODS server.") from exc

    if response.status_code == 401:
        raise IrodsNoAccount(
            "You don't have an iRODS account yet - ask an admin to create one "
            "matching your username."
        )
    if response.status_code == 403:
        raise IrodsPermissionDenied("iRODS refused that operation.")
    if response.status_code >= 400:
        raise IrodsError(f"iRODS HTTP API returned {response.status_code}.")

    if stream:
        return response
    if not response.content:
        return {}
    return _check_irods_response(response.json())


def _get(endpoint, params, **kwargs):
    return _request("GET", endpoint, params=params, **kwargs)


def _post(endpoint, data, **kwargs):
    return _request("POST", endpoint, data=data, **kwargs)


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------

def _audited(action, object_type, lpath, call, *, actor=None, **fields):
    """Run one iRODS operation and record it, whether or not it succeeded.

    A refused operation is logged too, as outcome "denied": an audit trail that
    only shows successes cannot answer "did anyone try to reach this patient's
    data", which is usually the first question asked after an incident.
    """
    try:
        result = call()
    except IrodsPermissionDenied as exc:
        audit.record("irods", action, object=lpath, object_type=object_type,
                     outcome="denied", actor=actor, detail=str(exc), **fields)
        raise
    except IrodsError as exc:
        audit.record("irods", action, object=lpath, object_type=object_type,
                     outcome="error", actor=actor, detail=str(exc), **fields)
        raise

    audit.record("irods", action, object=lpath, object_type=object_type,
                 actor=actor, **fields)
    return result


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------

def _quote(value):
    """Escape a value for embedding in a GenQuery string literal.

    GenQuery uses SQL-style doubling, so a collection genuinely named
    "it's here" becomes 'it''s here'. Verified against both parsers.
    """
    return str(value).replace("'", "''")


# Must match irods_client.max_number_of_rows_per_catalog_query in
# IRODS/http_api/config.json.template (see Documentation/iRODS.md's throughput-tuning
# table). This HTTP API version has no client-facing continuation for
# GenQuery1: a query past this cap is silently truncated to it, with nothing
# in the response to say so - discovered when a 1952-file DICOM series staged
# into a batch job as only 512 files (the cap in effect at the time), no
# error either side. No caller here ever passes `count`, so in practice this
# server-side cap is always what limits a query's rows.
_MAX_ROWS_PER_QUERY = 20000


def genquery(query, parser="genquery1", count=None, token=None):
    params = {"op": "execute_genquery", "query": query, "parser": parser}
    if count is not None:
        params["count"] = count
    rows = _get("query", params, token=token).get("rows") or []

    limit = count if count is not None else _MAX_ROWS_PER_QUERY
    if len(rows) >= limit:
        raise IrodsError(
            f"This query returned {len(rows)} rows, at or beyond the "
            f"per-query cap ({limit}) - results were very likely truncated "
            "rather than complete. Narrow the query (e.g. a smaller "
            "collection), or raise max_number_of_rows_per_catalog_query."
        )
    return rows


# --------------------------------------------------------------------------
# Browsing
# --------------------------------------------------------------------------

def list_collection(lpath, token=None):
    """Return {"collections": [...], "data_objects": [...]} for one collection.

    Uses GenQuery rather than the HTTP API's collections/list operation because
    list returns bare paths with no indication of type, size, or mtime - which is
    exactly what the browser needs to render a row.

    Assumes the collection exists and is readable: a query against a collection
    that doesn't exist (or that you can't see) simply matches nothing, so this
    returns an empty listing rather than raising. Call stat_collection first if
    you need to tell "empty" apart from "not there".
    """
    path = _quote(lpath.rstrip("/") or "/")

    collections = [
        {
            "path": row[0],
            "name": row[0].rsplit("/", 1)[-1],
            "modified": row[1],
        }
        for row in genquery(
            f"select COLL_NAME, COLL_MODIFY_TIME where COLL_PARENT_NAME = '{path}'",
            token=token,
        )
    ]
    data_objects = [
        {
            "path": f"{lpath.rstrip('/')}/{row[0]}",
            "name": row[0],
            "size": int(row[1]) if row[1] else 0,
            "modified": row[2],
        }
        for row in genquery(
            f"select DATA_NAME, DATA_SIZE, DATA_MODIFY_TIME where COLL_NAME = '{path}'",
            token=token,
        )
    ]

    collections.sort(key=lambda entry: entry["name"].lower())
    data_objects.sort(key=lambda entry: entry["name"].lower())

    # "Who looked at what folder", which for a file browser is the closest
    # thing to "who viewed these files" short of opening each one.
    audit.record(
        "irods", "list", object=lpath, object_type="collection",
        count=len(collections) + len(data_objects),
    )
    return {"collections": collections, "data_objects": data_objects}


def stat_collection(lpath):
    """Permissions and metadata for one collection.

    Only *failures* are audited here. A successful stat is always followed by
    the list_collection above, which records the view, so auditing both would
    double every folder someone opens. A refusal has no such follow-up, and
    "who tried to open a collection they cannot see" is exactly the kind of
    thing the trail is kept for - browse() calls this first precisely because
    it is the call that refuses.
    """
    try:
        return _get("collections", {"op": "stat", "lpath": lpath})
    except IrodsError as exc:
        audit.record(
            "irods", "list", object=lpath, object_type="collection",
            outcome="denied" if isinstance(exc, (IrodsPermissionDenied, IrodsNoAccount))
                    else "error",
            detail=str(exc),
        )
        raise


def _shared_files_outside(username, listed_collections):
    """Files shared with this person individually, i.e. not inside a folder
    that shared_with_me already lists.

    Files inside listed folders are excluded in the query itself rather than
    filtered afterwards: a recursively shared cohort holds thousands of
    objects, which would hit the per-query row cap and fail the whole page.
    """
    own_home = home_collection(username)
    conditions = [
        f"DATA_ACCESS_USER_NAME = '{_quote(username)}'",
        f"COLL_NAME != '{_quote(own_home)}'",
        f"COLL_NAME not like '{_quote(own_home)}/%'",
        f"COLL_NAME not like '/{_quote(IRODS_ZONE)}/trash/%'",
    ]
    for coll in listed_collections:
        conditions.append(f"COLL_NAME != '{_quote(coll)}'")
        conditions.append(f"COLL_NAME not like '{_quote(coll)}/%'")
    rows = genquery(
        "select COLL_NAME, DATA_NAME, DATA_ACCESS_PERM_NAME where "
        + " and ".join(conditions),
        parser="genquery2",
    )
    # One row per replica, so the same file can come back more than once.
    files = {}
    for coll, name, permission in rows:
        path = f"{coll}/{name}"
        files[path] = {"path": path, "name": name, "permission": permission,
                       "type": "data_object"}
    return list(files.values())


def shared_with_me(username):
    """Collections this person can reach that are not under their own home,
    plus files shared with them individually (see _shared_files_outside).

    This is the entry point a tree browser structurally cannot provide: iRODS has
    no path traversal permission, so being granted read on
    /zone/home/alice/project does NOT let you list /zone/home to find it. Without
    this query a share is invisible to the person it was granted to.

    GenQuery2 is required - COLL_ACCESS_USER_NAME does not exist in GenQuery1.
    It also resolves group membership on its own: a collection shared with a
    group this person belongs to comes back from a query on their *username*,
    so there is no need to query each of their groups separately.
    """
    rows = genquery(
        "select COLL_NAME, COLL_ACCESS_PERM_NAME "
        f"where COLL_ACCESS_USER_NAME = '{_quote(username)}'",
        parser="genquery2",
    )

    own_home = home_collection(username)
    trash_prefix = f"/{IRODS_ZONE}/trash/"

    shared = [
        {"path": row[0], "name": row[0].rsplit("/", 1)[-1], "permission": row[1],
         "type": "collection"}
        for row in rows
        if not row[0].startswith(trash_prefix)
        and row[0] != own_home
        and not row[0].startswith(f"{own_home}/")
    ]

    # Shares are recursive (see share_collection), so every subfolder of a
    # shared folder comes back too. List only the top of each shared tree; the
    # person browses down into it from there, with its structure intact.
    # Exception: a grant on someone's whole home (/zone/home/alice) usually
    # exists only so a share beneath it can be navigated to, so a home with
    # shared entries beneath it is dropped in favour of those entries.
    home_prefix = f"/{IRODS_ZONE}/home/"
    paths = {entry["path"] for entry in shared}

    def has_shared_descendant(path):
        return any(other.startswith(f"{path}/") for other in paths)

    def is_navigation_home(path):
        return (path.startswith(home_prefix)
                and "/" not in path[len(home_prefix):]
                and has_shared_descendant(path))

    def ancestors(path):
        parts = path.split("/")
        return ("/".join(parts[:i]) for i in range(2, len(parts)))

    # Checking each path's own ancestors, not every pair: a shared cohort can
    # contain thousands of series folders.
    listed = {path for path in paths if not is_navigation_home(path)}
    shared = [
        entry
        for entry in shared
        if entry["path"] in listed
        and not any(parent in listed for parent in ancestors(entry["path"]))
    ]

    shared += _shared_files_outside(username, [entry["path"] for entry in shared])
    shared.sort(key=lambda entry: entry["path"].lower())

    audit.record(
        "irods", "search", object_type="collection", count=len(shared),
        criteria=f"shared_with={username}",
    )
    return shared


# --------------------------------------------------------------------------
# Collections
# --------------------------------------------------------------------------

def create_collection(lpath, token=None, actor=None):
    """`token` / `actor`: as on read_data_object - pass both explicitly when
    calling this from outside a Flask request (batch.py's stage-in creates
    the collection its tar bundles land in from a background thread)."""
    return _audited(
        "create", "collection", lpath,
        lambda: _post("collections", {"op": "create", "lpath": lpath}, token=token),
        actor=actor,
    )


def remove_collection(lpath, recurse=True):
    return _audited(
        "delete", "collection", lpath,
        lambda: _post(
            "collections",
            {"op": "remove", "lpath": lpath, "recurse": int(bool(recurse))},
        ),
        detail="recursive" if recurse else None,
    )


def rename_collection(old_lpath, new_lpath):
    return _audited(
        "modify", "collection", old_lpath,
        lambda: _post(
            "collections",
            {"op": "rename", "old-lpath": old_lpath, "new-lpath": new_lpath},
        ),
        detail=f"renamed to {new_lpath}",
    )


def set_collection_permission(lpath, entity_name, permission, admin=False):
    return _audited(
        "permission_change", "collection", lpath,
        lambda: _post(
            "collections",
            {
                "op": "set_permission",
                "lpath": lpath,
                "entity-name": entity_name,
                "permission": permission,
                "admin": int(bool(admin)),
            },
        ),
        detail=f"{entity_name} -> {permission}" + (" (admin mode)" if admin else ""),
    )


def share_collection(lpath, entity_name, permission, admin=False):
    """Grant `permission` on a collection *and everything already inside it*.

    The HTTP API's collections set_permission touches only the collection
    itself, and inheritance only applies to entries created afterwards, so a
    plain grant shares an empty-looking folder: the grantee sees it but none
    of its existing subfolders or files. This is `ichmod -r` instead, run
    server-side through the rule engine as the caller, so iRODS still checks
    that they own each entry (or are a rodsadmin, with `admin`).

    Inheritance is always turned on throughout the tree as well (not when
    revoking), so files added later are shared too. It is deliberately not
    optional: a share without it silently goes stale.

    Returns False when the path can't be embedded in rule text: the grant is
    then applied to the collection alone, as before, and the caller should
    say that existing contents were not covered.
    """
    if not (path_is_rule_safe(lpath) and path_is_rule_safe(entity_name)):
        set_collection_permission(lpath, entity_name, permission, admin=admin)
        if permission != "null":
            set_inheritance(lpath, True, admin=admin)
        return False

    flag = "admin:recursive" if admin else "recursive"
    rule_text = f'msiSetACL("{flag}", "{permission}", "{entity_name}", "{lpath}");'
    if permission != "null":
        rule_text += f' msiSetACL("{flag}", "inherit", "{entity_name}", "{lpath}");'
    _audited(
        "permission_change", "collection", lpath,
        lambda: execute_rule(rule_text),
        detail=f"{entity_name} -> {permission} (recursive)"
               + (", inheritance enabled" if permission != "null" else "")
               + (" (admin mode)" if admin else ""),
    )
    return True


def set_inheritance(lpath, enable, admin=False):
    """Turn ACL inheritance on for a collection.

    Without this, a grant only covers what exists at grant time: anything added
    to a shared collection afterwards is invisible to everyone it was shared with.
    This is the single most common reason "sharing doesn't work" in iRODS.
    """
    return _audited(
        "permission_change", "collection", lpath,
        lambda: _post(
            "collections",
            {
                "op": "set_inheritance",
                "lpath": lpath,
                "enable": int(bool(enable)),
                "admin": int(bool(admin)),
            },
        ),
        detail=f"inheritance {'enabled' if enable else 'disabled'}"
               + (" (admin mode)" if admin else ""),
    )


# --------------------------------------------------------------------------
# Data objects
# --------------------------------------------------------------------------

def read_data_object(lpath, token=None, close_connection=False, actor=None):
    """Stream a data object's bytes. Returns a streaming requests.Response.

    `token`: pass the caller's access token explicitly (see
    current_access_token()) when calling this from outside a Flask request -
    e.g. a background thread - since the default (pulling it from the
    session) only works inside one.

    `actor`: the username to attribute this read to in the audit trail, for
    the same reason - a worker thread has no session for audit.current_actor()
    to read the identity out of, and an unattributed PHI read is exactly what
    the trail exists to prevent. Resolve it on the request thread with
    current_username() and pass it down.

    `close_connection`: send `Connection: close` instead of reusing a pooled
    connection. Added for docker-context-gui's batch stage-in, which found
    the HTTP API could wedge a request outright (no error either side, just
    silence) under sustained concurrent reads over reused connections."""
    extra_headers = {"Connection": "close"} if close_connection else None
    return _audited(
        "download", "data_object", lpath,
        lambda: _request(
            "GET",
            "data-objects",
            params={"op": "read", "lpath": lpath},
            stream=True,
            token=token,
            extra_headers=extra_headers,
        ),
        actor=actor,
    )


def write_data_object(lpath, fileobj, token=None, actor=None):
    """Stream bytes into a data object.

    Uses the HTTP API's "method 2" (operation in headers, payload as the raw
    body) so neither Flask nor the HTTP API buffers the whole upload in memory.

    `token` / `actor`: as on read_data_object - pass both explicitly when
    calling this from outside a Flask request, e.g. from one of
    write_data_objects()' worker threads.
    """
    return _audited(
        "upload", "data_object", lpath,
        lambda: _write_data_object(lpath, fileobj, token),
        actor=actor,
    )


def _write_data_object(lpath, fileobj, token):
    headers = _auth_header(token)
    headers.update(
        {
            "irods-api-request-op": "write",
            "irods-api-request-lpath": lpath,
            "irods-api-request-truncate": "1",
            "Content-Type": "application/octet-stream",
        }
    )
    try:
        response = _session.post(
            f"{IRODS_HTTP_API_URL}/data-objects",
            headers=headers,
            data=fileobj,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        raise IrodsUnavailable("Could not reach the iRODS server.") from exc

    if response.status_code == 401:
        raise IrodsNoAccount("You don't have an iRODS account yet.")
    if response.status_code >= 400:
        raise IrodsError(f"Upload failed: iRODS HTTP API returned {response.status_code}.")
    return _check_irods_response(response.json() if response.content else {})


def write_data_objects(items, token=None, concurrency=None, actor=None):
    """Write several data objects concurrently. Returns [(lpath, error), ...].

    `items` is an iterable of (lpath, opener), where opener() returns a fresh
    file object positioned at the start - a callable rather than an open
    stream because each write is handed to a different thread and a single
    stream cannot be read by more than one of them.

    `error` is None for a write that succeeded and the IrodsError otherwise,
    so one bad file (no permission, name taken) reports itself without
    aborting the rest of the upload. Results come back in `items` order, not
    completion order.

    Must be called from within a Flask request unless `token` and `actor` are
    given: both are read once here, on the calling thread, because the workers
    have no request context to read them from themselves.
    """
    items = list(items)
    if not items:
        return []

    token = token if token is not None else oidc.get_access_token()
    actor = actor if actor is not None else audit.current_actor()["actor"]

    def write_one(item):
        lpath, opener = item
        try:
            fileobj = opener()
        except OSError as exc:
            audit.record("irods", "upload", object=lpath,
                         object_type="data_object", outcome="error",
                         actor=actor, detail=f"Could not read local file: {exc}")
            return lpath, IrodsError(f"Could not read local file: {exc}")
        try:
            write_data_object(lpath, fileobj, token=token, actor=actor)
            return lpath, None
        except IrodsError as exc:
            return lpath, exc
        finally:
            close = getattr(fileobj, "close", None)
            if close:
                close()

    workers = min(concurrency or WRITE_CONCURRENCY, len(items))
    if workers <= 1:
        return [write_one(item) for item in items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(write_one, items))


def remove_data_object(lpath, token=None, actor=None):
    # catalog-only has no default in the HTTP API - omitting it is a 400.
    return _audited(
        "delete", "data_object", lpath,
        lambda: _post(
            "data-objects", {"op": "remove", "lpath": lpath, "catalog-only": 0},
            token=token,
        ),
        actor=actor,
    )


def set_data_object_permission(lpath, entity_name, permission, admin=False):
    return _audited(
        "permission_change", "data_object", lpath,
        lambda: _post(
            "data-objects",
            {
                "op": "set_permission",
                "lpath": lpath,
                "entity-name": entity_name,
                "permission": permission,
                "admin": int(bool(admin)),
            },
        ),
        detail=f"{entity_name} -> {permission}" + (" (admin mode)" if admin else ""),
    )


# --------------------------------------------------------------------------
# Rules / server-side bundling
# --------------------------------------------------------------------------

# The rule engine plugin instance to run rule text against. Must match an
# instance_name in the provider's /etc/irods/server_config.json
# (plugin_configuration.rule_engines) - the native rule language one, since
# that is what understands msiTarFileCreate.
RULE_ENGINE_INSTANCE = os.environ.get(
    "IRODS_RULE_ENGINE_INSTANCE",
    "irods_rule_engine_plugin-irods_rule_language-instance",
)

# Characters that cannot be embedded in an iRODS rule-language string literal
# without changing what the rule means. `*` introduces a variable and `$` a
# session variable - both are expanded *inside* double quotes - and `"` and
# `\` end or escape the literal. A collection whose name contains any of them
# is refused rather than escaped: the only caller (batch.py's stage-in) simply
# falls back to per-file downloads, so the cautious answer costs a slower
# stage rather than a wrong or injected rule.
_RULE_UNSAFE_CHARS = set('"\\*$`\r\n')


def path_is_rule_safe(lpath):
    return not (set(lpath) & _RULE_UNSAFE_CHARS)


def execute_rule(rule_text, token=None, rep_instance=None):
    """Run rule text on the server and return the HTTP API's response.

    `token`: as on read_data_object - required when calling from outside a
    Flask request.
    """
    return _post(
        "rules",
        {
            "op": "execute",
            "rule-text": rule_text,
            "rep-instance": rep_instance or RULE_ENGINE_INSTANCE,
        },
        token=token,
    )


def create_tar_bundle(coll_path, tar_lpath, token=None, actor=None, file_count=None):
    """Tar `coll_path` server-side into a new data object at `tar_lpath`.

    This is what `ibun -c` does, driven through the rule engine: the bytes
    never leave the provider, so a collection of thousands of small DICOM
    files becomes *one* object the caller can then fetch in a single read.
    That is the whole point - see batch.py's stage-in, where the cost was
    never the data volume but the per-file "open, read, close" round trip.

    Needs only *read* on each object in `coll_path` (the bundle API's
    chkCollForBundleOpr checks ACCESS_READ_OBJECT), so this works on a
    collection shared read-only, not just on one the caller owns. Note that
    being a rodsadmin does *not* substitute for that read ACL: the check is a
    literal catalog lookup, so `rods` cannot bundle another user's collection.

    `tar_lpath` must be somewhere the caller can write, which is generally
    *not* inside `coll_path`, and must not already exist - a failed bundle can
    leave a locked zero-byte object behind that even `irm -f` refuses to
    remove, so callers should use a fresh unique name per attempt rather than
    a stable one they retry into.

    Audited as a read of the *collection*, with the file count: the tar is
    fetched afterwards as a single data object, so without this the PHI trail
    would record one download of an opaque .tar and nothing about the patient
    data actually leaving the zone.
    """
    if not (path_is_rule_safe(coll_path) and path_is_rule_safe(tar_lpath)):
        raise IrodsError(
            "This folder's name contains characters that can't be used in a "
            "server-side bundle."
        )
    # Empty resource = the zone's default, rather than pinning a resource
    # name this module would then have to keep in step with the deployment.
    rule_text = f'msiTarFileCreate("{tar_lpath}", "{coll_path}", "", "");'
    return _audited(
        "download", "collection", coll_path,
        lambda: execute_rule(rule_text, token=token),
        actor=actor,
        count=file_count,
        detail=f"server-side tar bundle -> {tar_lpath}",
    )


# --------------------------------------------------------------------------
# Users and groups (rodsadmin only - enforced by iRODS, not by this module)
# --------------------------------------------------------------------------

def _all_principals():
    """Every user and group in the zone, with its type.

    Deliberately GenQuery rather than the users-groups "users"/"groups"
    operations: those require rodsadmin, while this works for any logged-in
    person, so ordinary users can pick someone to share a collection with.
    """
    return [
        {"name": row[0], "type": row[1], "zone": row[2]}
        for row in genquery("select USER_NAME, USER_TYPE, USER_ZONE")
    ]


def list_users():
    users = [p for p in _all_principals() if p["type"] != "rodsgroup"]
    users.sort(key=lambda user: user["name"].lower())
    return users


def list_groups():
    groups = [p["name"] for p in _all_principals() if p["type"] == "rodsgroup"]
    return sorted(groups, key=str.lower)


def user_type(name):
    """rodsuser / rodsadmin / groupadmin / rodsgroup, or None if no such user."""
    for principal in _all_principals():
        if principal["name"] == name:
            return principal["type"]
    return None


# Who is *able* to reach patient data is audited alongside who actually did:
# granting someone an account or adding them to a group that holds a share is
# a change to the set of people with access, and reconstructing that set as it
# stood on some past date is impossible from the access records alone.

def create_user(name, user_type="rodsuser"):
    return _audited(
        "account_admin", "user", name,
        lambda: _post(
            "users-groups",
            {"op": "create_user", "name": name, "zone": IRODS_ZONE, "user-type": user_type},
        ),
        detail=f"created as {user_type}",
    )


def remove_user(name):
    return _audited(
        "account_admin", "user", name,
        lambda: _post(
            "users-groups", {"op": "remove_user", "name": name, "zone": IRODS_ZONE}
        ),
        detail="removed",
    )


def set_user_type(name, new_type):
    return _audited(
        "account_admin", "user", name,
        lambda: _post(
            "users-groups",
            {"op": "set_user_type", "name": name, "zone": IRODS_ZONE, "new-user-type": new_type},
        ),
        detail=f"type -> {new_type}",
    )


def create_group(name):
    return _audited(
        "account_admin", "group", name,
        lambda: _post("users-groups", {"op": "create_group", "name": name}),
        detail="created",
    )


def remove_group(name):
    return _audited(
        "account_admin", "group", name,
        lambda: _post("users-groups", {"op": "remove_group", "name": name}),
        detail="removed",
    )


def add_to_group(user, group):
    return _audited(
        "account_admin", "group", group,
        lambda: _post(
            "users-groups",
            {"op": "add_to_group", "user": user, "zone": IRODS_ZONE, "group": group},
        ),
        detail=f"added {user}",
    )


def remove_from_group(user, group):
    return _audited(
        "account_admin", "group", group,
        lambda: _post(
            "users-groups",
            {"op": "remove_from_group", "user": user, "zone": IRODS_ZONE, "group": group},
        ),
        detail=f"removed {user}",
    )


def groups_of(username):
    """Groups this person belongs to.

    Needed to read an ACL correctly: iRODS lists the *group* on a collection
    shared with a group, never the individual members, so matching a stat's
    permissions against the username alone reports "read-only" for someone who
    can in fact write.
    """
    rows = genquery(f"select USER_GROUP_NAME where USER_NAME = '{_quote(username)}'")
    return {row[0] for row in rows if row and row[0] != username}


def effective_permission(stat, username, groups=()):
    """Strongest permission `username` holds on a collection, via self or group."""
    ranking = {"read_object": 1, "read": 1, "modify_object": 2, "write": 2, "own": 3}
    holders = {username} | set(groups)

    best = None
    for entry in stat.get("permissions") or []:
        if entry.get("name") in holders:
            perm = entry.get("perm")
            if best is None or ranking.get(perm, 0) > ranking.get(best, 0):
                best = perm
    return best


def group_members(group):
    """Members of a group, via GenQuery - the HTTP API has no list-members op."""
    rows = genquery(
        f"select USER_NAME where USER_GROUP_NAME = '{_quote(group)}'",
    )
    members = [row[0] for row in rows if row and row[0] != group]
    return sorted(members, key=str.lower)
