# app/routes/api_proxy.py
"""
Authenticated front door for the batch/segmentation API (API/app/routes.py).

That API has no authentication of its own and its endpoints fetch, process
and STOW identifiable patient studies, so it used to be published on
0.0.0.0:9200 purely because the browser needed to reach it directly --
batch.html polls /batch/status/<job_id>, OHIF's studyWebhooks buttons call
/fastsurfer/... and /totalsegmentator/..., and hippocampus reports link to
/hippocampus_files/... . That made every study on dcm4chee readable by
anything that could reach the host.

The API is now bound to loopback and reached over the shared Docker network
instead. This blueprint is what the browser talks to: same paths, but behind
@oidc.require_login, so a Keycloak session is required before anything
touches patient data. Point browser-side callers at /api-proxy/... .

Deliberately not a general-purpose proxy: only the paths the browser
actually needs are forwarded (ALLOWED_PREFIXES), so this cannot be turned
into an open relay to the API's whole surface.

Being the only authenticated door to that API also makes it the only place
that can attribute its work to a person: the API itself never learns who
asked, so a study it fetches, segments and STOWs back would otherwise appear
in no audit trail under anybody's name. Hence the audit calls below - see
app/utils/audit.py.
"""
import os
import re

import requests
from flask import Blueprint, Response, request, stream_with_context

from app.oidc_auth import oidc
from app.utils import audit

api_proxy_bp = Blueprint("api_proxy", __name__, url_prefix="/api-proxy")

# Reached over interact-dcm4chee by container name -- no host port.
UPSTREAM = os.environ.get("BATCH_API_INTERNAL", "http://ohif_api:9200").rstrip("/")

# Only these upstream paths are exposed to the browser: what the batch page,
# the hippocampus report links and OHIF's toolbar and Upload buttons need, and
# nothing else. Everything absent from this list is unreachable from a browser
# even though the API itself has no auth of its own.
ALLOWED_PREFIXES = (
    # batch.html polling + result download
    "batch/status/",
    "batch/result/",
    "batch/jobs",
    "job_status/",
    # hippocampus report "open in 3D Slicer" links, and the copyable-text
    # links page (hippocampus_links_route in API/app/routes.py)
    "hippocampus_files/",
    "hippocampus_links/",
    # DTI tractography's interactive whole-brain fibre viewer link
    # (dti_files_route in API/app/routes.py)
    "dti_files/",
    # OHIF studyWebhooks / seriesWebhooks buttons, and the NIfTI/NRRD
    # branch of the Upload button. These are cross-origin calls from :1081
    # to :5005 -- but same-site (cookies are host-scoped, not port-scoped),
    # so the SameSite=Lax session cookie is sent as long as the fetches set
    # credentials:'include' and CORS allows credentials. See
    # app/__init__.py's CORS config.
    "download_series/",
    "download_series_nifti/",
    "tumorseg/",
    "totalsegmentator/",
    "fastsurfer/",
    "synthseg/",
    # Longitudinal FastSurfer is its own upstream route (it analyses several
    # of the patient's studies together, not one study in a mode), so the
    # "fastsurfer/" prefix above does not cover it.
    "fastsurfer_long/",
    "hippocampus/",
    "hippocampus_reoriented/",
    "siena/",
    "screw_infer/",
    "fsl_register_qc/",
    "dti/",
    "coreg/",
    "ldct/",
    # Upload button: mint a StudyInstanceUID, then POST the volume.
    "new_study_uid",
    "volume_upload",
    # SAMinRAD's "store the segmentation in the database" option: the mask a
    # person drew on an imported series is POSTed back as a DICOM SEG.
    "segmentation_upload/",
)

# Hop-by-hop headers must not be forwarded in either direction.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length", "content-encoding",
}

_SAFE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")

# Status polling. batch.html polls these every couple of seconds for the life
# of a job, so auditing them would drown the trail in lines that record no
# access to patient data - the job's actual reads and writes are recorded
# where they happen, on submission and on result download.
_NOT_PHI_PREFIXES = ("batch/status/", "job_status/", "batch/jobs")

