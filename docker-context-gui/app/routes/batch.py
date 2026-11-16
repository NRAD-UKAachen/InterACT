# app/routes/batch.py
"""
GUI page for the cohort batch pipeline (Documentation/API_and_API_storage.md).
The API side (API/app/routes.py + batch_io.py + batch_jobs.py) already runs a batch job from an uploaded ZIP and answers
GET /batch/status/<job_id> - this blueprint's job is the page, plus staging
input in from an upload or iRODS and pushing output back out to iRODS on
request (the API's compute core touches only local disk and
holds no iRODS credential, so any iRODS I/O has to happen at the edges, here,
using this person's own token via app.utils.irods_client).

The batch API has no auth of its own (see API/app/routes.py). It used to be
published on 0.0.0.0:9200 so the browser could poll it directly, which meant
anyone able to reach the host could read other people's batch results -- and,
through the same API, any study on dcm4chee. It is now bound to loopback:
this blueprint reaches it server-side over the shared Docker network
({BATCH_API_BASE}, container name), and the page polls
GET {BATCH_API_BROWSER_BASE}/batch/status/<job_id> and downloads
GET {BATCH_API_BROWSER_BASE}/batch/result/<job_id> through
app/routes/api_proxy.py, which requires a Keycloak session. Only the submit
(which needs the cohort staged first, from
either an iRODS collection or a folder-drop upload), the iRODS push-back
(which needs this person's Keycloak token), and the /my_jobs listing (which
needs this person's identity, which the batch API has no way to know) go
through here.

The cohort is handed to the API by *path*, not by upload: see
BATCH_STAGE_ROOT below.

Per-user job listing: submit() stamps every job it starts with `owner` =
irods.current_username() (a form field the batch API just stores -- it does
NOT enforce it, see batch_jobs.start_batch_job's docstring on the API side),
so /my_jobs can ask the API to filter to "this user's jobs". This is what
lets batch.html restore a still-running job's progress panel after a
logout/login (previously only held in that browser tab's JS memory) and
lets each user's "Your Batch Jobs" list show only their own submissions.
It is a convenience filter, not an access-control boundary: a job_id
obtained some other way (e.g. from browser history) can still be polled
directly against the batch API by anyone, same as before this existed --
push_to_irods() below is the one place this blueprint additionally checks
`owner` before acting, since that action moves another user's data into
the caller's own iRODS space.
"""
import json
import os
import shutil
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import requests
from flask import Blueprint, render_template, request, jsonify

from app import oidc_auth
from app.oidc_auth import oidc
from app.utils import irods_client as irods
from app.utils.irods_client import IrodsAlreadyExists, IrodsError
from app.utils.storage_paths import API_STORAGE_DIR

batch_bp = Blueprint("batch", __name__, url_prefix="/batch")

# Server-side base: this container -> the API over the shared Docker
# network. The API is bound to loopback on the host (it has no auth of its
# own), so it is no longer reachable at the host's own address on :9200.
BATCH_API_BASE = os.environ.get("BATCH_API_BASE", "http://ohif_api:9200").rstrip("/")

# Browser-side base: relative path to this app's authenticated proxy
# (app/routes/api_proxy.py). The page used to poll the API directly on
# the host's own address on :9200, which meant anyone who could reach the host could poll
# and download other people's batch results without logging in.
BATCH_API_BROWSER_BASE = os.environ.get("BATCH_API_BROWSER_BASE", "/api-proxy").rstrip("/")

# Where a staged cohort is written for the batch API to pick up. Both this
# container and the API bind-mount /mnt/.../API_storage at the same host
# path, so a cohort written here needs no second transfer: submit() hands
# the API this path and the API *moves* the tree into the job's work dir
# (batch_io.BATCH_STAGE_ROOT / resolve_staged_input_dir on that side). What
# this replaced: building the whole cohort as an in-memory ZIP here, POSTing
# it, and unpacking it there - three full copies of every byte, one of them
# resident in this process's RAM (a 100-patient cohort would simply have
# OOM'd the GUI).
#
# NOT to be confused with mounting the iRODS Vault: this is a scratch
# directory holding data the logged-in person has already been granted and
# downloaded through iRODS's own ACL-checked API, on a mount both containers
# already had. The vault itself stays untouched - see
# Documentation/iRODS.md.
BATCH_STAGE_ROOT = os.environ.get(
    "BATCH_STAGE_ROOT",
    f"{API_STORAGE_DIR}/batch_inputs",
)

# How many files to download from iRODS at once during stage-in (see
# _run_irods_stage below). A real 3-patient/~4500-slice/0.8 GB test cohort
# took 5-10 minutes staging sequentially through the iRODS HTTP API - not
# because of data volume, but because each file is a separate iRODS "open,
# read, close" round trip, and each read within it moved only 8 KiB
# (the HTTP API's shipped max_number_of_bytes_per_read_operation, since
# raised to 1 MiB - see Documentation/iRODS.md's throughput-tuning table, which is
# what made concurrency stop being the only available lever).
#
# Concurrency helped a lot on its own (2400+ files in ~90s) but at 3
# concurrent streams it also wedged the HTTP API outright under sustained
# load - a stall, not a crash: no error either side, the HTTP API's own
# /info kept answering, no DB locks, just 3 permanently-silent in-flight
# reads. That server
# now runs with more request/background-IO threads and a bigger, far less
# eagerly refreshed connection pool, so this default is raised too - the
# stall watchdog below stays regardless, since the root cause was never
# proven.
#
# Deliberately reading the iRODS Vault directly on disk to sidestep the HTTP
# API entirely was considered and rejected: every vault file is owned solely
# by the `irods` OS account with 600 permissions specifically so nothing
# else can read object bytes without going through iRODS's own ACL-checked
# API - this stays on the HTTP API and makes the API itself faster instead
# (see Documentation/iRODS.md).
IRODS_STAGE_CONCURRENCY = int(os.environ.get("IRODS_STAGE_CONCURRENCY", "6"))

# A stalled read looks identical to a slow one from the client's own
# request-level timeout (see _run_irods_stage's docstring on why the stall
# didn't just raise on its own) - this is the ceiling on how long the stage
# waits for *any* forward progress (a file completing) before giving up and
# reporting a clear error instead of hanging indefinitely.
IRODS_STAGE_STALL_SECONDS = int(os.environ.get("IRODS_STAGE_STALL_SECONDS", "45"))

# Bundle each series into one server-side tar and fetch that, instead of
# fetching its files one at a time (irods_client.create_tar_bundle). The
# round trip per file is the thing that made stage-in slow, and a series is
# the right unit to collapse: it is exactly what _plan_needed_series decides
# to keep or skip, so bundling per series preserves that filter completely
# (bundling the whole cohort in one tar would drag back the 60-75% of series
# no selected tool opens, which is the larger win of the two).
#
# The tar is built in the vault and deleted straight after, so a bundle costs
# transient vault space; per-series and bounded by the concurrency below,
# that peak is a few series rather than the whole cohort.
#
# Set to 0 to turn bundling off and go back to per-file downloads. Failures
# fall back to that path on their own, per series, so this is a kill switch
# rather than something that needs setting when a zone can't bundle.
IRODS_STAGE_BUNDLE = os.environ.get("IRODS_STAGE_BUNDLE", "1") not in ("0", "false", "")

# Below this many files a series is fetched per-file anyway: a bundle costs
# three round trips of its own (create the tar, read it, delete it) plus a
# vault-side copy of the series, which a handful of files doesn't earn back.
IRODS_STAGE_BUNDLE_MIN_FILES = int(os.environ.get("IRODS_STAGE_BUNDLE_MIN_FILES", "8"))

# Separate from IRODS_STAGE_CONCURRENCY and lower: a bundle occupies an HTTP
# API request thread for far longer than one file does, and each one in
# flight is another whole series duplicated in the vault until it is read and
# removed.
IRODS_STAGE_BUNDLE_CONCURRENCY = int(os.environ.get("IRODS_STAGE_BUNDLE_CONCURRENCY", "4"))

# Where stage-in puts the tars it asks the server to build. Under the
# staging user's own home because that is somewhere they are certain to be
# able to write, which the cohort's own collection (possibly shared
# read-only) is not. Each tar is deleted as soon as it has been read.
IRODS_STAGE_BUNDLE_DIRNAME = ".interact_stage_bundles"

