# app/routes/reverse_proxy.py
import os, re, urllib.parse, requests
from flask import Blueprint, request, Response, abort
from app.oidc_auth import oidc

proxy_bp = Blueprint("proxy", __name__, url_prefix="/proxy")

# SAMinRAD (docker-context-saminrad/), reached by Compose service name over
# the shared dcm4chee network. The real value comes from COLLEAGUE_BASE in the
# compose file; this default only keeps the two from drifting apart.
TARGET_BASE  = os.environ.get("COLLEAGUE_BASE", "http://saminrad:8003").rstrip("/")
TARGET_URL   = urllib.parse.urlparse(TARGET_BASE)
PROXY_PREFIX = "/proxy/colleague"

_ABS_TARGET_RE = re.compile(re.escape(TARGET_BASE), re.I)
_HTML_ATTR_ABS = re.compile(r'(\s(?:href|src|action)=["\'])/(?!proxy/colleague/)', re.I)
# Top-level route segments of the upstream app (SAMinRAD). Regenerate after
# routes are added there (from the repo root, so Compose can find the project):
#   docker compose exec -w /app saminrad python -c "from app import app; \
#     print(sorted({str(r.rule).lstrip('/').split('/')[0] for r in app.url_map.iter_rules()} - {''}))"
_UPSTREAM_ROUTES = (
    "back_to_annotation", "compare_ground_truth", "create_3d_mask_sam",
    "create_nifti_threshold", "dicom_viewer", "download_latest_segmentation",
    "download_nifti", "download_simple_results", "export_mask_nifti",
    "export_options", "few_shot_segment",
    "few_shot_segment_interpolation", "few_shot_segment_memory",
    "fewshot_overlay_from_archive", "fix_spacing", "get_current_config",
    "get_hybrid_params", "get_image", "list_fewshot_masks",
    "list_segmented_masks", "list_series", "memory_overlay_from_archive",
    "nifti_masks_sam", "nifti_masks_thresh", "overlay_from_archive",
    "overlays_archive", "overlays_fewshot_archive", "overlays_memory_archive",
    "overlays_s1sfq_archive", "refine_sam_with_threshold",
    "retry_from_windowing", "s1sfq_overlay_from_archive", "s1sfq_segment",
    "segment_image", "segment_sam", "select_file", "select_series",
    "serve_original_slice", "set_method", "slice", "start_new_session",
    "static", "upload", "upload_support_mask", "use_default",
)
# Longest first so /upload_support_mask is not shadowed by /upload.
_ROUTE_ALT = "|".join(re.escape(r) for r in sorted(_UPSTREAM_ROUTES, key=len, reverse=True))

# A quoted string that starts with one of those routes gets the proxy prefix.
#
# This used to match ANY string literal beginning with "/", which quietly
# corrupted third-party bundles: in niivue.umd.js it rewrote the regex literal
#   /'descr': '([^']+)'/   ->   /'descr': '([^']+)'/proxy/colleague//
# because the closing quote+slash of the literal looked like the start of a
# path. That is "SyntaxError: Invalid regular expression flags", so the bundle
# never parsed, window.niivue never existed, and opening the 3D viewer failed
# with "niivue is not defined". Anchoring on real routes also stops the 20-odd
# harmless-but-wrong rewrites of .split("/") in the same file.
_TEXT_ABS      = re.compile(
    r'(?<![a-zA-Z0-9])([\'"`])/(?!proxy/colleague/)(?=(?:%s)\b)' % _ROUTE_ALT, re.I)
_CSS_URL_ABS   = re.compile(r'url\(/(?!proxy/colleague/)', re.I)
# Relative endpoints used by the viewer
_RELATIVE_ENDPOINTS = r'(?:get_image|overlay|list_slices|save_mark|save_mask|finalize|download)'

# fetch("get_image/...")
_FETCH_REL = re.compile(r'(\bfetch\(\s*[\'"])(?!https?:|/|dicom_viewer/)(%s\b[^\'"]*)' % _RELATIVE_ENDPOINTS, re.I)

# XHR: xhr.open("GET", "get_image/...")
_XHR_OPEN = re.compile(r'(\.open\(\s*[\'"]\w+[\'"]\s*,\s*[\'"])(?!https?:|/|dicom_viewer/)(%s\b[^\'"]*)' % _RELATIVE_ENDPOINTS, re.I)

# Image.src = "get_image/..."
_SRC_REL = re.compile(r'(\.src\s*=\s*[\'"])(?!https?:|/|dicom_viewer/)(%s\b[^\'"]*)' % _RELATIVE_ENDPOINTS, re.I)

# new URL("get_image/...", window.location)
_NEW_URL = re.compile(r'(\bnew\s+URL\(\s*[\'"])(?!https?:|/|dicom_viewer/)(%s\b[^\'"]*)' % _RELATIVE_ENDPOINTS, re.I)

# Also catch href/src/action in the HTML itself if they’re relative
_HTML_ATTR_REL = re.compile(
    r'(\s(?:href|src|action)=["\'])(?!https?:|/|#|%s/)'
    % re.escape(PROXY_PREFIX.lstrip('/')),
    re.I,
)



