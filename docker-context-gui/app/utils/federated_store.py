"""JSON-file-backed registry of federated learning instances (this local
instance + registered remotes), plus per-remote token issuing/verification
for the machine-to-machine calls between instances.

Follows this codebase's existing convention of plain JSON state on disk
(see batch_jobs.py's per-job JSON) rather than introducing the first
SQLAlchemy-backed table in this app -- consistent with everything else
here, and there's exactly one writer (this module, behind a lock) so the
usual JSON-file race isn't a concern.

Token model -- EVERY remote
gets its OWN unique token, not one secret shared by every partner. A prior
version of this file used one shared secret per local instance; that made
it impossible to tell WHICH registered partner was calling (a token could
only prove "some trusted partner," never identify one), which silently
broke per-partner allowed_dags checks. Fixed by minting a fresh token per
remote at registration time -- see add_remote_instance.

Each pairwise relationship therefore has two distinct tokens, one per
direction:
  - `incoming_token_hash` on a remote's record = the token WE minted for
    THAT remote to present when calling US. Stored hashed, like a
    password -- we only ever check a match, never need it back.
  - `outgoing_token` on that same record = the token THEY minted for US to
    present when calling THEM. Plaintext (we have to send it), pasted in
    when registering them (if already known) or added later via PUT once
    they share it back.

find_by_incoming_token resolving a specific remote record (not ambiguous
between "someone" and "the local instance") is what lets a receiving
instance hand its own local DAG the exact right credential for calling
back the caller -- see federated_instances.incoming_job's callback_token
injection -- without the caller ever having to invent or transmit that
credential itself.
"""
import hashlib
import json
import os
import secrets
import stat
import threading
import uuid
from datetime import datetime, timezone
from app.utils.storage_paths import API_STORAGE_DIR

STORE_PATH = os.environ.get(
    "FEDERATED_STORE_PATH",
    f"{API_STORAGE_DIR}/federated/instances.json",
)
# Narrow-purpose token, NOT shared with any remote: only ever presented by
# this instance's own coordinator DAG calling back into its own Flask app
# (uploading a round result, fetching the recipe/global model) when this
# instance is training on its own local data too (federated_rounds.py's
# start_round, train_local_too) -- a same-instance HTTP self-call, not a
# federation relationship. See get_self_call_token.
SELF_CALL_TOKEN_PATH = os.environ.get(
    "FEDERATED_SELF_CALL_TOKEN_PATH",
    f"{API_STORAGE_DIR}/federated/self_call_token.secret",
)
LOCAL_INSTANCE_NAME = os.environ.get("FEDERATED_LOCAL_INSTANCE_NAME", "interact-local")

_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _load():
    if not os.path.isfile(STORE_PATH):
        return {"instances": {}}
    with open(STORE_PATH) as f:
        return json.load(f)


def _save(data):
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    tmp_path = STORE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # this file holds plaintext outgoing_tokens
    os.replace(tmp_path, STORE_PATH)  # atomic, matches batch_jobs.py's write pattern


def get_self_call_token():
    """Generated once, kept only on disk here (chmod 600), never displayed
    or sent to anyone -- this instance is both the only issuer and the
    only ever caller of it."""
    os.makedirs(os.path.dirname(SELF_CALL_TOKEN_PATH), exist_ok=True)
    if not os.path.isfile(SELF_CALL_TOKEN_PATH):
        with _lock:
            if not os.path.isfile(SELF_CALL_TOKEN_PATH):  # re-check inside the lock
                with open(SELF_CALL_TOKEN_PATH, "w") as f:
                    f.write(secrets.token_urlsafe(32))
                os.chmod(SELF_CALL_TOKEN_PATH, stat.S_IRUSR | stat.S_IWUSR)
    with open(SELF_CALL_TOKEN_PATH) as f:
        return f.read()


def ensure_local_instance():
    """Idempotent: creates this instance's own registry row on first use.
    No token fields -- this row is never authenticated against (nothing
    calls itself through this record; see get_self_call_token for the one
    same-instance HTTP call that does exist)."""
    with _lock:
        data = _load()
        for inst in data["instances"].values():
            if inst["role"] == "local":
                return inst

        inst = {
            "id": uuid.uuid4().hex,
            "role": "local",
            "name": LOCAL_INSTANCE_NAME,
            "base_url": os.environ.get("FEDERATED_LOCAL_BASE_URL", ""),
            "allowed_dags": [],
            "created_at": _now(),
            "updated_at": _now(),
        }
        data["instances"][inst["id"]] = inst
        _save(data)
        return inst


def get_local_instance():
    return ensure_local_instance()