# How long a finished/errored stage-in record (and its scratch dir) is kept
# around before being swept - covers a browser tab that staged a cohort and
# never came back to submit it.
_STAGE_TTL_SECONDS = 3600

# Stage directories used to live in the container's own /tmp, where a restart
# wiped them; on the shared mount they outlive the process, so a stage
# abandoned by a GUI restart (its _stages record gone, its directory not)
# would sit there forever. Anything under BATCH_STAGE_ROOT this process has
# no record of and that hasn't been touched in this long is such an orphan.
# Generous, since "no record of it" is only reliable *after* a restart and a
# genuinely huge stage-in must never be swept out from under itself.
_ORPHAN_STAGE_TTL_SECONDS = 24 * 3600

# stage_id -> {status, files_done, files_total, bytes_done, bytes_total,
# current_file, error, work_dir, created_at}. status is "downloading" ->
# "done" or "error". One process, threaded dev server (see docker-compose.yml
# - "flask run --with-threads", no gunicorn workers), so a plain in-memory
# dict guarded by a lock is visible to every request the same way
# API/app/batch_jobs.py's job registry is.
_stages = {}
_stage_lock = threading.Lock()

# Naming-convention hints for each "role" a batch tool can require (mirrors
# SERIES_CRITERIA in API/app/backend_helpers.py, which is what actually
# decides the match -- against DICOM SeriesDescription for a DICOM series, or
# the series *folder name* for an already-NIfTI one, since NIfTI carries no
# such header, see batch_io.map_series_to_roles). Duplicated here as
# human-readable text rather than imported: this GUI and the batch API are
# separate containers/codebases (see this module's docstring),
# so there is no shared Python module to import it from -- only the resulting
# rule, restated for a person naming a folder, not the matcher's code.
#
# The match is substring-based and case-insensitive, against the *whole*
# series/folder name -- "must" keywords all have to appear somewhere in it,
# "should" keywords are optional but improve the match against other
# candidates, and "must not" keywords rule a series out even if everything
# else matches (typically the marker for the wrong contrast phase).
SERIES_ROLE_INFO = {
    "t1n": {
        "label": "T1 native",
        "modality": "MR",
        "must_contain": ["t1", "mpr"],
        "should_contain": ["tra", "nativ"],
        "mustnt_contain": ["KM"],
        "example": "t1_mprage_tra_nativ",
    },
    "t1n_acquired": {
        "label": "T1 native (original 3D acquisition, not an MPR reformat)",
        "modality": "MR",
        "must_contain": ["t1", "mpr"],
        "should_contain": ["nativ", "iso"],
        "mustnt_contain": ["KM", "mpr_mpr", "mpr_easyguide"],
        "example": "t1_mprage_sag_iso",
    },
    "t1c": {
        "label": "T1 contrast-enhanced",
        "modality": "MR",
        "must_contain": ["t1", "mpr", "KM", "tra"],
        "mustnt_contain": ["nativ"],
        "example": "t1_mprage_tra_KM",
    },
    "flair": {
        "label": "FLAIR",
        "modality": "MR",
        "must_contain": ["t2"],
        "or_contain": ["tirm", "flair"],
        "should_contain": ["tra"],
        "example": "t2_tirm_tra_flair",
    },
    "t2": {
        "label": "T2",
        "modality": "MR",
        "must_contain": ["t2", "tse"],
        "should_contain": ["tra"],
        "example": "t2_tse_tra",
    },
    # SynthSeg's own T1/T2 roles -- looser than "t1n"/"t2" above so that plain
    # 2D clinical series ("T1 ax", "T2 ax") match too, not just 3D MPRAGE/TSE.
    # Mirrors SERIES_CRITERIA['synthseg_t1'/'synthseg_t2'] in
    # API/app/backend_helpers.py; keep the two in step.
    "synthseg_t1": {
        "label": "T1 (MPRAGE or plain axial)",
        "modality": "MR",
        "must_contain": ["t1"],
        "or_contain": ["mpr", "ax"],
        "should_contain": ["mpr", "nativ", "iso", "tra"],
        "mustnt_contain": ["KM", "relax", "thorax", "axilla", "map", "MIP", "seg", "mask"],
        "example": "t1_mpr_nativ_tra, or T1 ax",
    },
    "synthseg_t2": {
        "label": "T2 (TSE/SPACE or plain axial)",
        "modality": "MR",
        "must_contain": ["t2"],
        "or_contain": ["tse", "spc", "ax"],
        "should_contain": ["tse", "spc", "tra"],
        "mustnt_contain": ["flair", "tirm", "dark-fluid", "star", "t2*",
                           "relax", "thorax", "axilla", "map", "MIP", "seg", "mask"],
        "example": "t2_tse_tra, or T2 ax (FLAIR/TIRM excluded)",
    },
    "tof": {
        "label": "Time-of-flight MRA",
        "modality": "MR",
        "must_contain": ["tof", "3d"],
        "should_contain": ["tra"],
        "mustnt_contain": ["MIP"],
        "example": "tof_3d_tra",
    },
    "ct_tissue": {
        "label": "CT soft-tissue kernel",
        "modality": "CT",
        "or_contain": ["H30s", "Hr36", "WF"],
        "should_contain": ["ax"],
        "example": "H30s_ax",
    },
    "ct": {
        "label": "Any CT series",
        "modality": "CT",
        "example": "any CT series in the study",
    },
    "ct_screw": {
        "label": "CT for screw segmentation",
        "modality": "CT",
        "mustnt_contain": ["mask", "seg"],
        "example": "any CT series that isn't a previous screw run's output",
    },
}