### NEW: catch bad relative fetches like fetch("?axis=...") or fetch("123?axis=...")
_NUM_FETCH = re.compile(r'(\bfetch\(\s*[\'"])(\d+(?:[A-Za-z0-9._-]*)?\?)', re.I)
_Q_ONLY    = re.compile(r'(\bfetch\(\s*[\'"])\?', re.I)

def _target_url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    qs = ("?" + request.query_string.decode()) if request.query_string else ""
    return f"{TARGET_BASE}{path}{qs}"

def _build_upstream_headers():
    hop = {
        "host","content-length","transfer-encoding","connection","keep-alive",
        "proxy-authenticate","proxy-authorization","te","trailers","upgrade",
        "accept-encoding",
    }
    # NEW: strip conditional/cache headers so we always get 200 and can rewrite
    strip_conditional = {
        "if-none-match", "if-modified-since", "cache-control", "pragma"
    }
    # SAMinRAD never reads request.cookies or flask.session -- it has no use
    # for the GUI's session cookie. On the hospital domain the browser's
    # Cookie header also accumulates cookies from Keycloak and other apps
    # sharing that domain, which can exceed gunicorn's default 8190-byte
    # per-header limit ("limit request headers fields size", logged by
    # saminrad's gunicorn) and made every proxied request fail. Dropping it
    # here mirrors api_proxy.py's cookies=None for the same reason.
    strip_unused = {"cookie"}

    h = {k: v for k, v in request.headers
         if k.lower() not in hop
         and k.lower() not in strip_conditional
         and k.lower() not in strip_unused}

    h["Host"] = TARGET_URL.netloc
    h["Accept-Encoding"] = "identity"
    h["X-Forwarded-Proto"]  = request.headers.get("X-Forwarded-Proto", request.scheme)
    h["X-Forwarded-Host"]   = request.host
    h["X-Forwarded-Prefix"] = PROXY_PREFIX
    h["X-Forwarded-For"]    = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    # Hint to upstream not to cache
    h["Cache-Control"] = "no-cache"
    return h

def _rewrite_location(location: str) -> str:
    if not location:
        return location
    if location.startswith(TARGET_BASE):
        rest = location[len(TARGET_BASE):]
        return PROXY_PREFIX + (rest if rest.startswith("/") else "/" + rest)
    if location.startswith("/") and not location.startswith(PROXY_PREFIX + "/"):
        return PROXY_PREFIX + location
    return location

def _inject_base_tag(html: str, base_href: str) -> str:
    if re.search(r"<base\b", html, re.I):
        return html
    return re.sub(r"(<head[^>]*>)", rf'\1\n<base href="{base_href}">', html, count=1, flags=re.I)

def _rewrite_html(b: bytes, *, base_href: str) -> bytes:
    try:
        t = b.decode("utf-8", errors="ignore")
        t = _ABS_TARGET_RE.sub(PROXY_PREFIX, t)
        t = _HTML_ATTR_ABS.sub(r'\1' + PROXY_PREFIX + '/', t)
        t = _TEXT_ABS.sub(r'\1' + PROXY_PREFIX + '/', t)

        # NEW: make relative attributes point to proxy + dicom_viewer
        t = _HTML_ATTR_REL.sub(r'\1' + PROXY_PREFIX + '/dicom_viewer/', t)

        # Existing special cases
        t = _NUM_FETCH.sub(r'\1dicom_viewer/\2', t)
        t = _Q_ONLY.sub(r'\1dicom_viewer/?', t)

        # NEW: dynamic builders
        t = _FETCH_REL.sub(r'\1dicom_viewer/\2', t)
        t = _XHR_OPEN.sub(r'\1dicom_viewer/\2', t)
        t = _SRC_REL.sub(r'\1dicom_viewer/\2', t)
        t = _NEW_URL.sub(r'\1dicom_viewer/\2', t)

        t = _inject_base_tag(t, base_href)
        return t.encode("utf-8")
    except Exception:
        return b

def _rewrite_text_generic(b: bytes) -> bytes:
    try:
        t = b.decode("utf-8", errors="ignore")
        t = _ABS_TARGET_RE.sub(PROXY_PREFIX, t)
        t = _TEXT_ABS.sub(r'\1' + PROXY_PREFIX + '/', t)
        t = _NUM_FETCH.sub(r'\1dicom_viewer/\2', t)
        t = _Q_ONLY.sub(r'\1dicom_viewer/?', t)
        t = _FETCH_REL.sub(r'\1dicom_viewer/\2', t)
        t = _XHR_OPEN.sub(r'\1dicom_viewer/\2', t)
        t = _SRC_REL.sub(r'\1dicom_viewer/\2', t)
        t = _NEW_URL.sub(r'\1dicom_viewer/\2', t)

        return t.encode("utf-8")
    except Exception:
        return b

