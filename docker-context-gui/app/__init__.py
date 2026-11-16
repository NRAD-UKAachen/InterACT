import json
import os
from ast import literal_eval
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from flask import Flask, make_response, request
from markupsafe import escape

from cachelib.file import FileSystemCache
from flask_session import Session

from .oidc_auth import oidc
from .routes.home import home_bp
from .routes.viewer import viewer_bp
from .routes.auth import auth_bp
from .routes.interactive_segmentation_viewer import interactive_bp
from app.routes.reverse_proxy import proxy_bp
from app.routes.dcm4chee_proxy import dcm4chee_proxy_bp, dcm4chee_redirect_bp
from app.routes.irods_browser import irods_browser_bp
from app.routes.irods_admin import irods_admin_bp
from app.routes.batch import batch_bp
from app.routes.api_proxy import api_proxy_bp
from app.routes.audit_log import audit_bp
from app.routes.job_logs import job_logs_bp
from app.routes.federated_instances import federated_instances_bp
from app.routes.federated_rounds import federated_rounds_bp
from app.routes.federated_training_data import federated_training_data_bp

# --- Safe cookie patch: decode bytes -> str, keep Werkzeug's signature intact ---
import werkzeug.http as wz_http
import werkzeug.sansio.response as wz_resp

def _install_cookie_patch():
    # Avoid double wrapping (Flask reloader)
    if getattr(wz_http.dump_cookie, "__wrapped_by_cookie_patch__", False):
        return
    if getattr(wz_resp.dump_cookie, "__wrapped_by_cookie_patch__", False):
        return

    _orig_http_dump_cookie = wz_http.dump_cookie
    _orig_resp_dump_cookie = wz_resp.dump_cookie

    def _decode_cookie_value(value, charset="utf-8"):
        if isinstance(value, (bytes, bytearray)):
            return value.decode(charset, errors="strict")
        return value

    def _wrap_http_dump_cookie(*args, **kwargs):
        # Werkzeug http.dump_cookie(key, value='', ..., charset='utf-8', ...)
        charset = kwargs.get("charset", "utf-8")
        if len(args) >= 2:
            key, value = args[0], args[1]
            value = _decode_cookie_value(value, charset)
            args = (key, value) + args[2:]
        elif "value" in kwargs:
            kwargs["value"] = _decode_cookie_value(kwargs["value"], charset)
        # Pass through all kwargs (including any new ones like max_size)
        return _orig_http_dump_cookie(*args, **kwargs)

    def _wrap_resp_dump_cookie(*args, **kwargs):
        # Werkzeug sansio.response.dump_cookie(key, value='', ..., charset='utf-8', ...)
        charset = kwargs.get("charset", "utf-8")
        if len(args) >= 2:  # important: 2, not 3
            key, value = args[0], args[1]
            value = _decode_cookie_value(value, charset)
            args = (key, value) + args[2:]
        elif "value" in kwargs:
            kwargs["value"] = _decode_cookie_value(kwargs["value"], charset)
        return _orig_resp_dump_cookie(*args, **kwargs)

    _wrap_http_dump_cookie.__wrapped_by_cookie_patch__ = True
    _wrap_resp_dump_cookie.__wrapped_by_cookie_patch__ = True

    wz_http.dump_cookie = _wrap_http_dump_cookie
    wz_resp.dump_cookie = _wrap_resp_dump_cookie

_install_cookie_patch()
# --- end patch ---