# Small registry of batch-eligible tools.
# Each tool's "modes" list is the tool-specific choice that fills in one
# {"tool", "mode"} entry of the API's POST /batch/run `tools` field (mode
# for FastSurfer, task_name for TotalSegmentator, segmenter_name for BraTS
# -- see BATCH_TOOL_ROLES / TASK_REQUIRED_SERIES in API/app/routes.py and
# API/app/total_segmentator_functions.py). The page presents this as a
# checklist (several tools can run per batch job, each against
# every study, in one job) -- see batch.html/JS.
#
# "series_roles" (tool-level, for fastsurfer/brats) / each mode's own
# "series_roles" (totalsegmentator, whose required series varies per task --
# see TASK_REQUIRED_SERIES in API/app/total_segmentator_functions.py) list
# the SERIES_ROLE_INFO keys that tool/mode needs, purely so batch.html can
# render the "required series" hover cards (a person staging a
# cohort has no other way to know a study's series folders have to be named
# to match SERIES_CRITERIA before the batch even starts).
BATCH_TOOLS = [
    {
        "id": "fastsurfer",
        "label": "FastSurfer",
        "description": "Cortical/subcortical segmentation and volumetry from a T1 MPRAGE MRI.",
        "enabled": True,
        "series_roles": ["t1n"],
        "modes": [
            {"id": "seg", "label": "Segmentation only (~3 min/study)"},
            {"id": "full", "label": "Full surface reconstruction (~30 min/study)"},
        ],
    },
    {
        # The counterpart to FastSurfer above for scans FastSurfer is weakest
        # on. Its networks are trained across a deliberately wide range of
        # resolutions and contrasts, so it is the one to run over a cohort of
        # routine clinical T1s (thick-slice, anisotropic, mixed protocols)
        # rather than research-grade 1 mm volumes. No cortical thickness --
        # there is no surface stage -- but it is minutes per case instead of
        # half an hour, and it reports its own per-structure QC scores, which
        # land in results_overview.csv so a bad segmentation can be filtered
        # out of a group analysis instead of skewing it.
        "id": "synthseg",
        "label": "SynthSeg",
        "description": (
            "Whole-brain segmentation and volumetry from an arbitrary T1 or T2 MRI. ~1-2 min per study and contrast."
        ),
        "enabled": True,
        "series_roles": ["synthseg_t1", "synthseg_t2"],
        "modes": [
            {"id": "standard", "label": "SynthSeg 2.0 + cortical parcellation"},
            {"id": "robust", "label": "SynthSeg-robust (for poor-quality/low-resolution scans)"},
        ],
    },
    {
        "id": "totalsegmentator",
        "label": "TotalSegmentator",
        "description": "Brain CT/TOF-MRA organ and pathology segmentation.",
        "enabled": True,
        "modes": [
            {"id": "cerebral_bleed", "label": "Cerebral bleed (CT)", "series_roles": ["ct_tissue"]},
            {"id": "brain_aneurysm", "label": "Brain aneurysm (TOF-MRA)", "series_roles": ["tof"]},
            {"id": "brain_structures", "label": "Brain structures (CT)", "series_roles": ["ct_tissue"]},
        ],
    },
    {
        # The one tool here that consumes the original DICOM series rather
        # than a converted NIfTI (see TOOL_INPUT_KIND in API/app/batch_io.py),
        # so a cohort staged as NIfTI will skip every case with a note. Its
        # "modes" are dose levels -- checking several runs each of them
        # against every study, the same way several BraTS algorithms do.
        "id": "ldct",
        "label": "LDCT Simulation",
        "description": (
            "Simulates a reduced-dose acquisition from a full-dose CT series "
            "(Alsaihati method). Runs on every CT series in a study, each producing "
            "its own simulated DICOM series. "
            "Needs DICOM input, ~15-30 min per series."
        ),
        "enabled": True,
        "series_roles": ["ct"],
        "modes": [
            {"id": "dose10", "label": "10% dose"},
            {"id": "dose25", "label": "25% dose"},
            {"id": "dose50", "label": "50% dose"},
            {"id": "dose75", "label": "75% dose"},
        ],
    },
    {
        "id": "brats",
        "label": "BraTS Tumor Segmentation",
        "description": "Brain tumor segmentation from multi-sequence MRI (T1, T1c, T2, FLAIR).",
        "enabled": True,
        "series_roles": ["t1n", "t1c", "t2", "flair"],
        "modes": [
            {"id": "goat", "label": "GoAT (general adult tumor)"},
            {"id": "gliomapre", "label": "Adult glioma, pre-treatment"},
            {"id": "glioprepost", "label": "Adult glioma, pre- and post-treatment"},
            {"id": "meningioma", "label": "Meningioma"},
            {"id": "metastases", "label": "Metastases"},
            {"id": "pediatric", "label": "Pediatric"},
        ],
    },
    {
        "id": "fsl_register",
        "label": "FSL Template Registration",
        "description": (
            "Registers a T1 MRI to the MNI152 template with FSL FLIRT (optionally "
            "FNIRT on top) and returns the registered volume, a QC overlay snapshot "
            "and the correlation with the template. ~2-5 min per study (affine), "
            "considerably longer with FNIRT."
        ),
        "enabled": True,
        "series_roles": ["t1n"],
        "modes": [
            {"id": "flirt12", "label": "FLIRT affine, 12 DOF (as the study button)"},
            {"id": "flirt6", "label": "FLIRT rigid, 6 DOF"},
            {"id": "fnirt", "label": "FLIRT + FNIRT non-linear (BET first, slow)"},
        ],
    },
    {
        # Only one mode: FSL FIRST restricted to L/R hippocampus is the whole
        # tool, there is no second variant to pick (see SINGLE_MODE_TOOLS in
        # API/app/routes.py -- a batch entry is always a (tool, mode) pair,
        # so the single variant is still named).
        "id": "hippocampus",
        "label": "Hippocampus Segmentation",
        "description": (
            "Left/right hippocampus segmentation from a T1 MRI. ~30-60 min per study."
        ),
        "enabled": True,
        "series_roles": ["t1n"],
        "modes": [
            {"id": "first", "label": "FSL FIRST (left/right hippocampus)"},
        ],
    },
    {
        "id": "screw_infer",
        "label": "Screw Segmentation",
        "description": (
            "Runs the trained MONAI screw-segmentation model on a CT "
            "and returns the screw mask plus its volume. "
            "~2-5 min per study."
        ),
        "enabled": True,
        "series_roles": ["ct_screw"],
        "modes": [
            {"id": "infer", "label": "Trained model (GPU inference)"},
        ],
    },
    {
        # Unlike every tool above, this one looks past its own study at the
        # *other study folders the same patient has in this cohort* for its
        # timepoints -- upload several dated studies of the same patient
        # (under the same patient_id folder) to get a longitudinal run; a
        # patient with only one study in the cohort is skipped for this
        # tool (it has nothing to compare against). Single mode, like
        # hippocampus/screw_infer: there is no cross-sectional variant of
        # this tool (that's the plain "FastSurfer" entry above).
        "id": "fastsurfer_long",
        "label": "FastSurfer Longitudinal",
        "description": (
            "Longitudinal volumetry. "
            "~ 30 min per patient and timepoint."
        ),
        "enabled": True,
        "series_roles": ["t1n_acquired", "t1n"],
        "modes": [
            {"id": "long", "label": "Longitudinal stream (person-specific template)"},
        ],
    },
    {
        # Also patient-level like fastsurfer_long above, but with a
        # cross-sectional fallback built in: a patient with no partner study
        # in the cohort still gets a result (SIENAX), just not a change
        # measurement. Single mode: which of SIENA/SIENAX runs is decided
        # automatically per case, not chosen here.
        "id": "siena",
        "label": "SIENA / SIENAX Brain Atrophy",
        "description": (
            "Measures brain atrophy: longitudinal or cross-sectional. ~10-30 min per case."
        ),
        "enabled": True,
        "series_roles": ["t1n_acquired", "flair"],
        "modes": [
            {"id": "auto", "label": "Auto (longitudinal if a partner study is in the cohort)"},
        ],
    },
]

# First enabled tool's id -- default selection on page load (batch.html).
DEFAULT_TOOL_ID = next(t["id"] for t in BATCH_TOOLS if t["enabled"])

# Step 2's clinical-oriented layout: groups BATCH_TOOLS by modality then
# clinical task rather than presenting them as a flat toolbox list, since a
# radiologist reaches for "tumor" or "cerebral bleed", not for a tool's
# internal name. Purely presentational -- BATCH_TOOLS above stays the single
# source of truth for what a tool *is* (description, series_roles, modes,
# submit/validation id); an entry here just says *where* it's shown and,
# optionally, which of its modes belong there.
#
# totalsegmentator is the one tool whose modes span both modality and
# clinical intent (cerebral bleed / CT, brain structures / CT, aneurysm /
# TOF-MRA), so it's the only tool split across more than one entry below --
# each entry restricts it to the one relevant mode via "modes", gives it a
# "key" so the split rows get distinct DOM ids (they'd otherwise collide,
# since they all render id="tool-<id>"), and gives it a mode-specific
# "label"/"description" in place of "TotalSegmentator" and its generic
# description -- what's picked from a clinical category should be named
# after what it does (e.g. "Aneurysm Segmentation"), not the toolbox it
# comes from.
BATCH_TOOL_LAYOUT = [
    {
        "label": "MRI",
        "icon": "fa-brain",
        "categories": [
            {"label": "Tumor", "entries": [{"id": "brats"}]},
            {
                "label": "Volumetry & Atrophy",
                "entries": [
                    {"id": "fastsurfer"},
                    {"id": "synthseg"},
                    {"id": "fastsurfer_long"},
                    {"id": "hippocampus"},
                    {"id": "siena"},
                ],
            },
            {
                "label": "Vascular",
                "entries": [
                    {
                        "id": "totalsegmentator",
                        "key": "totalsegmentator-aneurysm",
                        "modes": ["brain_aneurysm"],
                        "label": "Aneurysm Segmentation",
                        "description": "Aneurysm segmentation from a time-of-flight MR angiogram.",
                    },
                ],
            },
            {"label": "Registration & QC", "entries": [{"id": "fsl_register"}]},
        ],
    },
    {
        "label": "CT",
        "icon": "fa-x-ray",
        "categories": [
            {
                "label": "Acute Findings",
                "entries": [
                    {
                        "id": "totalsegmentator",
                        "key": "totalsegmentator-bleed",
                        "modes": ["cerebral_bleed"],
                        "label": "Cerebral Bleed Segmentation",
                        "description": "Cerebral bleed segmentation from a soft-tissue-kernel CT.",
                    },
                ],
            },
            {
                "label": "Structural Segmentation",
                "entries": [
                    {
                        "id": "totalsegmentator",
                        "key": "totalsegmentator-structures",
                        "modes": ["brain_structures"],
                        "label": "Brain Structure Segmentation",
                        "description": "Normal brain structure segmentation from a soft-tissue-kernel CT.",
                    },
                ],
            },
            {"label": "Image Simulation / Research", "entries": [{"id": "ldct"}]},
        ],
    },
]


