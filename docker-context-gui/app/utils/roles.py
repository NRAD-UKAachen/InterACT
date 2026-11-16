# app/utils/roles.py
from functools import wraps
from flask import redirect, url_for, flash, request
from app.oidc_auth import oidc

def _is_admin():
    # logged in?
    if not oidc.user_loggedin:
        return False

    # normalize groups
    raw_groups = oidc.user_getfield("groups") or []
    if isinstance(raw_groups, str):
        try:
            import ast
            parsed = ast.literal_eval(raw_groups)
            raw_groups = parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            raw_groups = [raw_groups]
    groups = [str(g).strip().lower().lstrip("/") for g in raw_groups]

    # normalize roles
    realm = oidc.user_getfield("realm_access") or {}
    roles = [str(r).strip().lower() for r in (realm.get("roles", []) or [])]

    return ("admin" in groups) or ("admin" in roles)

def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not oidc.user_loggedin:
            # send to your login route; preserve where they were going
            return redirect(url_for("auth.login", next=request.url))
        if not _is_admin():
            flash("Admin access required.", "error")
            return redirect(url_for("home.home"))
        return view(*args, **kwargs)
    return wrapper