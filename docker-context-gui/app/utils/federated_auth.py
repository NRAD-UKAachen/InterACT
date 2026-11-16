"""Bearer-token auth for instance-to-instance FL calls (as opposed to
app.utils.roles.admin_required, which gates the human/browser admin
pages). Mirrors the shape of that decorator but checks a registered
remote instance's token instead of a Keycloak session."""
from functools import wraps

from flask import g, jsonify, request

from app.utils.federated_store import find_by_incoming_token


def require_instance_token(view):
    """Validates `Authorization: Bearer <token>` against the registered
    remote instances (see federated_store.find_by_incoming_token). On
    success, sets flask.g.calling_instance to that instance's record so
    the view can check allowed_dags/allowed use before acting.

    Errors are JSON {"error": ...}, not Flask's HTML abort pages, so the
    calling instance can show the reason instead of a bare status code."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "missing bearer token"}), 401
        token = auth_header[len("Bearer "):]
        inst = find_by_incoming_token(token)
        if inst is None:
            return jsonify({
                "error": "this site does not recognize the caller's token. "
                         "Re-register the two sites with each other under "
                         "Federated Learning > Instances."
            }), 403
        g.calling_instance = inst
        return view(*args, **kwargs)
    return wrapper