def _build_tool_groups():
    """Resolves BATCH_TOOL_LAYOUT against BATCH_TOOLS into the render-ready
    structure batch.html's Step 2 iterates: each entry carries the full tool
    dict (id, label, description, enabled, series_roles) plus its
    layout-scoped "modes" subset and a "dom_id" unique across the whole
    page, even for a tool (totalsegmentator) that appears more than once."""
    tools_by_id = {t["id"]: t for t in BATCH_TOOLS}
    groups = []
    for section in BATCH_TOOL_LAYOUT:
        categories = []
        for cat in section["categories"]:
            entries = []
            for layout_entry in cat["entries"]:
                tool = tools_by_id[layout_entry["id"]]
                entry = dict(tool)
                if "modes" in layout_entry:
                    wanted = set(layout_entry["modes"])
                    entry["modes"] = [m for m in tool["modes"] if m["id"] in wanted]
                if "label" in layout_entry:
                    entry["label"] = layout_entry["label"]
                if "description" in layout_entry:
                    entry["description"] = layout_entry["description"]
                entry["dom_id"] = layout_entry.get("key", tool["id"])
                entries.append(entry)
            categories.append({"label": cat["label"], "entries": entries})
        groups.append({
            "label": section["label"],
            "icon": section.get("icon"),
            "icon_svg": section.get("icon_svg"),
            "categories": categories,
        })
    return groups


def _sanitize_relpath(relpath):
    """Mirrors irods_browser._sanitize_relpath: a dropped folder's per-file
    path is untrusted input, so strip any "." / ".." segment rather than let
    a crafted drop write outside the staging directory."""
    return [p for p in relpath.replace("\\", "/").split("/") if p not in ("", ".", "..")]


def _to_irods_path(path):
    """A path picked via the iRODS browser modal is already an absolute
    logical path (see irods_browser.html's picker button, which posts back
    `current_path`) - this only guards against a hand-edited/missing value."""
    return "/" + (path or "").strip("/")


@batch_bp.route("/", methods=["GET"])
@oidc.require_login
def index():
    return render_template(
        "batch.html",
        title="Batch Processing",
        tools=BATCH_TOOLS,
        tool_groups=_build_tool_groups(),
        series_role_info=SERIES_ROLE_INFO,
        default_tool=DEFAULT_TOOL_ID,
        api_base=BATCH_API_BROWSER_BASE,
        irods_home=irods.home_collection(irods.current_username()),
        embedded=request.args.get("embedded") == "1",
    )


def _stage_upload_to_dir(dest_dir):
    """Write a folder-drop upload to disk under dest_dir, preserving
    structure. Same file[]/relpath[] shape as irods_browser.upload() (see
    batch.html's drop-zone JS, copied from irods_browser.html's) - adapted to
    land on the shared stage directory (BATCH_STAGE_ROOT) the batch API picks
    the tree up from, instead of writing each file straight to iRODS.

    A browser folder drop's relpath always carries the name of the folder the
    user actually dragged/selected as its first segment (e.g.
    "cohort/patient1/study1/t1n/0001.dcm") - but the batch API expects
    patient_id directories at the ZIP's top level. When every staged file
    shares that first segment (the normal case: one cohort folder dropped),
    drop it. If they don't all share one (several patient folders dragged in
    at once, each already named after its own patient_id), leave paths as
    they are - they're already patient_id-rooted.
    """
    uploaded_files = request.files.getlist("file")
    relpaths = request.form.getlist("relpath")
    if len(relpaths) != len(uploaded_files):
        relpaths = [None] * len(uploaded_files)

    entries = []
    for uploaded, relpath in zip(uploaded_files, relpaths):
        if not uploaded or not uploaded.filename:
            continue
        parts = _sanitize_relpath(relpath) if relpath else [os.path.basename(uploaded.filename)]
        if parts:
            entries.append((uploaded, parts))
    if not entries:
        return 0

    first_segments = {parts[0] for _, parts in entries if len(parts) > 1}
    strip_wrapper = len(first_segments) == 1

    count = 0
    for uploaded, parts in entries:
        if strip_wrapper and len(parts) > 1:
            parts = parts[1:]
        if not parts:
            continue
        target = os.path.join(dest_dir, *parts)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        uploaded.save(target)
        count += 1
    return count


def _new_stage_dir(stage_id):
    """Create this stage's directory on the shared API_storage mount (see
    BATCH_STAGE_ROOT) and return it. Named by the same random stage_id the
    page polls with, so nothing but the submitting session can name it."""
    work_dir = os.path.join(BATCH_STAGE_ROOT, stage_id)
    os.makedirs(work_dir, exist_ok=True)
    return work_dir


def _plan_needed_series(tools, entries):
    """Ask the batch API which series folders the selected `tools` (a
    batch can run several tools at once, see BATCH_TOOLS) actually
    need between them, and return the subset of `entries` worth
    downloading, plus the study folders to create empty.

    `tools` is [{"tool": ..., "mode": ...}, ...] -- one entry per checked
    tool. `entries` is _list_irods_tree's [(irods_path, size, rel_parts)]
    with rel_parts = [patient_id, study_name, series_name, ...file]. A real
    study holds 13-18 series and BraTS opens 4 of them, so this is where
    most of a cohort's transfer volume disappears. The decision itself is
    the API's (POST /batch/plan -> batch_io.plan_series_for_roles, unioned
    across every selected tool there), so SERIES_CRITERIA stays in one place
    instead of being copied into the GUI.

    Studies whose series match nothing are kept as empty directories: the
    API's walk still sees the study, and the case shows up in the job with
    its usual "missing required series" error rather than vanishing from the
    batch with no explanation.

    Falls back to "stage everything" (returning `entries` unchanged) if the
    API can't be reached or answers unexpectedly - a slower stage-in is a
    far better failure mode here than a silently under-staged cohort.
    Returns (entries_to_fetch, study_dirs, note) where note is a
    human-readable summary for the page, or None if nothing was filtered.
    """
    studies = {}  # (patient, study) -> [series_name, ...] in listing order
    for _obj_path, _size, parts in entries:
        if len(parts) < 4:
            continue
        key = (parts[0], parts[1])
        series = studies.setdefault(key, [])
        if parts[2] not in series:
            series.append(parts[2])
    if not studies:
        return entries, [], None

    payload = {
        "tools": tools,
        "studies": [
            {"patient_id": patient, "study_name": study, "series": series}
            for (patient, study), series in studies.items()
        ],
    }
    try:
        resp = requests.post(f"{BATCH_API_BASE}/batch/plan", json=payload, timeout=(5, 60))
        if resp.status_code != 200:
            return entries, [], None
        plan = resp.json()
        planned = {
            (s["patient_id"], s["study_name"]): set(s["series_needed"])
            for s in plan["studies"]
        }
    except (requests.exceptions.RequestException, ValueError, KeyError, TypeError):
        return entries, [], None

    kept = []
    for obj_path, size, parts in entries:
        # Anything not shaped patient/study/series/file (a stray file at the
        # top of a study or patient folder) isn't a series and isn't this
        # filter's business - pass it through.
        if len(parts) < 4 or parts[2] in planned.get((parts[0], parts[1]), set()):
            kept.append((obj_path, size, parts))

    study_dirs = [os.path.join(patient, study) for (patient, study) in studies]
    series_total = plan.get("series_total", 0)
    series_needed = plan.get("series_needed_total", 0)
    if series_needed >= series_total:
        return kept, study_dirs, None
    # dict.fromkeys, not set(): dedupes a tool selected under several modes
    # (e.g. two BraTS algorithms) while keeping selection order.
    tool_names = ", ".join(dict.fromkeys(t["tool"] for t in tools))
    note = (
        f"Staging {series_needed} of {series_total} series "
        f"({len(kept)} of {len(entries)} files) - the rest don't match "
        f"any series the selected tool(s) ({tool_names}) need."
    )
    return kept, study_dirs, note


