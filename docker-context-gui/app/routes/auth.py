# app/routes/auth.py
from flask import Blueprint, redirect, url_for, current_app, session, request, render_template, g
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
import json
import requests
import time

# Use the shared OIDC instance wired up in your factory
from app.oidc_auth import oidc, DISABLE as OIDC_DISABLED

auth_bp = Blueprint("auth", __name__)

# How many not-yet-completed logins we keep state for. Authlib writes one
# `_state_<name>_<state>` entry per authorization attempt and only ever pops
# the one whose callback comes back, so every abandoned login (closed tab,
# back button, timed-out Keycloak form) leaks an entry that lives until it
# expires. See _prune_authlib_states().
_MAX_PENDING_AUTH_STATES = 5


def _load_oidc_client_info():
    """Read issuer & client_id from client_secrets.json."""
    with open(current_app.config["OIDC_CLIENT_SECRETS"], "r") as f:
        data = json.load(f)["web"]
    issuer = data["issuer"].rstrip("/")
    client_id = data["client_id"]
    return issuer, client_id


def oidc_redirect_uri():
    """
    Pick the OIDC callback URI for *this* request from an allow-list.

    The URI can't simply be built from `request.host`: that is
    attacker-controlled, so a forged Host header would make us hand Keycloak
    a redirect_uri pointing at someone else's server, and the realm
    registers a wildcard rather than catching it. The host must appear in
    OIDC_ALLOWED_HOSTS; anything else falls back to the canonical URI.

    Computed per request and returned, never stashed in app.config -- that
    is shared by every thread of this (threaded=True) server, so a stored
    value belonging to one user's login can be read by another's.

    Authlib records the redirect_uri it used in the per-state session data
    and replays it at token-exchange time, so the callback stays consistent
    with whatever this returns.
    """
    scheme = "https" if request.is_secure else "http"
    host = request.host  # e.g. "interact.example.org:5005" or "127.0.0.1:5005"

    if host in current_app.config["OIDC_ALLOWED_HOSTS"]:
        return f"{scheme}://{host}/oidc_callback"
    return current_app.config["OIDC_CANONICAL_REDIRECT_URI"]


def _prune_authlib_states():
    """
    Drop expired and surplus Authlib authorization-state entries.

    Authlib clears these only on a *successful* callback, and only ones
    already past their expiry -- so a user who abandons logins accumulates
    them indefinitely. That used to be fatal: the entries rode in the
    client-side session cookie, ~80 bytes each, and pushed it past the
    browsers' 4093-byte limit until the cookie was dropped and login looped.
    Sessions are server-side now, but unbounded growth is still worth
    capping, and it keeps the loop from re-arming if the storage ever moves
    back.
    """
    now = time.time()
    live = []

    for key in list(session):
        if not key.startswith("_state_"):
            continue
        value = session.get(key)
        exp = value.get("exp") if isinstance(value, dict) else None
        if not exp or exp < now:
            session.pop(key, None)  # expired, or not a shape we recognise
        else:
            live.append((exp, key))

    # Keep the newest few; the rest are abandoned attempts.
    if len(live) > _MAX_PENDING_AUTH_STATES:
        live.sort()  # soonest expiry (oldest attempt) first
        for _, key in live[: len(live) - _MAX_PENDING_AUTH_STATES]:
            session.pop(key, None)

""" @auth_bp.route("/logout")
def logout():
    '''Clear local state AND end Keycloak session, then show logged out page.'''
    # Get the ID token BEFORE clearing the session (needed for Keycloak logout)
    # flask-oidc may store it under different keys depending on version
    id_token = (
        session.get("oidc_id_token") or
        session.get("oidc_id_token_cookie") or
        session.get("id_token")
    )

    # Debug: print session keys to see what's available
    print(f"🔐 Session keys before logout: {list(session.keys())}")
    print(f"🔑 ID token found: {bool(id_token)}")

    # Clear Flask session
    session.clear()

    # Build Keycloak logout URL to end the SSO session
    issuer, client_id = _load_oidc_client_info()
    logout_endpoint = f"{issuer}/protocol/openid-connect/logout"

    # Try to get the proper endpoint from discovery
    try:
        conf = requests.get(f"{issuer}/.well-known/openid-configuration", timeout=5).json()
        logout_endpoint = conf.get("end_session_endpoint", logout_endpoint)
    except Exception:
        pass

    # Redirect to Keycloak logout, then back to our logged_out page
    post_logout_redirect = url_for("auth.logged_out", _external=True)
    params = {
        "client_id": client_id,
        "post_logout_redirect_uri": post_logout_redirect
    }

    # Include id_token_hint if available (required by Keycloak to honor the redirect)
    if id_token:
        params["id_token_hint"] = id_token

    logout_url = f"{logout_endpoint}?{urlencode(params)}"
    print(f"🚪 Logout URL: {logout_url[:200]}...")  # truncated for readability
    print(f"📍 Post-logout redirect: {post_logout_redirect}")

    return redirect(logout_url)
 """