# What each upstream prefix does with patient data. Anything not listed is
# audited as a plain "view", which fails safe: an unrecognised path that
# reaches this API is reaching PHI by definition of what the API serves.
_ACTIONS = {
    "download_series/": ("download", "series"),
    "download_series_nifti/": ("download", "series"),
    "hippocampus_files/": ("download", "study"),
    "dti_files/": ("download", "study"),
    "batch/result/": ("export", "job"),
    "volume_upload": ("upload", "study"),
    "segmentation_upload/": ("upload", "study"),
    "new_study_uid": ("modify", "study"),
}

# A DICOM UID (or a job id) sitting in the path - what was actually touched.
_UID_RE = re.compile(r"(?:^|/)([0-9]+(?:\.[0-9]+){3,})(?:/|$)")


def _audit_api(path, status):
    if path.startswith(_NOT_PHI_PREFIXES):
        return

    action, object_type = ("view", "study")
    for prefix, classified in _ACTIONS.items():
        if path.startswith(prefix):
            action, object_type = classified
            break

    if status is None:
        outcome = "error"
    elif status in (401, 403):
        outcome = "denied"
    elif status >= 400:
        outcome = "error"
    else:
        outcome = "success"

    uid = _UID_RE.search(path)
    audit.record_request(
        "api", action,
        object=f"/{path}",
        object_type=object_type,
        outcome=outcome,
        # The API addresses studies and series interchangeably by UID and
        # does not say which one a given endpoint took, so record it under
        # the field the endpoint's own object_type implies.
        study_uid=uid.group(1) if uid and object_type != "series" else None,
        series_uid=uid.group(1) if uid and object_type == "series" else None,
    )


def _is_allowed(path: str) -> bool:
    # Reject anything with traversal or unexpected characters before the
    # prefix check, so ".." can't be used to walk out of an allowed prefix.
    if not _SAFE_PATH.match(path) or ".." in path:
        return False
    return path.startswith(ALLOWED_PREFIXES)


@api_proxy_bp.route("/<path:path>", methods=["GET", "POST", "DELETE", "OPTIONS"])
def proxy(path):
    # CORS preflight: browsers never attach credentials to it, so it has to
    # be answerable without a session. flask-cors fills in the headers.
    if request.method == "OPTIONS":
        return Response(status=204)

    # Deliberately a 401, not @oidc.require_login's redirect: these callers
    # are fetch()/XHR from OHIF, and a 302 to Keycloak fails CORS and
    # surfaces as an opaque network error. A 401 lets the caller say
    # "your session expired, reload".
    if not oidc.user_loggedin:
        _audit_api(path, 401)
        return Response(
            '{"error": "Authentication required. Reload the viewer and log in."}',
            status=401, mimetype="application/json",
        )

    if not _is_allowed(path):
        return Response("Not proxied.", status=404, mimetype="text/plain")

    upstream_headers = {
        k: v for k, v in request.headers if k.lower() not in _HOP_BY_HOP
    }

    try:
        resp = requests.request(
            method=request.method,
            url=f"{UPSTREAM}/{path}",
            params=request.args,
            data=request.get_data() if request.method != "GET" else None,
            headers=upstream_headers,
            # Never forward the caller's cookies upstream: the API has no
            # auth, so they'd serve no purpose and would leak the session
            # cookie into a service that logs its inputs.
            cookies=None,
            stream=True,
            allow_redirects=False,
            timeout=(5, 600),
        )
    except requests.RequestException as e:
        _audit_api(path, None)
        return Response(f"Upstream API unreachable: {e}", status=502,
                        mimetype="text/plain")

    _audit_api(path, resp.status_code)

    # Drop the upstream's own CORS headers. The API answers every request
    # with "Access-Control-Allow-Origin: *" (its cors_json/cors_file
    # helpers), and a wildcard is illegal on a credentialed request -- the
    # browser discards the response even on a 202. Strip them here and let
    # flask-cors set the correct, origin-specific ones on the way out.
    headers = [
        (k, v) for k, v in resp.raw.headers.items()
        if k.lower() not in _HOP_BY_HOP
        and not k.lower().startswith("access-control-")
    ]
    return Response(
        stream_with_context(resp.iter_content(chunk_size=64 * 1024)),
        status=resp.status_code,
        headers=headers,
    )