def _parse_tools_field(raw):
    """Parse the `tools` form field (JSON array of {"tool", "mode"} objects,
    built client-side from the checked tool/mode checkboxes -- see
    batch.html's submit handler) shared by stage_irods() and submit().
    Validates each entry against BATCH_TOOLS/its per-tool `modes` -- the
    same checks the API itself makes on its own copy of this list
    (_batch_tool_spec), done here too so a bad selection fails with a
    page-friendly message before any iRODS staging or file upload happens.

    The same tool can appear more than once with a different mode (e.g.
    running several BraTS algorithms in one job) -- only the exact same
    (tool, mode) *pair* twice is rejected. Raises ValueError with a
    user-facing message."""
    try:
        tools = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        raise ValueError("Invalid tool selection.")
    if not isinstance(tools, list) or not tools:
        raise ValueError("Choose at least one tool to run.")

    seen = set()
    for entry in tools:
        if not isinstance(entry, dict) or "tool" not in entry:
            raise ValueError("Invalid tool selection.")
        tool_id, mode = entry["tool"], entry.get("mode")
        key = (tool_id, mode)
        if key in seen:
            raise ValueError(f"'{tool_id}' ({mode}) was selected more than once.")
        seen.add(key)
        tool_def = next((t for t in BATCH_TOOLS if t["id"] == tool_id), None)
        if tool_def is None or not tool_def["enabled"]:
            raise ValueError(f"Tool '{tool_id}' is not available for batch runs yet.")
        if mode not in [m["id"] for m in tool_def["modes"]]:
            raise ValueError(f"Invalid mode '{mode}' for {tool_id}.")
    return tools


def _list_irods_tree(irods_path):
    """Recursively list an iRODS collection's files via GenQuery only (no
    bytes downloaded) - metadata queries are cheap even over a few hundred
    directories, so this runs synchronously in the stage_irods route to give
    an immediate file/byte total for the progress bar and to fail fast on a
    bad path, before any background download work starts."""
    entries = []  # (irods_path, size, [rel_path_parts])

    def walk(coll_path, rel_parts):
        listing = irods.list_collection(coll_path)
        for sub in listing["collections"]:
            walk(sub["path"], rel_parts + [sub["name"]])
        for obj in listing["data_objects"]:
            entries.append((obj["path"], obj["size"], rel_parts + [obj["name"]]))

    walk(irods_path, [])
    return entries


# A stage-in's access token, refreshed in place as it nears expiry - shared
# with irods_browser.py's folder download, see oidc_auth.RefreshingToken.
_StageToken = oidc_auth.RefreshingToken


class _ResponseReader:
    """Minimal read()-only file object over a streaming response.

    tarfile's stream mode ("r|") only needs .read(n). Reading through
    iter_content rather than response.raw keeps requests' own content
    decoding in play, and never holds more than one chunk plus the current
    remainder in memory - a cohort's tar does not fit in RAM.
    """

    def __init__(self, response, chunk_size=1024 * 1024):
        self._chunks = response.iter_content(chunk_size=chunk_size)
        self._buf = b""

    def read(self, size=-1):
        if size is None or size < 0:
            out, self._buf = self._buf + b"".join(self._chunks), b""
            return out
        while len(self._buf) < size:
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                break
        out, self._buf = self._buf[:size], self._buf[size:]
        return out


def _safe_member_parts(name):
    """Path components to extract a tar member into, or None if unsafe.

    Nothing iRODS bundles should ever be absolute or contain "..", but
    extracting a member name unchecked is how a tar writes outside the
    directory it was aimed at, and this one is assembled from collection and
    file names the GUI does not control.
    """
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return parts


def _group_series_for_bundling(irods_root, entries):
    """Split `entries` into (series worth bundling, files to fetch singly).

    `entries` is _list_irods_tree's (irods_path, size, rel_parts) with
    rel_parts = [patient, study, series, ...file], so a series collection is
    the first three components under the cohort root. Anything shallower is
    a stray file rather than a series and is always fetched on its own, as
    is any series too small to earn back a bundle's own round trips
    (IRODS_STAGE_BUNDLE_MIN_FILES).
    """
    by_series = {}
    loose = []
    for entry in entries:
        parts = entry[2]
        if len(parts) >= 4:
            by_series.setdefault(tuple(parts[:3]), []).append(entry)
        else:
            loose.append(entry)

    bundles = []
    for (patient, study, series), group in by_series.items():
        if len(group) < IRODS_STAGE_BUNDLE_MIN_FILES:
            loose.extend(group)
            continue
        bundles.append({
            "coll": f"{irods_root.rstrip('/')}/{patient}/{study}/{series}",
            # Member names are relative to the bundled collection itself,
            # with no leading component for it (tarring .../<series> yields
            # "<file>", and "<subdir>/<file>" for anything nested), so they
            # extract into the series directory rather than its parent.
            "dest": [patient, study, series],
            "entries": group,
        })
    return bundles, loose


def _note_stage_progress(stage_id, files=0, written=0, current=None):
    with _stage_lock:
        stage = _stages[stage_id]
        stage["files_done"] += files
        stage["bytes_done"] += written
        if current is not None:
            stage["current_file"] = current


def _await_stage_futures(futures, stage_id, token_box):
    """Wait for `futures`, keeping the token fresh and watching for a stall.

    Returns (error, results): `error` is a message if one task raised or
    everything stopped moving, and `results` holds whatever finished.
    """
    pending = set(futures)
    results = []
    last_progress = time.time()
    with _stage_lock:
        last_files_done = _stages[stage_id]["files_done"]

    while pending:
        token_box.refresh_if_needed()
        done, pending = wait(pending, timeout=5, return_when=FIRST_COMPLETED)

        # Files landing counts as forward progress in its own right, not just
        # whole tasks finishing: one bundle can stream for far longer than
        # IRODS_STAGE_STALL_SECONDS while doing perfectly well.
        with _stage_lock:
            files_done = _stages[stage_id]["files_done"]
        if files_done != last_files_done:
            last_files_done = files_done
            last_progress = time.time()

        if not done:
            if time.time() - last_progress > IRODS_STAGE_STALL_SECONDS:
                return (
                    f"iRODS stopped responding after {IRODS_STAGE_STALL_SECONDS}s "
                    f"with {len(pending)} transfer(s) still in flight - try again "
                    "with a smaller folder, or retry later."
                ), results
            continue

        last_progress = time.time()
        for future in done:
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - see _run_irods_stage
                return str(exc), results
    return None, results