@auth_bp.route("/auth/logout")
def logout():
    """Clear local state AND end Keycloak SSO session."""
    from flask import make_response

    # Get id_token from flask-oidc's session storage BEFORE clearing
    id_token = None
    token_data = session.get('oidc_auth_token')
    if token_data and isinstance(token_data, dict):
        id_token = token_data.get('id_token')

    # Build Keycloak logout URL
    issuer, client_id = _load_oidc_client_info()
    logout_endpoint = f"{issuer}/protocol/openid-connect/logout"

    # Build params
    post_logout_redirect = url_for('auth.logged_out', _external=True)
    params = {
        "client_id": client_id,
        "post_logout_redirect_uri": post_logout_redirect
    }

    # Include id_token_hint if available (required for Keycloak to honor redirect)
    if id_token:
        params["id_token_hint"] = id_token

    logout_url = f"{logout_endpoint}?{urlencode(params)}"

    # Clearing the session is what actually logs the user out: with
    # server-side sessions this drops the stored token set, and Flask-Session
    # expires the cookie for us. The explicit delete_cookie() calls that used
    # to sit here named flask-oidc 1.x cookies (oidc_id_token,
    # oidc_access_token, oidc_id_token_cookie) that 2.x never sets.
    session.clear()

    return make_response(redirect(logout_url))

def _strip_embedded_param(url):
    """
    Drop `?embedded=1` from a post-login return URL.

    `embedded=1` only means "render without the sidebar because you're inside
    the nav-shell's iframe" (see inject_embedded in create_app) - it has no
    meaning once we've already broken out of that iframe to show Keycloak's
    login page (see break_out_of_iframe_for_cross_origin_redirects), and left
    in place it would land the user back on the bare chrome-less page instead
    of the full portal.
    """
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "embedded"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _same_site_only(url):
    """
    Return `url` only if it stays on this portal, else None.

    flask-oidc's validate_return_url() lets "//evil.example/x" through as a
    "relative" path, and browsers read that as scheme-relative, i.e. another
    site -- an open redirect right after a genuine login. Accept only an
    absolute URL on this request's host, or a path that starts with exactly one
    "/" and contains no backslash (some browsers treat "/\\host" like "//host").
    """
    parts = urlsplit(url)
    if parts.scheme or parts.netloc:
        return url if parts.scheme in ("http", "https") and parts.netloc == request.host else None
    if url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return None


def _start_authorization(**extra_params):
    """
    Kick off the OIDC authorization-code flow for this request.

    Mirrors flask_oidc.views.login_view, but resolves the callback URI per
    request via oidc_redirect_uri() and lets callers pass extra authorization
    parameters (e.g. prompt=login). `session["next"]` is where flask-oidc's
    /authorize view sends the browser once the exchange succeeds.
    """
    from flask_oidc.views import validate_return_url

    next_url = request.args.get("next") or url_for("home.home", _external=True)
    next_url = _strip_embedded_param(next_url)
    next_url = _same_site_only(next_url) or url_for("home.home", _external=True)
    session["next"] = validate_return_url(next_url, request.url_root)

    _prune_authlib_states()

    return g._oidc_auth.authorize_redirect(oidc_redirect_uri(), **extra_params)


@auth_bp.route("/login")
def login():
    """
    Start login and return to ?next=... (or home) afterwards.

    This route has to be registered before flask-oidc's blueprint, which
    also claims /login -- see create_app(). If it isn't, flask-oidc's own
    view answers instead and the redirect URI silently reverts to the single
    canonical host.
    """
    if OIDC_DISABLED:
        return redirect(_same_site_only(request.args.get("next") or "") or url_for("home.home"))
    return _start_authorization()


@auth_bp.route("/auth/logged-out")
def logged_out():
    """Show logged out page and ensure session is fully cleared."""
    from flask import make_response

    # Clear Flask session (don't add flash - the page itself shows logout message)
    session.clear()

    # Render the standalone logged out page
    # embedded=True renders base.html without the sidebar: the session is gone,
    # so a nav full of links that would only bounce through Keycloak again is
    # misleading here. The page keeps the app's styling, minus the chrome.
    #
    # No manual delete_cookie() calls here. session.clear() above is enough
    # for our own cookie, and the KEYCLOAK_SESSION / KEYCLOAK_IDENTITY
    # deletions that used to follow were actively harmful: Keycloak sets
    # those at path=/realms/interact-realm/, delete_cookie defaults to
    # path=/, and cookies are not scoped by port -- so rather than deleting
    # Keycloak's cookies this planted *empty duplicates* of them at the root
    # path of a host we share with Keycloak, which the browser then sent
    # alongside the real ones. Ending the Keycloak SSO session is logout()'s
    # job, via the end_session_endpoint with an id_token_hint.
    return make_response(
        render_template(
            "logged_out.html", title="Logged Out - InterACT", embedded=True
        )
    )

@auth_bp.route("/auth/change-password")
@oidc.require_login
def change_password():
    """Send the user to Keycloak's hosted 'Update Password' screen (an
    Application-Initiated Action), then return to the app."""
    return _start_authorization(kc_action="UPDATE_PASSWORD")


@auth_bp.route("/auth/force-login")
def force_login():
    """
    Force the Keycloak login screen, ignoring any cached SSO session.

    prompt=login goes through Authlib's authorize_redirect so that state and
    nonce are stored and checked. This used to hand-build the authorization
    URL with redirect_uri=/ instead: nothing at / exchanges an authorization
    code, so the flow dead-ended there with a code in the URL bar and no
    session. (The config key it was working around,
    OIDC_EXTRA_AUTH_REQUEST_PARAMS, is a flask-oidc 1.x setting that 2.x
    does not read at all.)
    """
    if OIDC_DISABLED:
        return redirect(url_for("home.home"))
    return _start_authorization(prompt="login")