def update_local_instance(**fields):
    """name and base_url are otherwise frozen at whatever
    FEDERATED_LOCAL_INSTANCE_NAME / FEDERATED_LOCAL_BASE_URL happened to
    be the FIRST time this instance's record was created (ensure_local_
    instance only ever creates it once, never re-reads the env after)
    -- this is the only way to change either afterward, e.g. if the env
    var was wrong/generic (the "interact-local" default) at first use."""
    inst = ensure_local_instance()
    return update_instance(inst["id"], **fields)


def list_instances():
    data = _load()
    return sorted(data["instances"].values(), key=lambda i: (i["role"] != "local", i["name"]))


def get_instance(instance_id):
    return _load()["instances"].get(instance_id)


def find_by_incoming_token(token):
    """Used to authenticate AND identify an incoming request: which
    specific registered remote handed us this exact token? Each remote
    has its own, so a match unambiguously names the caller (see module
    docstring). O(n) over registered instances -- fine at federation
    scale (a handful to dozens of sites, not thousands).

    Also recognizes the self-call token (see get_self_call_token) and
    resolves it to the local instance's own record -- the one legitimate
    case of this instance calling itself (training on its own local data
    too, see federated_rounds.py's train_local_too). That record's
    allowed_dags is always empty, which is fine: nothing on the self-call
    path checks it."""
    if token == get_self_call_token():
        return ensure_local_instance()
    token_hash = _hash_token(token)
    for inst in _load()["instances"].values():
        if inst.get("incoming_token_hash") == token_hash:
            return inst
    return None


def add_remote_instance(name, base_url, allowed_dags=None, risk_acknowledged_by=None,
                         their_outgoing_token=""):
    """Register a remote instance we now trust to call. Mints a FRESH
    token for this specific remote to use calling us -- returned in the
    result's `incoming_token_for_them` field, shown to the admin exactly
    once, for them to send to that remote's admin out-of-band. That
    remote's admin pastes it into THEIR add_remote_instance call for us
    (as their_outgoing_token there).

    their_outgoing_token is the reverse: the token THAT remote already
    generated for US to use calling THEM, if the admin already has it
    (e.g. mid-way through a live handshake call). Often unknown yet --
    leave "" and fill it in later with update_instance(outgoing_token=...)
    once they share theirs back.
    """
    if not risk_acknowledged_by:
        raise ValueError("risk_acknowledged_by is required -- see the FL risk disclosure")

    incoming_token_for_them = secrets.token_urlsafe(32)
    with _lock:
        data = _load()
        inst = {
            "id": uuid.uuid4().hex,
            "role": "remote",
            "name": name,
            "base_url": base_url.rstrip("/"),
            "incoming_token_hash": _hash_token(incoming_token_for_them),
            "outgoing_token": their_outgoing_token,
            "allowed_dags": allowed_dags or [],
            "risk_acknowledged": True,
            "risk_acknowledged_by": risk_acknowledged_by,
            "risk_acknowledged_at": _now(),
            # reminder_days is the recommended rotation cadence for this
            # pairing (see rotate_incoming_token) -- a UI hint only, nothing
            # here auto-expires a token, so an overdue rotation never breaks
            # a call by surprise.
            "reminder_days": 30,
            "token_issued_at": _now(),
            "created_at": _now(),
            "updated_at": _now(),
        }
        data["instances"][inst["id"]] = inst
        _save(data)
        return {**inst, "incoming_token_for_them": incoming_token_for_them}


def update_instance(instance_id, **fields):
    with _lock:
        data = _load()
        inst = data["instances"].get(instance_id)
        if not inst:
            raise KeyError(instance_id)
        inst.update(fields)
        inst["updated_at"] = _now()
        _save(data)
        return inst


def rotate_incoming_token(instance_id):
    """Mints a fresh token for an existing remote to use calling us,
    replacing the old one -- which stops matching the instant this saves
    (find_by_incoming_token only ever checks the current hash), same
    end result as deleting and re-adding the instance, but keeps
    everything else (base_url, allowed_dags, reminder_days, risk
    acknowledgement) intact. Returns the new plaintext token once, same
    one-time-display contract as add_remote_instance's incoming_token_for_them
    -- the caller must show it to the admin now; it is never recoverable
    from the store afterward."""
    new_token = secrets.token_urlsafe(32)
    with _lock:
        data = _load()
        inst = data["instances"].get(instance_id)
        if not inst:
            raise KeyError(instance_id)
        inst["incoming_token_hash"] = _hash_token(new_token)
        inst["token_issued_at"] = _now()
        inst["updated_at"] = _now()
        _save(data)
    return new_token


def delete_instance(instance_id):
    with _lock:
        data = _load()
        data["instances"].pop(instance_id, None)
        _save(data)