def _timeago(value):
    """iRODS *_MODIFY_TIME is a decimal-seconds-since-epoch string; render it the
    way a file browser would ("2 days ago") instead of a raw timestamp."""
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return "—"
    if ts <= 0:
        return "—"

    delta = datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)
    seconds = delta.total_seconds()
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return "1 min ago" if minutes == 1 else f"{minutes} min ago"
    hours = int(minutes // 60)
    if hours < 24:
        return "1 hr ago" if hours == 1 else f"{hours} hr ago"
    days = delta.days
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _localtime(value, fmt="%Y-%m-%d %H:%M:%S"):
    """A stored UTC audit timestamp, rendered in server-local time.

    Display only - app/utils/audit.py keeps writing UTC, which is what makes
    the trail unambiguous across daylight-saving changes.
    """
    from app.utils.audit import to_local

    local = to_local(value)
    return local.strftime(fmt) if local else (value or "—")


def _filesize(num_bytes):
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


# ---------------------------------------------------------------------------
# This deployment's public identity.
#
# Everything browser-facing is derived from PUBLIC_HOST and the port variables
# in the root .env -- the OIDC redirect URIs, the CORS origins, and the links
# the templates render. Moving the platform to another machine is a change to
# PUBLIC_HOST and nothing else.
#
# The defaults describe a single-machine localhost install, which is what a
# fresh clone gets. They are deliberately NOT this or any other institution's
# hostname: a wrong-but-local default fails visibly on the machine in front of
# you, where a stale real hostname fails by silently pointing at somebody
# else's server.
# ---------------------------------------------------------------------------
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "localhost")
PUBLIC_SCHEME = os.environ.get("PUBLIC_SCHEME", "http")
PORT_PORTAL = os.environ.get("PORT_PORTAL", "5005")
PORT_OHIF = os.environ.get("PORT_OHIF", "1081")


def _base_url(env_name, port):
    """The public URL of one component, from the environment or composed."""
    return os.environ.get(
        env_name, f"{PUBLIC_SCHEME}://{PUBLIC_HOST}:{port}"
    ).rstrip("/")


portal_base_url = _base_url("PORTAL_BASE_URL", PORT_PORTAL)
ohif_base_url = _base_url("OHIF_BASE_URL", PORT_OHIF)
dcm4chee_admin_base_url = _base_url(
    "DCM4CHEE_ADMIN_BASE_URL", os.environ.get("PORT_DCM4CHEE_ADMIN", "1082")
)


