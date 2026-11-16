# app/security.py
#
# The admin_required decorator lives in app/utils/roles.py, which is what every
# route imports. This module holds only the group/role parsing it and the
# base.html context processor share.
from ast import literal_eval

# We'll import `oidc` where used to avoid circular imports.
def _normalize_groups(raw_groups):
    if not raw_groups:
        return []
    if isinstance(raw_groups, str):
        try:
            parsed = literal_eval(raw_groups)
            raw_groups = parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            raw_groups = [raw_groups]
    return [g.strip().lower().lstrip("/") for g in raw_groups if isinstance(g, str)]

def current_user_is_admin(oidc):
    if not getattr(oidc, "user_loggedin", False):
        return False

    groups = _normalize_groups(oidc.user_getfield("groups"))
    realm = oidc.user_getfield("realm_access") or {}
    roles = [r.strip().lower() for r in (realm.get("roles", []) or []) if isinstance(r, str)]

    return ("admin" in groups) or ("admin" in roles)