def _rewrite_text_generic(b: bytes) -> bytes:
    try:
        t = b.decode("utf-8", errors="ignore")
        t = _ABS_TARGET_RE.sub(PROXY_PREFIX, t)
        t = _TEXT_ABS.sub(r'\1' + PROXY_PREFIX + '/', t)
        t = _NUM_FETCH.sub(r'\1dicom_viewer/\2', t)
        t = _Q_ONLY.sub(r'\1dicom_viewer/?', t)
        t = _FETCH_REL.sub(r'\1dicom_viewer/\2', t)
        t = _XHR_OPEN.sub(r'\1dicom_viewer/\2', t)
        t = _SRC_REL.sub(r'\1dicom_viewer/\2', t)
        t = _NEW_URL.sub(r'\1dicom_viewer/\2', t)

        return t.encode("utf-8")
    except Exception:
        return b


def _rewrite_json(b: bytes) -> bytes:
    try:
        t = b.decode("utf-8", errors="ignore")
        t = _ABS_TARGET_RE.sub(PROXY_PREFIX, t)
        t = _TEXT_ABS.sub(r'\1' + PROXY_PREFIX + '/', t)
        return t.encode("utf-8")
    except Exception:
        return b

def _rewrite_css(b: bytes) -> bytes:
    try:
        t = b.decode("utf-8", errors="ignore")
        t = _ABS_TARGET_RE.sub(PROXY_PREFIX, t)
        t = _CSS_URL_ABS.sub('url(' + PROXY_PREFIX + '/', t)
        return t.encode("utf-8")
    except Exception:
        return b

@proxy_bp.before_request
def require_login():
    if not oidc.user_loggedin:
        abort(401)

@proxy_bp.route("/colleague/", defaults={"path": ""}, methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"])
@proxy_bp.route("/colleague/<path:path>", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"])
def colleague_proxy(path):
   
    upstream_path = path
    qs = request.query_string.decode() if request.query_string else ""

    if upstream_path == "" and qs.startswith("axis="):
        upstream_path = "dicom_viewer"

    if upstream_path and upstream_path.split("/")[0].isdigit() and "axis=" in qs:
        upstream_path = f"dicom_viewer/{upstream_path}"
    
    data = request.get_data() if request.method not in ("GET","HEAD") else None

    upstream = requests.request(
        method=request.method,
        url=_target_url(upstream_path),
        headers=_build_upstream_headers(),
        data=data,
        stream=True,
        allow_redirects=False,
        timeout=120,
    )

    hop = {
        "content-encoding","transfer-encoding","connection","keep-alive",
        "proxy-authenticate","proxy-authorization","te","trailers","upgrade",
    }

    drop_resp_cache = {"etag", "last-modified", "expires"}

    headers = []
    for k, v in upstream.headers.items():
        lk = k.lower()
        if lk in hop:
            continue
        if lk == "location":
            v = _rewrite_location(v)
        headers.append((k, v))

    ctype = upstream.headers.get("Content-Type", "")

    # Choose a helpful <base href> depending on the path

    def _force_no_store(hdrs):
        return [(k, v) for (k, v) in hdrs if k.lower() not in drop_resp_cache] + \
               [("Cache-Control", "no-store, max-age=0")]

    base_href = f"{PROXY_PREFIX}/"
    if "dicom_viewer" in (path or ""):
        base_href = f"{PROXY_PREFIX}/dicom_viewer/"

    if "text/html" in ctype or "application/xhtml" in ctype:
        body = _rewrite_html(upstream.content, base_href=base_href)
        return Response(body, status=upstream.status_code, headers=_force_no_store(headers))

    if any(t in ctype for t in ("application/javascript", "text/javascript", "text/plain")):
        body = _rewrite_text_generic(upstream.content)
        return Response(body, status=upstream.status_code, headers=_force_no_store(headers))

    if "application/json" in ctype:
        body = _rewrite_json(upstream.content)
        return Response(body, status=upstream.status_code, headers=_force_no_store(headers))

    if "text/css" in ctype:
        body = _rewrite_css(upstream.content)
        return Response(body, status=upstream.status_code, headers=_force_no_store(headers))

    # Binary passthrough: forward the body byte for byte, Content-Encoding included.
    #
    # Flask's send_file() derives the encoding from the file name, so a .nii.gz
    # segmentation mask arrives as "Content-Encoding: gzip" with the COMPRESSED
    # Content-Length. iter_content() inflates it transparently, which used to
    # leave a 3.5 MB body behind a Content-Length of 17387 (the header list
    # above drops content-encoding as hop-by-hop). The browser then stopped
    # reading after 17 KB, niivue got a truncated NIfTI, and the 3D viewer
    # rendered nothing while the 2D view -- served as PNG, no encoding -- was
    # fine. decode_content=False keeps body and headers in step and leaves the
    # inflating to the browser, exactly as when the app is opened directly.
    enc = upstream.headers.get("Content-Encoding")
    if enc:
        headers.append(("Content-Encoding", enc))
    return Response(upstream.raw.stream(64 * 1024, decode_content=False),
                    status=upstream.status_code, headers=headers)