def create_app():
    app = Flask(__name__, static_folder="static")

    # Public URLs the templates build links and iframe sources from, so no
    # template has to know a hostname. See the module-level block above.
    @app.context_processor
    def inject_public_urls():
        return {
            "PORTAL_BASE_URL": portal_base_url,
            "OHIF_BASE_URL": ohif_base_url,
            "DCM4CHEE_ADMIN_BASE_URL": dcm4chee_admin_base_url,
        }

    # Behind the TLS terminator, the app only learns the
    # request was HTTPS from X-Forwarded-Proto. Without this, request.is_secure
    # stays False, Secure cookies are never honoured as such and the OIDC
    # redirect_uri is built as http:// even on an https:// deployment.
    # Gated on an env flag: trusting these headers when NOT behind a proxy
    # would let a client spoof its own scheme and source IP.
    if os.environ.get("BEHIND_TLS_PROXY", "0") == "1":
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1
        )

    from flask_cors import CORS
    # Origins allowed to call this app cross-origin -- the OHIF viewer.
    # Env-configurable so the switch to https:// origins is a config change.
    ohif_origins = [
        o.strip()
        for o in os.environ.get("CORS_ALLOWED_ORIGINS", ohif_base_url).split(",")
        if o.strip()
    ]
    CORS(app, resources={
        r"/api/*": {"origins": ohif_origins},
        # The authenticated front door for the batch/segmentation API.
        # supports_credentials is what lets OHIF's fetches send the session
        # cookie; it requires an explicit origin list (never "*").
        r"/api-proxy/*": {
            "origins": ohif_origins,
            "supports_credentials": True,
            # download_series / download_series_nifti send the real filename
            # via Content-Disposition; without exposing it, cross-origin JS
            # can't read the header (see series-webhooks' parseFilename) and
            # falls back to an extensionless name.
            "expose_headers": ["Content-Disposition"],
        },
    })

    # Session signing key. Must come from the environment -- it signs the
    # session cookie that carries the logged-in identity, so a value baked
    # into source (as this used to be) lets anyone with repo read access
    # forge an admin session. No default: refuse to boot rather than run on
    # a guessable key.
    secret_key = os.environ.get("FLASK_SECRET_KEY")
    if not secret_key:
        raise RuntimeError(
            "FLASK_SECRET_KEY is not set. Generate one with "
            "`python -c 'import secrets; print(secrets.token_urlsafe(48))'` and "
            "pass it via the environment (see docker-compose.yml / .env.example). "
            "It must NOT be the same value as the Keycloak client secret."
        )

    # TLS-dependent cookie flags. Default to the secure setting; only an
    # explicit COOKIES_INSECURE=1 (plain-HTTP local dev) turns it off, so
    # deploying behind TLS doesn't silently keep sending tokens in the clear.
    cookies_insecure = os.environ.get("COOKIES_INSECURE", "0") == "1"
    cookie_secure = not cookies_insecure

    # Hosts the OIDC redirect_uri may be built for (see
    # set_dynamic_redirect_uri below). Colleagues reaching the GUI on their
    # own IP still work -- but only if that IP is listed here, so a forged
    # Host header cannot redirect the authorization code elsewhere.
    canonical_redirect_uri = os.environ.get(
        "OIDC_CANONICAL_REDIRECT_URI", f"{portal_base_url}/oidc_callback"
    )
    allowed_oidc_hosts = {
        h.strip()
        for h in os.environ.get(
            "OIDC_ALLOWED_HOSTS",
            f"{PUBLIC_HOST}:{PORT_PORTAL},localhost:{PORT_PORTAL},"
            f"127.0.0.1:{PORT_PORTAL}",
        ).split(",")
        if h.strip()
    }

    # How long a login survives without activity. Rolling: Flask refreshes
    # the cookie and Flask-Session the server-side record on every request.
    session_lifetime_hours = int(os.environ.get("SESSION_LIFETIME_HOURS", "12"))

    # Keycloak / OIDC settings
    app.config.update({
        "SECRET_KEY": secret_key,
        "OIDC_CLIENT_SECRETS": os.environ.get(
            "OIDC_CLIENT_SECRETS", "/app/client_secrets.json"
        ),
        "OIDC_SCOPES": ["openid", "email", "profile"],
        "OIDC_INTROSPECTION_AUTH_METHOD": "client_secret_post",
        "OIDC_OPENID_REALM": "flask-gui",
        "OIDC_ID_TOKEN_COOKIE_SECURE": cookie_secure,
        "OIDC_USER_INFO_ENABLED": True,

        # Session cookie hardening: never readable from JS (limits token
        # theft via XSS), not sent on cross-site navigations (CSRF), and
        # HTTPS-only unless explicitly running plain-HTTP dev.
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": cookie_secure,

        # Cookies are scoped by host but NOT by port, so every service on
        # this machine (Keycloak, OHIF, dcm4chee, ...) shares one jar with us.
        # A cookie called plain "session" is a name collision waiting to
        # happen -- give ours an app-specific name.
        #
        # Same reasoning one level up: two InterACT stacks on ONE hostname,
        # differing only by port, are two different Flask apps signing with
        # two different FLASK_SECRET_KEYs. Under a shared cookie name each
        # overwrites the other's session and then rejects what it reads back,
        # so logging into one silently logs you out of the other. Hence
        # STACK_NAME in the name -- compose passes it (see docker-compose.yml).
        "SESSION_COOKIE_NAME": os.environ.get(
            "SESSION_COOKIE_NAME", "interact_gui_session"
        ),

        # Sessions live server-side. flask-oidc keeps the whole Keycloak
        # token set (access + refresh + ID token) plus the userinfo profile
        # in the session; with this realm's group/role claims that is ~4.1 KB
        # signed, i.e. OVER the browsers' hard 4093-byte-per-cookie limit.
        # The browser then silently drops the cookie, the token never
        # persists, and every request bounces back to Keycloak -- the
        # "redirected too many times" / "invalid_grant: Code not valid"
        # login loop. Storing the payload on disk and sending only a session
        # id keeps the cookie at ~50 bytes no matter how big the token gets.
        # Do NOT move this back to the default client-side cookie session.
        "SESSION_TYPE": "cachelib",
        "SESSION_CACHELIB": FileSystemCache(
            cache_dir=os.environ.get("SESSION_FILE_DIR", "/app/flask_sessions"),
            threshold=1000,
            # 0600: the session files hold live access/refresh tokens.
            mode=0o600,
        ),
        "SESSION_PERMANENT": True,
        "PERMANENT_SESSION_LIFETIME": timedelta(hours=session_lifetime_hours),

        # IMPORTANT when running behind Docker port mapping. Note the
        # OIDC_ prefix: flask-oidc 2.x reads the unprefixed
        # OVERWRITE_REDIRECT_URI only once, at init_app() time, so setting
        # that one per-request (as this used to) had no effect at all.
        "OIDC_OVERWRITE_REDIRECT_URI": canonical_redirect_uri,

        # Read per-request by auth.oidc_redirect_uri() to pick the callback
        # host from the allow-list. Set once here and never mutated: this is
        # process-global state shared by every thread.
        "OIDC_CANONICAL_REDIRECT_URI": canonical_redirect_uri,
        "OIDC_ALLOWED_HOSTS": allowed_oidc_hosts,

        # Werkzeug 3.1 defaults MAX_FORM_PARTS to 1000, which counts every
        # multipart part (each uploaded file *and* its paired hidden field,
        # e.g. irods_browser.upload()'s/batch.submit()'s "relpath" per
        # "file"). A real DICOM series folder-drop easily carries thousands
        # of individual files, so the stock default 413s (as a bare HTML
        # error page, not JSON) before a route ever runs. MAX_CONTENT_LENGTH
        # is already unlimited (Flask's own default) - drop this cap too.
        "MAX_FORM_PARTS": None,
    })

    # Swap in the server-side session interface BEFORE anything starts
    # writing to `session` (flask-oidc's init_app registers request hooks
    # that do exactly that).
    Session(app)

    # Register our own auth blueprint BEFORE flask-oidc's. Both define a
    # rule for /login, and with identical static rules the first one
    # registered wins -- which used to be flask-oidc's, silently making
    # auth.login() dead code. Ours needs to win so the callback URI can be
    # chosen per request; flask-oidc's /authorize and /logout are
    # unaffected (no path collision).
    app.register_blueprint(auth_bp)

    # Initialize OIDC
    oidc.init_app(app)

    app.jinja_env.filters["timeago"] = _timeago
    app.jinja_env.filters["filesize"] = _filesize
    app.jinja_env.filters["localtime"] = _localtime

    @app.context_processor
    def inject_security():
        if not oidc.user_loggedin:
            return {
                "lowered_groups": [],
                "realm_roles": [],
                "is_admin": False,
                "logged_in": False,
                "username": None,
                "oidc": oidc,
            }

        # Get raw groups
        raw_groups = oidc.user_getfield("groups") or []

        if isinstance(raw_groups, str):
            try:
                parsed = literal_eval(raw_groups)
                raw_groups = parsed if isinstance(parsed, list) else [parsed]
            except Exception:
                raw_groups = [raw_groups]

        groups = [g.strip().lower().lstrip("/") for g in raw_groups if isinstance(g, str)]

        # Get roles
        realm = oidc.user_getfield("realm_access") or {}
        raw_roles = realm.get("roles", [])
        roles = [r.strip().lower() for r in raw_roles if isinstance(r, str)]

        is_admin = ("admin" in groups) or ("admin" in roles)

        username = oidc.user_getfield("preferred_username")

        return {
            "lowered_groups": groups,
            "realm_roles": roles,
            "is_admin": is_admin,
            "logged_in": True,
            "username": username,
            #"user_logged_in": oidc.user_loggedin,
            "oidc": oidc,
        }

    @app.context_processor
    def inject_embedded():
        # Lets any page be loaded chrome-less inside an <iframe> via ?embedded=1
        # (used by the sidebar's client-side navigation shell, see base.html).
        # Routes that already pass their own `embedded` value to render_template
        # take precedence over this default.
        return {"embedded": request.args.get("embedded") == "1"}

    @app.after_request
    def break_out_of_iframe_for_cross_origin_redirects(response):
        # base.html's sidebar nav loads every page (including this one) into
        # a persistent <iframe>, not a top-level navigation. flask-oidc's
        # token-expiry check and require_login both react to an expired
        # Keycloak session with a redirect that eventually points at
        # Keycloak's own hosted login page - a different origin. Keycloak
        # sends frame-ancestors 'self' on that page, so a browser asked to
        # load it inside our iframe just renders a blank frame: the tab's
        # URL never changes, nothing looks wrong, and the stale page behind
        # it stays fully interactive. A same-origin GET refresh bypasses the
        # iframe entirely, which is why "logging in again" only ever worked
        # via a manual page reload.
        #
        # Sec-Fetch-Dest is a fetch-metadata header the browser sets itself
        # (not something a caller can spoof to bypass auth - it only changes
        # how a session-expiry redirect is *delivered*) and stays "iframe"
        # for every hop of a redirect chain still targeting that frame, so it
        # survives even the internal check_token_expiry -> /auth/logout hop
        # that doesn't carry `?embedded=1` forward. Checking both covers
        # older browsers that don't send it.
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location")
        if not location:
            return response
        target_host = urlparse(location).netloc
        if not target_host or target_host == request.host:
            return response
        is_framed_request = (
            request.headers.get("Sec-Fetch-Dest") == "iframe"
            or request.args.get("embedded") == "1"
        )
        if not is_framed_request:
            return response

        target_js = json.dumps(location).replace("<", "\\u003c")
        escaped_html = make_response(
            "<!doctype html>\n"
            "<title>Signing in…</title>\n"
            f"<script>window.top.location.replace({target_js});</script>\n"
            "<p>Signing in… if this page doesn't redirect, "
            f'<a href="{escape(location)}">click here</a>.</p>\n'
        )
        escaped_html.headers["Content-Type"] = "text/html; charset=utf-8"
        return escaped_html

    # NOTE: a `log_request_info` before_request handler used to print
    # request.url plus every header and cookie here. That wrote bearer
    # tokens, the OIDC id_token cookie and the session cookie into the
    # container log on every single request, alongside URLs carrying
    # study/patient identifiers -- i.e. it turned `docker logs` into an
    # unprotected store of credentials and patient data. Do not reinstate
    # it. If you need request tracing, log method + path only, never
    # request.headers, request.cookies or the query string.

    # NOTE: a `set_dynamic_redirect_uri` before_request handler used to pick
    # the OIDC redirect URI here by assigning to app.config on every
    # request. That was wrong twice over: app.config is process-global, so
    # on this threaded server one user's login could overwrite the URI
    # another user's in-flight login was about to read; and it wrote the
    # *unprefixed* OVERWRITE_REDIRECT_URI, which flask-oidc 2.x copies into
    # OIDC_OVERWRITE_REDIRECT_URI once during init_app() and never reads
    # again -- so the per-host behaviour it was written for never actually
    # happened. The allow-list logic now lives in auth.oidc_redirect_uri()
    # and is evaluated per request, touching no shared state.


    # Register Blueprints
    app.register_blueprint(home_bp)
    app.register_blueprint(viewer_bp, url_prefix="/viewer")
    # auth_bp is registered above, ahead of oidc.init_app(), so that its
    # /login rule wins over flask-oidc's.
    app.register_blueprint(interactive_bp)
    app.register_blueprint(proxy_bp)
    app.register_blueprint(dcm4chee_proxy_bp)
    app.register_blueprint(dcm4chee_redirect_bp)  # Handle direct /dcm4chee-arc/ requests
    app.register_blueprint(irods_browser_bp)      # iRODS file browser
    app.register_blueprint(irods_admin_bp)        # iRODS user/permission management (admin only)
    app.register_blueprint(batch_bp)              # Cohort batch processing (FastSurfer, ...)
    app.register_blueprint(api_proxy_bp)          # Authenticated front door for the no-auth batch API
    app.register_blueprint(audit_bp)              # PHI access trail (admin only)
    app.register_blueprint(job_logs_bp)           # Processing API job logs (admin only)
    app.register_blueprint(federated_instances_bp) # Federated learning: instance registration/trust
    app.register_blueprint(federated_rounds_bp)    # Federated learning: training rounds
    app.register_blueprint(federated_training_data_bp)  # Federated learning: local training data

    return app
