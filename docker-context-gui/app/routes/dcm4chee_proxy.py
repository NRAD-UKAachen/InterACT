# app/routes/dcm4chee_proxy.py
"""
Reverse proxy for DCM4CHEE - allows iframe embedding while keeping
dcm4chee protected behind the GUI's Keycloak authentication.

This is also the only point in this path that knows *who* is asking: dcm4chee
itself has no authentication here (it is reachable only from inside the Docker
network, fronted by this proxy and by oauth2-proxy), so its own logs can name
an IP address but never a person. Every request that reaches patient data is
therefore audited here, against the Keycloak identity - see app/utils/audit.py.

Traffic from the OHIF viewer on :1081 does NOT come through this file; it goes
browser -> oauth2-proxy -> nginx -> dcm4chee and is audited in nginx instead
(viewers/platform/app/.recipes/Nginx-Dcm4chee/config/nginx.conf).
"""
import os
import re
import requests
from flask import Blueprint, request, Response, abort
from app.oidc_auth import oidc
from app.utils import audit

dcm4chee_proxy_bp = Blueprint("dcm4chee_proxy", __name__)

# Also create a redirect blueprint for direct /dcm4chee-arc/ requests
dcm4chee_redirect_bp = Blueprint("dcm4chee_redirect", __name__)

# DCM4CHEE internal URL (internal Docker network or direct access)
DCM4CHEE_BASE = os.environ.get("DCM4CHEE_INTERNAL_URL", "http://dcm4chee-ui-proxy:80").rstrip("/")

# Headers to strip from requests/responses
HOP_BY_HOP = {
    "content-encoding", "transfer-encoding", "connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade",
}


def _build_upstream_headers():
    """Build headers to send to dcm4chee, stripping hop-by-hop headers."""
    headers = {}
    for key, value in request.headers:
        if key.lower() not in HOP_BY_HOP and key.lower() != "host":
            headers[key] = value

    # Set the correct host for dcm4chee
    headers["Host"] = DCM4CHEE_BASE.split("://")[1].split("/")[0]
    headers["Accept-Encoding"] = "identity"
    headers["X-Forwarded-For"] = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    headers["X-Forwarded-Proto"] = request.scheme
    headers["X-Forwarded-Host"] = request.host

    return headers


def _filter_response_headers(upstream_headers):
    """Filter response headers, removing hop-by-hop headers."""
    headers = []
    for key, value in upstream_headers.items():
        if key.lower() not in HOP_BY_HOP:
            # Rewrite Location headers to go through proxy
            if key.lower() == "location" and value.startswith(DCM4CHEE_BASE):
                value = value.replace(DCM4CHEE_BASE, "/dcm4chee-proxy")
            headers.append((key, value))
    return headers


def _require_login():
    """Require Keycloak authentication for all dcm4chee proxy requests."""
    if not oidc.user_loggedin:
        # Logged as "denied" rather than dropped: an unauthenticated request
        # aimed at a study URL is the shape of an attempt worth noticing, and
        # it never reaches _proxy_to_dcm4chee to be recorded there.
        _audit_proxy(request.path.lstrip("/"), 401)
        abort(401, description="Authentication required")


@dcm4chee_redirect_bp.before_request
def redirect_require_login():
    _require_login()