def _run_irods_stage(stage_id, irods_root, entries, work_dir, token_box, actor):
    """Background worker: download every (path, size, rel_parts) in `entries`
    into `work_dir`, several at once. Updates the stage record in `_stages`
    as files land so stage_status() has live progress to report; the actual
    zip-building still happens later, in submit(), once everything is on
    local disk.

    Two passes. Each series big enough to be worth it is tarred server-side
    and fetched as a single object (irods_client.create_tar_bundle), which is
    what makes this fast: the per-file "open, read, close" round trip was
    always the cost here, not the bytes - the same cohort tars off the vault
    in about a second. Whatever is left over - stray files, series too small
    to bundle, and any series whose bundle failed - is then fetched file by
    file, the way everything used to be.

    A series that cannot be bundled is therefore slower rather than broken,
    which is deliberate: bundling needs a rule engine that understands
    msiTarFileCreate and read ACLs on every object, and neither is worth
    turning a working stage-in into a failed one over.

    `token_box` is a _StageToken seeded synchronously in the stage_irods()
    request handler (via oidc_auth.token_refresh_bundle()) - this function
    and the worker threads it spawns run with no Flask request (or session)
    active at all, so irods_client's normal "pull it from the session"
    default (oidc.get_access_token()) would raise "Working outside of
    request context" the moment a download started, and a plain captured
    token string would go stale partway through a large stage-in (see
    _StageToken).
    `actor` is captured the same way and for the same reason: the PHI audit
    trail must name the person whose cohort is being staged, and these threads
    have no session for app/utils/audit.py to read a username out of.

    Each pass waits via _await_stage_futures rather than as_completed()/a
    blocking `with ThreadPoolExecutor()`, so a stalled read (the
    HTTP API can wedge a request outright under sustained concurrent
    streaming, with neither side raising an error) doesn't hang this function
    - and therefore stage_status() - forever. pool.shutdown(wait=False) on
    the way out means a genuinely wedged worker thread (still blocked on a
    socket read the server never answers) is abandoned rather than joined: it
    leaks one idle thread per stalled transfer, bounded by the pass's own
    concurrency, in exchange for this function actually returning."""

    def fetch_one(entry):
        obj_path, _size, parts = entry
        target = os.path.join(work_dir, *parts)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            # close_connection=True: force a fresh connection per file rather
            # than reusing one from irods_client's shared pool. The stall this
            # is working around only appeared under sustained concurrent reuse
            # of pooled connections (see IRODS_STAGE_CONCURRENCY's comment) -
            # this is a targeted mitigation for that, not a proven root cause,
            # which is why the watchdog exists regardless.
            upstream = irods.read_data_object(
                obj_path, token=token_box.get(), close_connection=True, actor=actor
            )
            written = 0
            with open(target, "wb") as f:
                for chunk in upstream.iter_content(chunk_size=256 * 1024):
                    f.write(chunk)
                    written += len(chunk)
        except Exception as exc:  # noqa: BLE001 - any failure aborts the
            # stage rather than silently handing the batch API an incomplete
            # cohort, or hanging stage_status at "downloading" forever with no
            # way for the page to know it died. Not just IrodsError: a bug
            # here (or a future change that calls something session-bound
            # without a token= override) fails with a bare RuntimeError and
            # still needs to surface to the user.
            raise RuntimeError(f"{'/'.join(parts)}: {exc}") from exc
        _note_stage_progress(stage_id, files=1, written=written,
                             current="/".join(parts))

    def fetch_bundle(bundle):
        """Tar one series server-side, stream it back, extract it as it comes.

        Returns None once the series is on disk, or the bundle itself if it
        could not be fetched this way - the caller then falls back to
        per-file downloads for exactly that series.
        """
        dest_dir = os.path.join(work_dir, *bundle["dest"])
        # Unique per attempt: a bundle that fails midway can leave a locked
        # zero-byte object behind that iRODS then refuses to overwrite *or*
        # remove, which a stable name would turn into a permanent failure.
        tar_lpath = f"{bundle_coll}/{uuid.uuid4().hex}.tar"
        try:
            irods.create_tar_bundle(
                bundle["coll"], tar_lpath, token=token_box.get(), actor=actor,
                file_count=len(bundle["entries"]),
            )
        except (IrodsError, requests.exceptions.RequestException):
            return bundle

        files = written = 0
        try:
            upstream = irods.read_data_object(
                tar_lpath, token=token_box.get(), close_connection=True, actor=actor
            )
            with tarfile.open(fileobj=_ResponseReader(upstream), mode="r|") as tar:
                for member in tar:
                    if not member.isfile():
                        continue
                    parts = _safe_member_parts(member.name)
                    if parts is None:
                        raise IrodsError(
                            f"Refusing to extract {member.name!r} from {tar_lpath}."
                        )
                    target = os.path.join(dest_dir, *parts)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with open(target, "wb") as f:
                        shutil.copyfileobj(tar.extractfile(member), f)
                    files += 1
                    written += member.size
                    _note_stage_progress(
                        stage_id, files=1, written=member.size,
                        current="/".join(bundle["dest"] + parts),
                    )
        except Exception:  # noqa: BLE001 - any failure just costs this series
            # its fast path. Give back what this bundle already counted, so
            # the per-file retry doesn't count those files a second time and
            # push the progress bar past its own total.
            _note_stage_progress(stage_id, files=-files, written=-written)
            return bundle
        finally:
            try:
                irods.remove_data_object(tar_lpath, token=token_box.get(), actor=actor)
            except (IrodsError, requests.exceptions.RequestException):
                # See above on locked leftovers: the name is unique per
                # attempt, so failing to clean up costs vault space rather
                # than blocking the next try.
                pass
        return None

    def run_pass(fn, items, workers):
        pool = ThreadPoolExecutor(max_workers=min(workers, len(items)))
        try:
            return _await_stage_futures(
                [pool.submit(fn, item) for item in items], stage_id, token_box
            )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    bundles, singles = [], list(entries)
    bundle_coll = None
    if IRODS_STAGE_BUNDLE:
        bundles, singles = _group_series_for_bundling(irods_root, entries)
        if bundles:
            bundle_coll = (
                f"{irods.home_collection(actor)}/{IRODS_STAGE_BUNDLE_DIRNAME}"
            )
            try:
                irods.create_collection(
                    bundle_coll, token=token_box.get(), actor=actor
                )
            except IrodsAlreadyExists:
                pass
            except (IrodsError, requests.exceptions.RequestException):
                # Nowhere to put the tars: stage the whole cohort per-file.
                bundles, singles = [], list(entries)

    error = None
    try:
        if bundles:
            error, results = run_pass(
                fetch_bundle, bundles, IRODS_STAGE_BUNDLE_CONCURRENCY
            )
            for failed in results:
                if failed is not None:
                    singles.extend(failed["entries"])
        if error is None and singles:
            error, _ = run_pass(fetch_one, singles, IRODS_STAGE_CONCURRENCY)
    except Exception as exc:  # noqa: BLE001 - must not leave the stage record
        # stuck at "downloading" on an unexpected failure.
        error = error or str(exc)

    with _stage_lock:
        stage = _stages[stage_id]
        if error:
            stage["status"] = "error"
            stage["error"] = error
        else:
            stage["status"] = "done"


def _sweep_stale_stages():
    """Drop finished/errored stage records (and their scratch dirs) older
    than _STAGE_TTL_SECONDS - opportunistic, run on every new stage_irods
    call rather than via a separate timer thread."""
    cutoff = time.time() - _STAGE_TTL_SECONDS
    with _stage_lock:
        stale = [
            sid for sid, s in _stages.items()
            if s["status"] in ("done", "error") and s["created_at"] < cutoff
        ]
        stale_dirs = [_stages[sid]["work_dir"] for sid in stale]
        for sid in stale:
            del _stages[sid]
        known = {s["work_dir"] for s in _stages.values()}
    for work_dir in stale_dirs:
        shutil.rmtree(work_dir, ignore_errors=True)

    # Plus any directory left behind by a previous run of this process -
    # see _ORPHAN_STAGE_TTL_SECONDS.
    orphan_cutoff = time.time() - _ORPHAN_STAGE_TTL_SECONDS
    try:
        candidates = os.listdir(BATCH_STAGE_ROOT)
    except OSError:
        return
    for name in candidates:
        path = os.path.join(BATCH_STAGE_ROOT, name)
        try:
            if path in known or not os.path.isdir(path):
                continue
            if os.stat(path).st_mtime >= orphan_cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)


