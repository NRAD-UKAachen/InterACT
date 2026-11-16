""" from flask_oidc import OpenIDConnect

oidc = OpenIDConnect() """

# app/oidc_auth.py
import os
import threading
import time

import requests

DISABLE = os.getenv("DISABLE_OIDC", "0") == "1"

if not DISABLE:
    from flask_oidc import OpenIDConnect
    oidc = OpenIDConnect()
else:
    # No‑auth drop-in compatible with common Flask-OIDC calls
    class _DummyOIDC:
        def init_app(self, app):
            pass

        # decorator becomes a no-op
        def require_login(self, f):
            return f

        def user_loggedin(self):
            return True

        def user_getinfo(self, fields=None):
            # Return sensible defaults for templates/routes that expect these
            base = {
                "preferred_username": "devuser",
                "email": "dev@example.com",
                "name": "Dev User",
                "given_name": "Dev",
                "family_name": "User",
                "groups": [],
            }
            if not fields:
                return base
            return {k: base.get(k) for k in fields}

        def user_getfield(self, field, default=None):
            return self.user_getinfo().get(field, default)

        # Some code paths may touch these; keep them harmless
        def get_access_token(self):
            return None

        def token_type(self):
            return "Bearer"

        def logout(self):
            pass

    oidc = _DummyOIDC()


def token_refresh_bundle():
    """Everything needed to refresh the current person's access token from
    outside a request.

    Exists for docker-context-gui's batch iRODS stage-in (app/routes/batch.py),
    which can run for many minutes on a large cohort - long enough for the
    short-lived Keycloak access token to expire mid-download. The iRODS HTTP
    API cannot tell an expired token from a nonexistent account and reports
    both as "no iRODS account" (see app/utils/irods_client.py's IrodsNoAccount),
    which is what that looks like to the end user without this.

    Must be called from within a request (reads the Flask session and app
    config) - the caller hands the result to a background thread, which
    calls refresh_access_token() on it once the access token is close to
    expiring. Returns None when there's nothing to refresh with (OIDC
    disabled, or this session has no refresh token) - the caller should then
    fall back to a single static token for the whole stage-in, same as
    before this existed.
    """
    if DISABLE:
        return None
    from flask import current_app, session

    token = session.get("oidc_auth_token") or {}
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        return None
    try:
        token_endpoint = oidc.oauth.oidc.load_server_metadata()["token_endpoint"]
    except Exception:
        return None

    return {
        "access_token": token.get("access_token"),
        "refresh_token": refresh_token,
        "expires_at": token.get("expires_at"),
        "token_endpoint": token_endpoint,
        "client_id": current_app.config["OIDC_CLIENT_ID"],
        "client_secret": current_app.config["OIDC_CLIENT_SECRET"],
    }


def refresh_access_token(bundle):
    """Exchange bundle['refresh_token'] for a new access token.

    A direct POST to the token endpoint, not flask_oidc's own
    ensure_active_token(): that path writes the refreshed token into the
    Flask session via an update_token hook registered on the shared OAuth
    client, which requires a request context - exactly what the caller (a
    background thread) doesn't have.

    Returns a new bundle in the same shape as token_refresh_bundle(). Raises
    requests.exceptions.RequestException on failure; callers should keep
    using the old (soon-to-expire) token rather than fail a whole
    in-progress stage-in over one missed refresh.
    """
    response = requests.post(
        bundle["token_endpoint"],
        data={
            "grant_type": "refresh_token",
            "refresh_token": bundle["refresh_token"],
            "client_id": bundle["client_id"],
            "client_secret": bundle["client_secret"],
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    new_bundle = dict(bundle)
    new_bundle["access_token"] = payload["access_token"]
    new_bundle["refresh_token"] = payload.get("refresh_token", bundle["refresh_token"])
    expires_in = payload.get("expires_in")
    new_bundle["expires_at"] = time.time() + expires_in if expires_in else None
    return new_bundle


# Refresh a long-running download's access token this long before it actually
# expires, so a file read never races the expiry itself. See RefreshingToken.
TOKEN_REFRESH_MARGIN_SECONDS = 60


class RefreshingToken:
    """An access token for a long-running iRODS transfer, refreshed in place
    as it nears expiry.

    Most iRODS calls borrow the caller's token for one quick request, but a
    batch stage-in (app/routes/batch.py) or a folder ZIP download
    (app/routes/irods_browser.py) can run far longer than a Keycloak access
    token lives - long enough that every remaining read gets a 401 that
    irods_client.py reports as "no iRODS account" (it can't tell an expired
    token from a genuinely missing one). This keeps the token alive for the
    whole transfer instead.

    Seed it with token_refresh_bundle(), captured inside the request. Falls
    back to never refreshing - a single static token - when the bundle has
    no "expires_at" (OIDC disabled, or this session has no refresh token),
    so a missing refresh token degrades gracefully instead of breaking the
    transfer entirely. Thread-safe: batch.py shares one across a worker pool.
    """

    def __init__(self, bundle):
        self._lock = threading.Lock()
        self._bundle = bundle

    def get(self):
        with self._lock:
            return self._bundle["access_token"]

    def refresh_if_needed(self):
        with self._lock:
            bundle = self._bundle
            if not bundle.get("expires_at"):
                return
            if time.time() < bundle["expires_at"] - TOKEN_REFRESH_MARGIN_SECONDS:
                return
            try:
                self._bundle = refresh_access_token(bundle)
            except requests.exceptions.RequestException:
                # Leave the old token in place - a read that then hits 401
                # surfaces its own clear error, which beats aborting a whole
                # in-progress transfer over one missed refresh.
                pass