@dcm4chee_redirect_bp.route("/dcm4chee-arc/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@dcm4chee_redirect_bp.route("/dcm4chee-arc/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def redirect_dcm4chee(path):
    """Handle direct /dcm4chee-arc/ requests by proxying them."""
    # path is everything after /dcm4chee-arc/, so we need dcm4chee-arc/ + path
    full_path = f"dcm4chee-arc/{path}" if path else "dcm4chee-arc/"
    return _proxy_to_dcm4chee(full_path)


def _rewrite_content(content, content_type):
    """Rewrite URLs in HTML/JS/CSS content to go through proxy."""
    # Since we now handle /dcm4chee-arc/ directly, we don't need to rewrite URLs.
    # The Angular app will make requests to /dcm4chee-arc/... which we intercept.
    # Only rewrite base href to ensure Angular router works correctly.
    if not content_type:
        return content

    base_type = content_type.split(";")[0].strip().lower()
    if base_type != "text/html":
        return content

    # Decode if bytes
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            return content

    # Only rewrite base href to match where the app is served from
    # The app is loaded from /dcm4chee-arc/ui2/ (via our redirect handler)
    content = re.sub(
        r'<base\s+href="[^"]*"',
        '<base href="/dcm4chee-arc/ui2/"',
        content,
        flags=re.IGNORECASE
    )

    return content.encode("utf-8") if isinstance(content, str) else content


# Static assets of the dcm4chee-arc UI2 Angular app. Auditing these would bury
# the handful of lines that describe actual patient-data access under a few
# hundred per page load, which makes the trail unusable rather than thorough.
_ASSET_SUFFIXES = (
    ".js", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".html",
)

# .../studies/<uid>[/series/<uid>[/instances/<uid>]] anywhere in a DICOMweb path.
_STUDY_RE = re.compile(r"/studies/([0-9.]+)")
_SERIES_RE = re.compile(r"/series/([0-9.]+)")
_INSTANCE_RE = re.compile(r"/instances/([0-9.]+)")


def _classify(path):
    """(action, object_type) for a dcm4chee path, or None to skip auditing.

    QIDO-RS, WADO-RS and STOW-RS all live under the same URL space and are
    told apart by method and shape, not by a distinct prefix: a GET on
    /studies is a search, a GET on /studies/<uid>/... is a retrieval, and a
    POST on /studies is a store.
    """
    lowered = path.lower()
    if lowered.endswith(_ASSET_SUFFIXES):
        return None

    # The management API (/dcm4chee-arc/ui2/rs/..., devices, queues) is
    # configuration, not patient data - except where it names a study.
    is_dicomweb = "/rs/" in lowered or "/wado" in lowered or "/aets/" in lowered
    has_study = "/studies/" in lowered or lowered.endswith("/studies")

    if not is_dicomweb and not has_study:
        return None

    if request.method == "DELETE":
        return "delete", "study" if has_study else "object"
    if request.method in ("POST", "PUT", "PATCH"):
        if "/reject/" in lowered or "/expire/" in lowered:
            return "modify", "study"
        if "/export/" in lowered:
            return "export", "study"
        if lowered.rstrip("/").endswith("/studies"):
            return "upload", "study"
        return "modify", "study" if has_study else "object"

    # GET/HEAD.
    if "/wado" in lowered:
        return "view", "instance"
    if _STUDY_RE.search(path):
        # A retrieval of identified pixel data or metadata: someone is looking
        # at a specific patient's images.
        return "view", "instance" if "/instances/" in lowered else "series" \
            if "/series/" in lowered else "study"
    # /studies, /series, /instances with no UID: a query across the archive.
    return "search", "study"


def _audit_proxy(path, status):
    """Record one PHI access through this proxy, if the path involves any."""
    classified = _classify(path)
    if classified is None:
        return

    action, object_type = classified
    if status is None:
        outcome = "error"
    elif status in (401, 403):
        outcome = "denied"
    elif status >= 400:
        outcome = "error"
    else:
        outcome = "success"

    study = _STUDY_RE.search(path)
    series = _SERIES_RE.search(path)
    instance = _INSTANCE_RE.search(path)

    audit.record_request(
        "dcm4chee", action,
        object=f"/{path}",
        object_type=object_type,
        outcome=outcome,
        study_uid=study.group(1) if study else None,
        series_uid=series.group(1) if series else None,
        detail=f"instance {instance.group(1)}" if instance else None,
        patient_id=request.args.get("PatientID") or request.args.get("00100020"),
    )


def _proxy_to_dcm4chee(path):
    """Internal function to proxy requests to dcm4chee."""
    # Build upstream URL
    upstream_url = f"{DCM4CHEE_BASE}/{path}"
    if request.query_string:
        upstream_url += f"?{request.query_string.decode()}"

    # Get request body for non-GET methods
    data = request.get_data() if request.method not in ("GET", "HEAD") else None

    try:
        # Make request to dcm4chee
        upstream = requests.request(
            method=request.method,
            url=upstream_url,
            headers=_build_upstream_headers(),
            data=data,
            stream=False,  # Need full content for rewriting
            allow_redirects=False,
            timeout=120,
        )

        # Filter response headers
        response_headers = _filter_response_headers(upstream.headers)

        # Get content type
        content_type = upstream.headers.get("Content-Type", "")

        # Rewrite content if needed
        content = _rewrite_content(upstream.content, content_type)

        # Update content-length if content was modified
        response_headers = [(k, v) for k, v in response_headers if k.lower() != "content-length"]
        response_headers.append(("Content-Length", str(len(content))))

        _audit_proxy(path, upstream.status_code)

        # Return response
        return Response(
            content,
            status=upstream.status_code,
            headers=response_headers,
        )

    except requests.RequestException as e:
        _audit_proxy(path, None)
        return Response(
            f"Error connecting to DCM4CHEE: {str(e)}",
            status=502,
            content_type="text/plain"
        )


@dcm4chee_proxy_bp.before_request
def proxy_require_login():
    _require_login()


@dcm4chee_proxy_bp.route("/dcm4chee-proxy/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@dcm4chee_proxy_bp.route("/dcm4chee-proxy/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def proxy_dcm4chee(path):
    """Proxy all requests to dcm4chee via /dcm4chee-proxy/ path."""
    return _proxy_to_dcm4chee(path)