@batch_bp.route("/stage_irods", methods=["POST"])
@oidc.require_login
def stage_irods():
    """Kick off the stage-in download in the background and return right
    away with a stage_id to poll (a synchronous-edge download,
    but not blocking the request for it - see IRODS_STAGE_CONCURRENCY's
    comment on why a single-threaded, no-feedback version of this was slow
    enough that a 0.8 GB test cohort looked hung for 5-10 minutes).

    Lists the whole tree first (GenQuery, no bytes) so a bad/empty path
    fails fast and the caller gets a real file/byte total before the
    background download starts, then narrows that list to the series the
    selected tools will actually open (_plan_needed_series) - which is why
    this route needs `tools` and not just the path."""
    irods_input_path = (request.form.get("irods_input_path") or "").strip()
    if not irods_input_path:
        return jsonify({"error": "Choose an iRODS folder to process."}), 400
    try:
        tools = _parse_tools_field(request.form.get("tools"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    irods_path = _to_irods_path(irods_input_path)

    try:
        entries = _list_irods_tree(irods_path)
    except IrodsError as exc:
        return jsonify({"error": f"Could not read {irods_input_path} from iRODS: {exc}"}), 502
    if not entries:
        return jsonify({"error": f"No files found under {irods_input_path} in iRODS."}), 400

    entries, study_dirs, plan_note = _plan_needed_series(tools, entries)

    _sweep_stale_stages()

    # Captured here, inside the request, and handed explicitly to the
    # background thread below - that thread has no Flask session of its own
    # to pull a token from (see _run_irods_stage's docstring). The bundle
    # carries a refresh token too where one is available, so _StageToken can
    # keep the access token alive for a stage-in that outlasts it.
    token_bundle = oidc_auth.token_refresh_bundle() or {
        "access_token": irods.current_access_token(), "expires_at": None,
    }
    token_box = _StageToken(token_bundle)
    actor = irods.current_username()

    stage_id = uuid.uuid4().hex
    work_dir = _new_stage_dir(stage_id)
    # Every study gets its folder up front, including any whose series all
    # got filtered out - see _plan_needed_series on why they must not just
    # disappear from the batch.
    for rel_dir in study_dirs:
        os.makedirs(os.path.join(work_dir, rel_dir), exist_ok=True)

    stage = {
        "status": "downloading",
        "files_done": 0,
        "files_total": len(entries),
        "bytes_done": 0,
        "bytes_total": sum(size for _, size, _ in entries),
        "current_file": None,
        "error": None,
        "note": plan_note,
        "work_dir": work_dir,
        "created_at": time.time(),
    }
    with _stage_lock:
        _stages[stage_id] = stage

    threading.Thread(
        target=_run_irods_stage,
        args=(stage_id, irods_path, entries, work_dir, token_box, actor),
        name=f"batch-stage-{stage_id}", daemon=True,
    ).start()

    return jsonify({
        "stage_id": stage_id,
        "files_total": stage["files_total"],
        "bytes_total": stage["bytes_total"],
        "note": plan_note,
    }), 202


@batch_bp.route("/stage_status/<stage_id>", methods=["GET"])
@oidc.require_login
def stage_status(stage_id):
    with _stage_lock:
        stage = _stages.get(stage_id)
        if stage is None:
            return jsonify({"error": "Unknown or expired stage id - pick the iRODS folder again."}), 404
        return jsonify({
            "status": stage["status"],
            "files_done": stage["files_done"],
            "files_total": stage["files_total"],
            "bytes_done": stage["bytes_done"],
            "bytes_total": stage["bytes_total"],
            "current_file": stage["current_file"],
            "note": stage.get("note"),
            "error": stage["error"],
        })


@batch_bp.route("/submit", methods=["POST"])
@oidc.require_login
def submit():
    """Hand the chosen input to the batch API's POST /batch/run and return
    its job_id/status_url JSON as-is (plus the output choice, so the page
    knows whether to offer "push to iRODS" once the job is done) for
    batch.html's JS to poll directly against the API.

    The cohort itself is not sent: it is already on the API_storage mount
    both containers share (BATCH_STAGE_ROOT), so this posts `input_path` and
    the API moves that tree into the job's work dir. Nothing but the form
    fields crosses the network.

    For input_source == "irods", the download already happened in
    stage_irods()/_run_irods_stage() (with progress the page could poll),
    into the directory named by `stage_id`. For input_source == "upload" the
    folder-drop upload is written to a stage directory here, inline - a
    browser-side folder drop is already fast enough not to need the same
    background-job treatment.

    Either way the staged directory stops being this blueprint's to clean up
    the moment the API accepts it (the API moves it); the cleanup below only
    fires on the paths where the handoff didn't happen."""
    try:
        tools = _parse_tools_field(request.form.get("tools"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    input_source = request.form.get("input_source")
    output_mode = request.form.get("output_mode", "zip")
    irods_output_path = (request.form.get("irods_output_path") or "").strip()
    use_all_series = request.form.get("use_all_series") == "1"

    if output_mode == "irods" and not irods_output_path:
        return jsonify({"error": "Choose an iRODS destination folder."}), 400

    stage_id = None  # set for input_source == "irods": owned by _stages, not this request

    if input_source == "irods":
        stage_id = request.form.get("stage_id")
        with _stage_lock:
            stage = _stages.get(stage_id)
        if stage is None:
            return jsonify({"error": "iRODS input was not staged (or has expired) - pick the folder again."}), 400
        if stage["status"] != "done":
            return jsonify({"error": f"iRODS input is still staging (status: {stage['status']})."}), 409
        input_dir = stage["work_dir"]
    elif input_source == "upload":
        input_dir = _new_stage_dir(uuid.uuid4().hex)
        n = _stage_upload_to_dir(input_dir)
        if n == 0:
            shutil.rmtree(input_dir, ignore_errors=True)
            return jsonify({"error": "Choose a folder to upload."}), 400
    else:
        return jsonify({"error": "Choose an input source."}), 400

    try:
        resp = requests.post(
            f"{BATCH_API_BASE}/batch/run",
            data={
                "tools": json.dumps(tools),
                "input_path": input_dir,
                "owner": irods.current_username(),
                "output_mode": output_mode,
                "irods_output_path": irods_output_path,
                "use_all_series": "1" if use_all_series else "0",
            },
            # Read timeout, not just "big response, slow link": _start_batch_job
            # (API/app/routes.py) still does series->role matching for the
            # WHOLE cohort synchronously before it answers - moving the staged
            # directory itself is instant (same filesystem, so shutil.move is
            # a rename), and role matching is cheap (name scoring plus a
            # stop-before-pixels header read per series, no subprocess). The
            # dcm2niix conversion that used to run inline here (and was slow
            # enough on a 20+ GB cohort to need a 3600s budget) is now
            # deferred into batch_jobs.py's per-case worker pool -- see
            # batch_io.materialize_tool_input. 120s comfortably covers
            # role-matching even a very large cohort; if that ever changes,
            # revisit alongside the /batch/run response time itself.
            timeout=(5, 120),
        )
    except requests.exceptions.RequestException as exc:
        shutil.rmtree(input_dir, ignore_errors=True)
        return jsonify({"error": f"Could not reach the batch processing service: {exc}"}), 502
    finally:
        # The directory itself is the API's now (it moves it) - drop only
        # this blueprint's bookkeeping for it. rmtree on the error paths is
        # harmless either way: it's ignore_errors, and after a successful
        # handoff there is nothing left at that path to remove.
        if stage_id:
            with _stage_lock:
                _stages.pop(stage_id, None)

    if resp.status_code not in (200, 202):
        # Rejected before the API took the directory over, so it's still
        # this side's to clean up.
        shutil.rmtree(input_dir, ignore_errors=True)
        try:
            detail = resp.json().get("error", resp.text)
        except ValueError:
            detail = resp.text
        return jsonify({"error": detail}), resp.status_code

    payload = resp.json()
    payload["output_mode"] = output_mode
    payload["irods_output_path"] = irods_output_path
    payload["api_base"] = BATCH_API_BROWSER_BASE
    return jsonify(payload), 202


@batch_bp.route("/my_jobs", methods=["GET"])
@oidc.require_login
def my_jobs():
    """List the logged-in user's batch jobs (summary only), for batch.html
    to restore "Your Batch Jobs" on page load -- including after a
    logout/login, since this is pulled from the API's persisted job state
    rather than anything held in the browser. Just a thin proxy onto the
    batch API's GET /batch/jobs?owner=<owner>, with `owner` supplied here
    (server-side, from this person's Keycloak session) rather than trusted
    from the browser."""
    try:
        resp = requests.get(
            f"{BATCH_API_BASE}/batch/jobs",
            params={"owner": irods.current_username()},
            timeout=(5, 30),
        )
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": f"Could not reach the batch processing service: {exc}"}), 502
    if resp.status_code != 200:
        return jsonify({"error": "Could not list batch jobs."}), 502

    payload = resp.json()
    payload["api_base"] = BATCH_API_BROWSER_BASE
    return jsonify(payload)


def _owned_job_or_error(job_id):
    """Shared owner-check for every per-job action below (delete/pause/
    resume/stop): the batch API has no auth of its own, so it's this
    blueprint's job to refuse a job_id that isn't this person's before ever
    forwarding the action. Returns (status_dict, None) on success, or
    (None, (response, code)) with a ready-to-return Flask response on
    failure -- including the "already gone" 404 case, since a caller acting
    on a job (delete in particular) should treat that as success, not an
    error, but every caller needs the same not-found check first."""
    try:
        status_resp = requests.get(f"{BATCH_API_BASE}/batch/status/{job_id}", timeout=(5, 30))
    except requests.exceptions.RequestException as exc:
        return None, (jsonify({"error": f"Could not reach the batch processing service: {exc}"}), 502)
    if status_resp.status_code == 404:
        return None, (None, 404)
    try:
        status = status_resp.json()
    except ValueError:
        status = {}
    if status_resp.status_code != 200:
        return None, (jsonify({"error": status.get("error", "Unknown batch job.")}), status_resp.status_code)
    job_owner = status.get("owner")
    if job_owner and job_owner != irods.current_username():
        return None, (jsonify({"error": "This batch job belongs to a different user."}), 403)
    return status, None


@batch_bp.route("/job/<job_id>", methods=["DELETE"])
@oidc.require_login
def delete_job(job_id):
    """Delete one job from "Your Batch Jobs" (the per-job trash-can button
    in batch.html, confirmed client-side before this is ever called)."""
    status, err = _owned_job_or_error(job_id)
    if err is not None:
        response, code = err
        return (jsonify({"deleted": job_id}) if code == 404 else response), (200 if code == 404 else code)

    try:
        del_resp = requests.delete(f"{BATCH_API_BASE}/batch/jobs/{job_id}", timeout=(5, 30))
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": f"Could not reach the batch processing service: {exc}"}), 502
    try:
        del_data = del_resp.json()
    except ValueError:
        del_data = {}
    if del_resp.status_code not in (200, 404):
        return jsonify({"error": del_data.get("error", "Could not delete batch job.")}), del_resp.status_code
    return jsonify({"deleted": job_id})


def _forward_job_action(job_id, action):
    """POST {job_id}/{action} to the batch API once ownership is confirmed --
    shared by pause/resume/stop below, which differ only in which API path
    they hit and that a job gone by the time the button is clicked is a
    no-op success only for stop (a still-unfinished job that vanished from
    under the user isn't the delete route's "already gone" case)."""
    status, err = _owned_job_or_error(job_id)
    if err is not None:
        response, code = err
        if code == 404:
            return jsonify({"error": "Unknown batch job id."}), 404
        return response, code

    try:
        resp = requests.post(f"{BATCH_API_BASE}/batch/jobs/{job_id}/{action}", timeout=(5, 30))
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": f"Could not reach the batch processing service: {exc}"}), 502
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200:
        return jsonify({"error": data.get("error", f"Could not {action} batch job.")}), resp.status_code
    return jsonify(data)


@batch_bp.route("/job/<job_id>/pause", methods=["POST"])
@oidc.require_login
def pause_job(job_id):
    """Pause a running job (the per-job pause button in batch.html)."""
    return _forward_job_action(job_id, "pause")


@batch_bp.route("/job/<job_id>/resume", methods=["POST"])
@oidc.require_login
def resume_job(job_id):
    """Resume a paused job (the per-job resume button in batch.html)."""
    return _forward_job_action(job_id, "resume")


@batch_bp.route("/job/<job_id>/stop", methods=["POST"])
@oidc.require_login
def stop_job(job_id):
    """Stop a job for good (the per-job stop button in batch.html, confirmed
    client-side before this is ever called)."""
    return _forward_job_action(job_id, "stop")


@batch_bp.route("/jobs", methods=["DELETE"])
@oidc.require_login
def clear_jobs():
    """Delete every finished job this user owns (the "Clear history" button
    above "Your Batch Jobs"). Loops delete_job's same API call rather than
    adding a bulk-delete endpoint on the API side - a user's job list is
    small enough (dozens, not thousands) that this isn't worth a second
    code path there. Jobs still running are left alone (see batch_jobs.
    delete_job on the API side) and reported back as `skipped`."""
    try:
        resp = requests.get(
            f"{BATCH_API_BASE}/batch/jobs",
            params={"owner": irods.current_username()},
            timeout=(5, 30),
        )
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": f"Could not reach the batch processing service: {exc}"}), 502
    if resp.status_code != 200:
        return jsonify({"error": "Could not list batch jobs."}), 502

    deleted, skipped, failed = [], [], []
    for j in resp.json().get("jobs", []):
        jid = j["job_id"]
        try:
            del_resp = requests.delete(f"{BATCH_API_BASE}/batch/jobs/{jid}", timeout=(5, 30))
        except requests.exceptions.RequestException:
            failed.append(jid)
            continue
        if del_resp.status_code in (200, 404):
            deleted.append(jid)
        elif del_resp.status_code == 409:
            skipped.append(jid)
        else:
            failed.append(jid)

    return jsonify({"deleted": deleted, "skipped": skipped, "failed": failed})


@batch_bp.route("/push_to_irods/<job_id>", methods=["POST"])
@oidc.require_login
def push_to_irods(job_id):
    """Stage-out: pull a batch job's result ZIP (the full
    result once "done", or whatever cases finished before a pause/stop --
    see batch_result_route on the API side) and re-upload its tree into
    iRODS under the chosen destination. A separate action the user clicks
    rather than something the batch job triggers itself on completion, so
    the iRODS write always carries a fresh token - a multi-hour/day batch
    can easily outlive the token that was current when the job was
    submitted.

    Unlike plain status polling/ZIP download (left as direct browser->API
    calls, see this module's docstring), this action moves another user's
    data into the caller's own iRODS space, so it's worth actually checking
    `owner` here rather than just filtering it out of a list: a job with a
    recorded owner that isn't this person is refused. A job with no owner
    recorded (submitted before this existed, or outside the GUI) is left
    unrestricted, matching the batch API's baseline no-auth behavior."""
    dest_path = _to_irods_path(request.form.get("irods_output_path"))
    if dest_path == "/":
        return jsonify({"error": "Choose an iRODS destination folder."}), 400

    try:
        status_resp = requests.get(f"{BATCH_API_BASE}/batch/status/{job_id}", timeout=(5, 30))
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": f"Could not reach the batch processing service: {exc}"}), 502
    try:
        status = status_resp.json()
    except ValueError:
        status = {}
    if status_resp.status_code != 200:
        return jsonify({"error": status.get("error", "Unknown batch job.")}), status_resp.status_code
    job_owner = status.get("owner")
    if job_owner and job_owner != irods.current_username():
        return jsonify({"error": "This batch job belongs to a different user."}), 403
    if status.get("status") not in ("done", "paused", "stopped"):
        return jsonify({"error": f"Batch job is not finished yet (status: {status.get('status')})."}), 409

    try:
        result_resp = requests.get(f"{BATCH_API_BASE}/batch/result/{job_id}", timeout=(5, 600), stream=True)
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": f"Could not download batch results: {exc}"}), 502
    if result_resp.status_code != 200:
        return jsonify({"error": "Could not download batch results."}), 502

    work_dir = tempfile.mkdtemp(prefix="batch_result_")
    try:
        zip_path = os.path.join(work_dir, "result.zip")
        with open(zip_path, "wb") as f:
            for chunk in result_resp.iter_content(chunk_size=64 * 1024):
                f.write(chunk)

        extract_dir = os.path.join(work_dir, "extracted")
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)

        try:
            irods.create_collection(dest_path)
        except IrodsAlreadyExists:
            pass

        dirs_needed = set()
        files_to_upload = []
        for root, _dirs, files in os.walk(extract_dir):
            for name in files:
                full = os.path.join(root, name)
                parts = os.path.relpath(full, extract_dir).replace("\\", "/").split("/")
                for depth in range(1, len(parts)):
                    dirs_needed.add(tuple(parts[:depth]))
                files_to_upload.append((full, parts))

        for dir_parts in sorted(dirs_needed, key=len):
            try:
                irods.create_collection(f"{dest_path.rstrip('/')}/{'/'.join(dir_parts)}")
            except IrodsAlreadyExists:
                pass

        # Concurrently, for the same reason the stage-in above reads
        # concurrently: a data object costs ~25-30 ms in iRODS whatever its
        # size, so a result set of a few thousand small files is bounded by
        # round trips, not bytes. See irods_client.WRITE_CONCURRENCY.
        results = irods.write_data_objects(
            (f"{dest_path.rstrip('/')}/{'/'.join(parts)}", (lambda p=full: open(p, "rb")))
            for full, parts in files_to_upload
        )
        uploaded, failed = 0, []
        for (_full, parts), (_dest, exc) in zip(files_to_upload, results):
            if exc is None:
                uploaded += 1
            else:
                failed.append(f"{'/'.join(parts)} ({exc})")
    except IrodsError as exc:
        return jsonify({"error": str(exc)}), 502
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if failed:
        return jsonify({
            "uploaded": uploaded, "failed": failed,
            "message": f"Pushed {uploaded} file(s) to {dest_path}; {len(failed)} failed.",
        }), 207
    return jsonify({"uploaded": uploaded, "message": f"Pushed {uploaded} file(s) to {dest_path}."})
