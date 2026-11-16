import csv
import html
import io
import json
import logging
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime

import pydicom
from flask import Blueprint, render_template, request, send_file
from werkzeug.utils import secure_filename

from .backend_helpers import (
    client, log, retrieve_series_sorted, series_to_zip, series_to_nifti,
    upload_mask_file_as_segmentation,
)
from . import batch_jobs
from .job_logging import timed
from .batch_io import (
    TOOL_INPUT_KIND, TOOL_MODALITY, TOOL_SERIES_SELECTION, compute_study_dates,
    materialize_timepoint_inputs, materialize_tool_input, plan_series_for_roles,
    prepare_batch_for_tools, resolve_staged_input_dir, unpack_batch_zip, zip_output_tree,
)
from .brats_functions import (
    brats_wrapper, brats_core, brats_batch_stats, save_brats_core_outputs, ensure_segmenter_image_pulled,
    ensure_brats_preprocessing_assets_pulled,
)
from .total_segmentator_functions import (
    totalsegmentator_wrapper, totalsegmentator_core, totalsegmentator_batch_stats,
    save_totalsegmentator_core_outputs, TASK_REQUIRED_SERIES, TASK_MODALITY,
)
from .fastsurfer_functions import (
    fastsurfer_wrapper, fastsurfer_core, fastsurfer_batch_stats, save_fastsurfer_core_outputs,
)
from .synthseg_functions import (
    synthseg_wrapper, synthseg_core, synthseg_batch_stats, save_synthseg_core_outputs,
    SYNTHSEG_MODES, SYNTHSEG_CONTRASTS, SYNTHSEG_ROLES, CONTRAST_FOR_ROLE,
)
from .fastsurfer_long_functions import (
    fastsurfer_long_wrapper, fastsurfer_long_core, fastsurfer_long_batch_stats,
    plan_longitudinal_fastsurfer_batch, save_fastsurfer_long_core_outputs, LONGITUDINAL_ROLES,
)
from .coreg_functions import coreg_wrapper
from .dti_functions import (
    dti_tensor_wrapper, dti_tractography_wrapper, dti_reupload_wrapper,
    STORAGE_ROOT as DTI_STORAGE_ROOT,
)
from .fsl_registration_functions import (
    fsl_register_qc_wrapper, fsl_register_core, fsl_register_batch_stats,
    save_fsl_register_core_outputs, FSL_REGISTRATION_MODES,
)
from .ldct_functions import (
    ldct_wrapper, ldct_core, ldct_batch_stats, LDCT_DOSE_MODES,
)
from .screw_inference_functions import (
    screw_inference_wrapper, screw_inference_core, screw_inference_batch_stats,
    save_screw_inference_core_outputs,
)
from .hippocampus_functions import (
    hippocampus_wrapper, hippocampus_core, hippocampus_batch_stats,
    save_hippocampus_core_outputs, upload_reoriented_hippocampus_series,
    DOWNLOADABLE_FILES, FILES_BASE_URL,
)
from .siena_functions import (
    siena_wrapper, validate_bet_options, DEFAULT_BET_OPTIONS, siena_core, sienax_core,
    siena_batch_stats, sienax_batch_stats, plan_siena_analysis_batch, save_siena_core_outputs,
    save_sienax_core_outputs, SIENA_ROLES,
)
from .nifti_upload import convert_and_store_nifti
from .nrrd_upload import convert_and_store_nrrd
from .jobs import logged_work, start_bg_job, job_status_response, cors_file, cors_json

logger = logging.getLogger('interact.routes')
from .storage_paths import API_STORAGE_DIR

# Required series role(s) per batch-eligible tool whose modality is fixed
# regardless of the mode/segmenter chosen (fastsurfer, brats, ldct).
# TotalSegmentator isn't listed here -- its required roles (and their
# modality) vary per task; see TASK_REQUIRED_SERIES / TASK_MODALITY in
# total_segmentator_functions.py.
BATCH_TOOL_ROLES = {
    'fastsurfer': ['t1n'],
    # Both roles are listed so the batch stage-in converts and materializes
    # whichever the study has, but -- like LONGITUDINAL_ROLES and SIENA_ROLES
    # below -- they are *alternatives*, not a simultaneous requirement.
    # SynthSeg segments each contrast independently and is contrast-agnostic,
    # so a study with only a T1, or only a T2, still gets a complete result on
    # what it has; only a study with neither has nothing to run.
    # _run_synthseg_tool therefore does its own check rather than going
    # through _require_tool_input, which demands every declared role and would
    # skip most of a real cohort here.
    #
    # Its own roles rather than the shared 't1n'/'t2': those require an MPRAGE
    # and a TSE respectively, which would skip the plain "T1 ax"/"T2 ax"
    # clinical series this tool is meant for. See SERIES_CRITERIA in
    # backend_helpers.py.
    'synthseg': list(SYNTHSEG_ROLES),
    'brats': ['t1n', 't1c', 't2', 'flair'],
    'ldct': ['ct'],
    'fsl_register': ['t1n'],
    'hippocampus': ['t1n'],
    # Any CT except a previous screw run's own derived series -- the batch
    # equivalent of screw_functions._find_ct_series_uid()'s exclusion, see
    # SERIES_CRITERIA in backend_helpers.py.
    'screw_infer': ['ct_screw'],
    # These two roles cover this *study's* own plausibility check only (used
    # by prepare_batch_for_tools/_require_tool_input as a fast-path "does
    # this study have anything a longitudinal run could use at all"), not
    # the actual cross-study timepoint selection -- see
    # _run_fastsurfer_long_tool/_run_siena_tool, which look past this study
    # at the rest of the patient's studies in the cohort themselves.
    'fastsurfer_long': list(LONGITUDINAL_ROLES),
    'siena': list(SIENA_ROLES),
}

# Tools the batch form's "use every matching series" checkbox (_start_batch_job's
# `use_all_series` form field) does not apply to -- they keep 'roles'
# (best-series-per-role) regardless of the checkbox:
#
#   'brats' genuinely needs one T1+T1c+T2+FLAIR combination at once, so
#     there is no single role here to fan out over without combining across
#     roles (which the checkbox deliberately does not do -- see
#     batch_io.map_series_to_roles_all's docstring).
#   'fastsurfer_long'/'siena' pick a series from *other* studies in the
#     cohort entirely, not from this study's candidates (see
#     _run_fastsurfer_long_tool/_run_siena_tool) -- there is no per-study
#     "candidate series for a role" step here for the checkbox to affect.
ALL_MATCHING_EXCLUDED_TOOLS = {'brats', 'fastsurfer_long', 'siena'}

# A tool whose `mode` is a single fixed variant rather than a real choice
# (unlike FastSurfer's seg/full or BraTS's segmenters). The batch pipeline
# keys everything -- job params, scratch/result directories,
# results_overview.csv columns -- by (tool, mode) pairs (see
# _validate_tool_entries), so every tool needs *a* mode; these declare the
# one they have instead of inventing an option the tool doesn't offer.
#
# 'siena' too: which of SIENA/SIENAX actually runs is decided per case by
# plan_siena_analysis_batch (mirroring the interactive button), not chosen
# by the caller, so there is nothing to expose as a real mode either.
SINGLE_MODE_TOOLS = {
    'hippocampus': ('first', 'Hippocampus (FSL FIRST)'),
    'fastsurfer_long': ('long', 'FastSurfer Longitudinal (person-specific template + trajectories)'),
    'siena': ('auto', 'SIENA / SIENAX (auto-detects a longitudinal partner in this cohort)'),
    'screw_infer': ('infer', 'Screw segmentation (live inference)'),
}


def _batch_tool_spec(tool, mode):
    """Resolve a (tool, mode) pair to (required_roles, tool_modality, label).
    `mode` is whichever second choice that tool exposes -- FastSurfer's
    mode, TotalSegmentator's task_name, BraTS's segmenter_name.

    Single source of truth for the roles/modality tables, shared by
    _start_batch_job (one job can run several (tool, mode) pairs
    at once, see BATCH_TOOL_ROLES's callers below) and by /batch/plan
    (which needs the same answer *before* any data has been transferred --
    see batch_io.plan_series_for_roles). Raises ValueError with a
    user-facing message for an unknown tool or an invalid mode for that
    tool.
    """
    if tool == 'fastsurfer':
        if mode not in ('seg', 'full'):
            raise ValueError("mode must be 'seg' or 'full'.")
        return BATCH_TOOL_ROLES['fastsurfer'], TOOL_MODALITY['fastsurfer'], f"FastSurfer ({mode})"

    if tool == 'synthseg':
        if mode not in SYNTHSEG_MODES:
            raise ValueError(
                f"Unknown SynthSeg mode '{mode}' "
                f"(expected one of {', '.join(sorted(SYNTHSEG_MODES))})."
            )
        return (
            BATCH_TOOL_ROLES['synthseg'], TOOL_MODALITY['synthseg'],
            f"SynthSeg ({mode})",
        )

    if tool == 'totalsegmentator':
        if mode not in TASK_REQUIRED_SERIES:
            raise ValueError(f"Unknown TotalSegmentator task '{mode}'.")
        return TASK_REQUIRED_SERIES[mode], TASK_MODALITY[mode], f"TotalSegmentator ({mode})"

    if tool == 'brats':
        return BATCH_TOOL_ROLES['brats'], TOOL_MODALITY['brats'], f"BraTS ({mode})"

    if tool == 'ldct':
        if mode not in LDCT_DOSE_MODES:
            raise ValueError(
                f"Unknown LDCT dose level '{mode}' "
                f"(expected one of {', '.join(sorted(LDCT_DOSE_MODES))})."
            )
        return (
            BATCH_TOOL_ROLES['ldct'], TOOL_MODALITY['ldct'],
            f"LDCT ({round(LDCT_DOSE_MODES[mode] * 100)}% dose)",
        )

    if tool == 'fsl_register':
        if mode not in FSL_REGISTRATION_MODES:
            raise ValueError(
                f"Unknown FSL registration mode '{mode}' "
                f"(expected one of {', '.join(sorted(FSL_REGISTRATION_MODES))})."
            )
        return (
            BATCH_TOOL_ROLES['fsl_register'], TOOL_MODALITY['fsl_register'],
            f"FSL registration ({FSL_REGISTRATION_MODES[mode]['label']})",
        )

    if tool in SINGLE_MODE_TOOLS:
        only_mode, label = SINGLE_MODE_TOOLS[tool]
        if mode != only_mode:
            raise ValueError(f"'{tool}' has one mode, '{only_mode}' (got '{mode}').")
        return BATCH_TOOL_ROLES[tool], TOOL_MODALITY[tool], label

    raise ValueError(f"Unknown batch tool '{tool}'.")

main = Blueprint('main', __name__)

# Maps a recognized upload extension to the converter that turns it into a
# DICOM series (see nifti_upload.py / nrrd_upload.py). Add a new (suffix,
# converter) pair here to support another format through the same route --
# every converter shares the (path, metadata, study_instance_uid, series_number)
# -> (study_instance_uid, series_instance_uid) signature.
VOLUME_CONVERTERS = {
    '.nii.gz': convert_and_store_nifti,
    '.nii': convert_and_store_nifti,
    '.nrrd': convert_and_store_nrrd,
}


def _volume_converter_for_filename(filename):
    lower = filename.lower()
    for suffix, converter in VOLUME_CONVERTERS.items():
        if lower.endswith(suffix):
            return converter
    return None


@main.route('/job_status/<job_id>', methods=['GET'])
def job_status_route(job_id):
    """Report the state of any background job."""
    return job_status_response(job_id)


@main.route('/')
def index():
    """Landing page confirming the API is running."""
    return render_template('index.html')


@main.route('/download_series/<studyInstanceUID>/<seriesInstanceUID>', methods=['GET'])
def download_series_route(studyInstanceUID, seriesInstanceUID):
    """Download a series as a ZIP of its original DICOM files."""
    try:
        log(f"Downloading series {seriesInstanceUID} of study {studyInstanceUID}")
        instances = retrieve_series_sorted(studyInstanceUID, seriesInstanceUID)
        if not instances:
            return "No DICOM instances found", 404
        log(f"Found {len(instances)} instances in series {seriesInstanceUID}")

        zip_buffer = series_to_zip(instances, studyInstanceUID, seriesInstanceUID)
        log(f"Created ZIP with {len(instances)} DICOM files")

        return cors_file(zip_buffer, f"{studyInstanceUID}_{seriesInstanceUID}.zip", "application/zip")

    except Exception as e:
        logger.exception("Error downloading series %s of study %s", seriesInstanceUID, studyInstanceUID)
        return f"Error: {str(e)}", 500

@main.route('/download_series_nifti/<studyInstanceUID>/<seriesInstanceUID>', methods=['GET'])
def download_series_nifti_route(studyInstanceUID, seriesInstanceUID):
    """Download a series converted to a single NIfTI (.nii.gz) file."""
    try:
        log(f"Downloading series {seriesInstanceUID} as NIfTI")
        instances = retrieve_series_sorted(studyInstanceUID, seriesInstanceUID)
        if not instances:
            return "No DICOM instances found", 404

        nii_path = series_to_nifti(instances, studyInstanceUID, seriesInstanceUID)
        log("Sending NIfTI file for download")

        return cors_file(nii_path, f"{studyInstanceUID}_{seriesInstanceUID}.nii.gz", "application/gzip")

    except Exception as e:
        logger.exception("Error downloading series %s of study %s as NIfTI", seriesInstanceUID, studyInstanceUID)
        return f"Error: {str(e)}", 500

@main.route('/tumorseg/<segmenter_name>/<studyInstanceUID>', methods=['GET'])
def tumorseg_route(studyInstanceUID, segmenter_name):
    """Start a tumor segmentation in the background and return a job id."""
    try:
        # Synchronous and single-threaded, before the background job (which
        # runs concurrently with any other in-flight job/request) even
        # starts -- see ensure_segmenter_image_pulled's docstring for why
        # this can't happen inside the background thread itself.
        ensure_segmenter_image_pulled(segmenter_name)
    except Exception as e:
        return cors_json({"error": f"Could not prepare BraTS segmenter '{segmenter_name}': {e}"}, status=502)

    work = logged_work(
        f"{segmenter_name} segmentation for study {studyInstanceUID}",
        lambda: brats_wrapper(studyInstanceUID, segmenter_name=segmenter_name)
        or f"Segmentation ({segmenter_name}) completed and uploaded for study {studyInstanceUID}.",
    )
    return start_bg_job(
        kind=f"tumorseg:{segmenter_name}",
        work=work,
        message=f"Segmentation ({segmenter_name}) started for study {studyInstanceUID}.",
    )

@main.route('/totalsegmentator/<task_name>/<studyInstanceUID>', methods=['GET'])
def totalsegmentator_route(studyInstanceUID, task_name):
    """Start a TotalSegmentator segmentation in the background and return a job id."""
    work = logged_work(
        f"{task_name} segmentation for study {studyInstanceUID}",
        lambda: totalsegmentator_wrapper(studyInstanceUID, task_name)
        or f"Segmentation ({task_name}) completed and uploaded for study {studyInstanceUID}.",
    )
    return start_bg_job(
        kind=f"totalseg:{task_name}",
        work=work,
        message=f"Segmentation ({task_name}) started for study {studyInstanceUID}.",
    )

@main.route('/coreg/<studyInstanceUID>', methods=['POST'])
def coreg_route(studyInstanceUID):
    """Start a co-registration in the background and return a job id.

    `studyInstanceUID` is the *center* series' study -- co-registered results
    are always uploaded there. Expects a JSON body of
    { centerSeriesUID, movingSeries: [{studyInstanceUID, seriesInstanceUID}, ...] },
    sent by the frontend's series-selection dialog. Each moving series carries
    its own studyInstanceUID since it may come from a different study than
    the center series (e.g. co-registering a prior PET study onto today's CT).
    """
    body = request.get_json(silent=True) or {}
    center_series_uid = body.get('centerSeriesUID')
    moving_series = body.get('movingSeries') or []

    if not center_series_uid or not moving_series:
        return cors_json(
            {"error": "centerSeriesUID and at least one movingSeries entry are required."},
            status=400,
        )

    work = logged_work(
        f"co-registration for study {studyInstanceUID}",
        lambda: coreg_wrapper(studyInstanceUID, center_series_uid, moving_series),
    )
    return start_bg_job(
        kind="coreg",
        work=work,
        message=f"Co-registration started for study {studyInstanceUID}.",
    )

@main.route('/ldct/<studyInstanceUID>', methods=['POST'])
def ldct_route(studyInstanceUID):
    """Start a low-dose CT simulation for one of the study's CT series.

    Expects a JSON body of { seriesInstanceUID, f, beta, ... } (see
    SIMULATION_PARAMS in ldct_functions.py), sent by the frontend's LDCT
    parameter dialog. The pipeline itself runs in the separate ldct_api
    service, which uploads the simulated series back into this same study;
    ldct_wrapper blocks on it so this job's status tracks the real run.
    """
    body = request.get_json(silent=True) or {}
    series_instance_uid = body.get('seriesInstanceUID')

    if not series_instance_uid:
        return cors_json({"error": "seriesInstanceUID is required."}, status=400)

    # f and beta are the only parameters the LDCT service insists on and does
    # not default; reject bad values here so the user gets an immediate 400
    # rather than a job that fails minutes later.
    try:
        dose_factor = float(body.get('f', 0.25))
        beta = float(body.get('beta', 0.5))
    except (TypeError, ValueError):
        return cors_json({"error": "f (dose factor) and beta must be numbers."}, status=400)

    if not 0 < dose_factor <= 1:
        return cors_json(
            {"error": "f (dose factor) must be greater than 0 and at most 1."}, status=400
        )

    params = {**body, 'f': dose_factor, 'beta': beta}

    work = logged_work(
        f"LDCT simulation for series {series_instance_uid} of study {studyInstanceUID}",
        lambda: ldct_wrapper(studyInstanceUID, series_instance_uid, params),
    )
    return start_bg_job(
        kind="ldct",
        work=work,
        message=f"LDCT simulation started for study {studyInstanceUID}.",
    )

@main.route('/fastsurfer/<mode>/<studyInstanceUID>', methods=['GET'])
def fastsurfer_route(studyInstanceUID, mode):
    """Start FastSurfer segmentation and/or surface reconstruction in the background and return a job id."""
    work = logged_work(
        f"{mode} FastSurfer Pipeline for study {studyInstanceUID}",
        lambda: fastsurfer_wrapper(studyInstanceUID, mode)
        or f"FastSurfer mode ({mode}) completed and uploaded for study {studyInstanceUID}.",
    )
    return start_bg_job(
        kind=f"fastsurfer:{mode}",
        work=work,
        message=f"FastSurfer processing ({mode}) started for study {studyInstanceUID}.",
    )

@main.route('/synthseg/<mode>/<studyInstanceUID>', methods=['GET'])
def synthseg_route(studyInstanceUID, mode):
    """Start a SynthSeg whole-brain segmentation in the background and return
    a job id.

    Its own route rather than another FastSurfer mode: this is a different
    network answering a different question -- see synthseg_functions.py's
    module docstring. In short, it is the one to point at a thick-slice or
    otherwise low-resolution clinical T1, where FastSurfer's 1 mm-conformed
    pipeline is on the weakest ground; it produces volumetry (and its own QC
    scores) in a couple of minutes, but no cortical thickness."""
    work = logged_work(
        f"SynthSeg ({mode}) for study {studyInstanceUID}",
        lambda: synthseg_wrapper(studyInstanceUID, mode)
        or f"SynthSeg ({mode}) completed and uploaded for study {studyInstanceUID}.",
    )
    return start_bg_job(
        kind=f"synthseg:{mode}",
        work=work,
        message=f"SynthSeg processing ({mode}) started for study {studyInstanceUID}.",
    )

@main.route('/fastsurfer_long/<studyInstanceUID>', methods=['GET'])
def fastsurfer_long_route(studyInstanceUID):
    """Run FastSurfer's longitudinal stream across this patient's studies in
    the background and return a job id.

    A route of its own rather than another mode of /fastsurfer/<mode>/: this
    one is not a segmentation of the clicked study but a joint analysis of
    several of the patient's, it picks its own timepoints (see
    plan_longitudinal_fastsurfer), and it produces trajectories rather than a
    single-study report. It is also hours rather than an hour, which is why
    the OHIF button carries its own raised poll timeout."""
    work = logged_work(
        f"longitudinal FastSurfer pipeline for study {studyInstanceUID}",
        lambda: fastsurfer_long_wrapper(studyInstanceUID)
        or f"Longitudinal FastSurfer completed and uploaded for study {studyInstanceUID}.",
    )
    return start_bg_job(
        kind="fastsurfer:long",
        work=work,
        message=f"Longitudinal FastSurfer processing started for study {studyInstanceUID}.",
    )

def _write_case_note(case_dir, message):
    """Drop a plain-text note into a batch case/tool's output folder when it
    was skipped or failed, so the result ZIP still explains every folder
    instead of silently omitting a tool's output files."""
    os.makedirs(case_dir, exist_ok=True)
    with open(os.path.join(case_dir, 'error.txt'), 'w') as f:
        f.write(message + '\n')


class _MissingRequiredSeriesError(ValueError):
    """Raised by a per-tool runner (below) when this case had no series for
    one of that tool's required roles. A distinct type from a genuine tool
    failure so _run_batch_case can record it as 'skipped' rather than
    'failed' -- the study just doesn't have what this tool needs, which is
    a different (and much more common, over a real cohort) outcome than the
    tool erroring out on input it did get."""


def _tool_scratch_dir(job, case_id, entry_id):
    """Where materialize_tool_input should write `entry_id`'s converted
    NIfTI/work dir for `case_id` -- <job work dir>/scratch/<tool_id>/<case_id>,
    the same convention batch_io.prepare_batch_for_tool used to build inline
    before conversion moved into the per-case worker pool (see
    _require_tool_input)."""
    tool_id = job['params']['tools'][entry_id]['tool']
    return os.path.join(job['work_dir'], 'scratch', tool_id, case_id)


def _nifti_cache_dir(job):
    """Job-wide dcm2niix output cache (batch_io.cached_dicom_series_to_nifti),
    shared by every tool entry and every case of `job` -- so a series picked
    by several tools (e.g. a T1 MPRAGE run through both SynthSeg and
    FastSurfer), or pulled in as another study's longitudinal timepoint, is
    converted once per job rather than once per tool. Lives under work_dir,
    so it goes away with the rest of the job."""
    return os.path.join(job['work_dir'], 'scratch', '_nifti_cache')


def _require_tool_input(job, case_id, entry_id):
    """Fetch this case's prepared input for `entry_id` (one selected (tool,
    mode) pair -- role-matched by batch_io.prepare_batch_for_tools at job
    creation, see _start_batch_job) and raise _MissingRequiredSeriesError if
    the study had no series for one of this tool's required roles.

    The role match alone doesn't mean the input is ready to run on: the
    actual dcm2niix conversion is deferred until here, materialize_tool_input
    doing it lazily inside this worker slot rather than upfront for the whole
    cohort (see that function's docstring) -- so a role that matched by name
    but whose series fails to convert (a corrupt series, a non-image folder)
    is caught here too, same "skip this entry, don't fail the case" handling
    as a role with no match at all."""
    tool_input = job['cases'][case_id]['tool_inputs'][entry_id]
    if tool_input['missing_roles']:
        raise _MissingRequiredSeriesError(
            f"No series matched required role(s) {tool_input['missing_roles']}. "
            f"Series found: {tool_input['series_found']}."
        )
    tool_input = materialize_tool_input(
        tool_input, _tool_scratch_dir(job, case_id, entry_id), nifti_cache_dir=_nifti_cache_dir(job)
    )
    if tool_input['missing_roles']:
        raise _MissingRequiredSeriesError(
            f"Series matched for required role(s) {tool_input['missing_roles']} "
            f"failed to convert to NIfTI. Series found: {tool_input['series_found']}."
        )
    return tool_input


def _run_over_matches(match_sets, out_dir, run_one):
    """Run `run_one(match, series_out_dir)` once per entry of a single-role
    tool's `tool_input['match_sets']` -- populated when this entry's
    series_selection is 'all_matching' (the batch form's "use every matching
    series" checkbox opting out of the default best-series-picker; see
    batch_io.prepare_batch_for_tool) -- each into its own
    out_dir/<role>__<series_name>/ subfolder. Generalizes the always-on
    multi-series loop _run_ldct_tool below already uses for LDCT: one
    candidate failing does not lose the others, and only if every candidate
    fails does the entry fail. Returns one aggregated stats dict, with each
    candidate's own *_batch_stats metrics namespaced by its match label so
    same-named metrics from different candidates don't collide."""
    results, failures = [], []
    for match in match_sets:
        series_out_dir = os.path.join(out_dir, match['label'])
        try:
            results.append((match['label'], run_one(match, series_out_dir)))
        except Exception as e:  # noqa: BLE001 - one matching series must not lose the rest
            message = str(e) or e.__class__.__name__
            _write_case_note(series_out_dir, f"Failed: {message}")
            failures.append(f"{match['label']}: {message}")

    if not results:
        raise RuntimeError(f"No matching series could be processed. {' | '.join(failures)}")
    if failures:
        _write_case_note(
            out_dir,
            f"{len(failures)} of {len(match_sets)} matching series failed:\n" + '\n'.join(failures),
        )

    stats = {'matching_series_used': len(results), 'matching_series_failed': len(failures)}
    for label, match_stats in results:
        for metric, value in match_stats.items():
            stats[f'{label}__{metric}'] = value
    return stats


def _run_fastsurfer_tool(job, case_id, entry_id, out_dir, gpu_index):
    tool_input = _require_tool_input(job, case_id, entry_id)
    mode = job['params']['tools'][entry_id]['mode']
    log(f"Batch FastSurfer ({mode}): {case_id}")
    # Scratch dir nested by mode, not entry_id -- 'fs_output' already scopes
    # this to FastSurfer entries only, so `mode` alone (seg/full) is enough
    # to keep two concurrently-selected modes apart, and it's one fewer
    # thing this path depends on _validate_tool_entries's entry_id format
    # for (see that function's docstring: entry_id is filesystem-safe by
    # construction, but a path bind-mounted into a docker container -- as
    # this one is -- is exactly the kind of place that assumption has to
    # keep holding).
    fs_scratch_dir = os.path.join(job['work_dir'], 'fs_output', case_id, mode)

    if tool_input['match_sets']:
        def run_one(match, series_out_dir):
            result = fastsurfer_core(
                match['work_dir'], os.path.join(fs_scratch_dir, match['label']),
                mode=mode, gpu_index=gpu_index, include_renders=False
            )
            save_fastsurfer_core_outputs(result, match['work_dir'], mode, series_out_dir)
            return fastsurfer_batch_stats(result, mode)
        return _run_over_matches(tool_input['match_sets'], out_dir, run_one)

    # include_renders=False: save_fastsurfer_core_outputs() below never
    # writes the cortical-thickness surface renders anywhere, so generating
    # them here (several slow matplotlib/nilearn renders per case -- see
    # fastsurfer_core's docstring) would just be discarded work.
    result = fastsurfer_core(
        tool_input['work_dir'], fs_scratch_dir, mode=mode, gpu_index=gpu_index, include_renders=False
    )
    save_fastsurfer_core_outputs(result, tool_input['work_dir'], mode, out_dir)
    return fastsurfer_batch_stats(result, mode)


def _run_synthseg_tool(job, case_id, entry_id, out_dir, gpu_index):
    """Segment this case's T1 with SynthSeg, the batch equivalent of the
    per-study SynthSeg button.

    Runs on whichever of the case's T1 and T2 were staged, segmenting each
    independently and producing a separate segmentation and set of tables per
    contrast (see SYNTHSEG_CONTRASTS -- SynthSeg is contrast-agnostic, but has
    no joint T1+T2 inference). A case with both gets two of everything; a case
    with either one alone gets a complete result on that one.

    Deliberately skips the _require_tool_input fast-path every other runner
    starts with, for the reason spelled out at BATCH_TOOL_ROLES['synthseg']:
    that check demands *every* declared role, whereas these two are
    alternatives. Only a case with neither contrast is skipped. Still calls
    materialize_tool_input itself (the dcm2niix conversion _require_tool_input
    would otherwise have done), since role_paths/match_sets are only
    populated after that runs -- see that function's docstring.

    Unlike FastSurfer, this one is cheap enough per case (~1-2 min per
    contrast) that a cohort run is not an overnight job, which is rather the
    point of offering it for batch: it makes whole-cohort volumetry practical
    on exactly the low-resolution clinical series FastSurfer struggles with."""
    tool_input = job['cases'][case_id]['tool_inputs'][entry_id]
    tool_input = materialize_tool_input(
        tool_input, _tool_scratch_dir(job, case_id, entry_id), nifti_cache_dir=_nifti_cache_dir(job)
    )
    mode = job['params']['tools'][entry_id]['mode']
    # See _run_fastsurfer_tool's comment: keyed by mode, not entry_id.
    synthseg_scratch_dir = os.path.join(job['work_dir'], 'synthseg_output', case_id, mode)

    if tool_input['match_sets']:
        # 'all_matching' mode: each match is a single (role, candidate) pair
        # rather than the whole-role_paths dict below, so run SynthSeg once
        # per matching series -- one contrast per call, never combined
        # across matches -- instead of once per case with every present
        # contrast together.
        def run_one(match, series_out_dir):
            contrast = CONTRAST_FOR_ROLE[match['role']]
            contrast_paths = {contrast: match['role_paths'][match['role']]}
            result = synthseg_core(
                contrast_paths, os.path.join(synthseg_scratch_dir, match['label']),
                mode=mode, gpu_index=gpu_index,
            )
            save_synthseg_core_outputs(result, series_out_dir)
            return synthseg_batch_stats(result)
        return _run_over_matches(tool_input['match_sets'], out_dir, run_one)

    role_paths = tool_input['role_paths']
    contrast_paths = {
        CONTRAST_FOR_ROLE[role]: role_paths[role]
        for role in SYNTHSEG_ROLES if role in role_paths
    }
    if not contrast_paths:
        raise _MissingRequiredSeriesError(
            f"No series matched any of SynthSeg's roles {sorted(SYNTHSEG_ROLES)}. "
            f"Series found: {tool_input['series_found']}."
        )

    log(f"Batch SynthSeg ({mode}): {case_id} -- {'+'.join(sorted(contrast_paths))}")
    result = synthseg_core(
        contrast_paths, synthseg_scratch_dir, mode=mode, gpu_index=gpu_index,
    )
    save_synthseg_core_outputs(result, out_dir)
    return synthseg_batch_stats(result)


def _run_totalsegmentator_tool(job, case_id, entry_id, out_dir, gpu_index):
    tool_input = _require_tool_input(job, case_id, entry_id)
    task_name = job['params']['tools'][entry_id]['mode']
    log(f"Batch TotalSegmentator ({task_name}): {case_id}")
    # See _run_fastsurfer_tool's comment: keyed by task_name, not entry_id.
    ts_scratch_dir = os.path.join(job['work_dir'], 'ts_output', case_id, task_name)

    if tool_input['match_sets']:
        def run_one(match, series_out_dir):
            result = totalsegmentator_core(
                match['work_dir'], os.path.join(ts_scratch_dir, match['label']), task_name, gpu_index=gpu_index
            )
            save_totalsegmentator_core_outputs(result, series_out_dir)
            return totalsegmentator_batch_stats(result)
        return _run_over_matches(tool_input['match_sets'], out_dir, run_one)

    result = totalsegmentator_core(tool_input['work_dir'], ts_scratch_dir, task_name, gpu_index=gpu_index)
    save_totalsegmentator_core_outputs(result, out_dir)
    return totalsegmentator_batch_stats(result)


def _run_brats_tool(job, case_id, entry_id, out_dir, gpu_index):
    tool_input = _require_tool_input(job, case_id, entry_id)
    segmenter_name = job['params']['tools'][entry_id]['mode']
    log(f"Batch BraTS ({segmenter_name}): {case_id}")
    # Deliberately NOT nested by segmenter_name, unlike FastSurfer/
    # TotalSegmentator's per-mode scratch dirs (_run_fastsurfer_tool/
    # _run_totalsegmentator_tool) -- atlas registration/skull-stripping/
    # normalization (brats_core's _atlas_preprocess step) is identical
    # regardless of which segmenter mode runs on the result, so every BraTS
    # mode selected for this case shares one scratch dir and reuses the same
    # preprocessed files instead of repeating that (slow) step per mode.
    # _atlas_preprocess() already skips its work when the output is already
    # there (matches the interactive per-study route's brats_wrapper, whose
    # output_dir was never nested by segmenter_name either); the entries in
    # job['params']['tools'] for one case's several BraTS modes run
    # sequentially within this same worker slot (_run_batch_case's loop), so
    # there's no cross-mode race on this shared dir to guard against.
    brats_scratch_dir = os.path.join(job['work_dir'], 'brats_output', case_id)
    result = brats_core(tool_input['work_dir'], brats_scratch_dir, segmenter_name=segmenter_name, gpu_index=gpu_index)
    save_brats_core_outputs(result, out_dir)
    return brats_batch_stats(result)


def _run_ldct_tool(job, case_id, entry_id, out_dir, gpu_index):
    """Simulate a low-dose acquisition of *every* CT series in the case.

    Unlike the segmentation tools, this one is not a single run per case:
    LDCT is a per-series operation with nothing to combine across series, so
    a study holding four CT reconstructions yields four simulated series
    (batch_io's TOOL_SERIES_SELECTION marks ldct 'every'). Each lands in its
    own out_dir/<series_name>/ so two series' DICOM output can't collide, and
    they run sequentially within this one worker slot -- and so on this one
    GPU -- rather than fanning out, since the pool's whole point is to cap
    how many of these are in flight at once.

    One series failing does not lose the others: the run continues and the
    failure is noted per series, matching how _run_batch_case treats one tool
    failing out of several. Only if *every* series fails does the entry fail.
    """
    mode = job['params']['tools'][entry_id]['mode']
    dose_factor = LDCT_DOSE_MODES[mode]
    tool_input = _require_tool_input(job, case_id, entry_id)

    # The only tool that consumes DICOM rather than <role>.nii.gz, so it
    # reads the series' own folders (batch_io's `source_series`, populated
    # because TOOL_INPUT_KIND marks ldct a 'dicom' tool) instead of
    # tool_input['work_dir'] -- which for this tool is never materialized.
    # A cohort staged as NIfTI can't feed this pipeline at all: it reads the
    # geometry off the DICOM headers (step0) and rebuilds its output series
    # by copying those same headers (step8_3). Report that as a skip, like a
    # missing role, rather than failing the case.
    if not tool_input['source_series']:
        raise _MissingRequiredSeriesError(
            f"LDCT found no CT series to simulate in this study "
            f"(series found: {tool_input['series_found']})."
        )
    ct_series = [s for s in tool_input['source_series'] if s['kind'] == 'dicom']
    if not ct_series:
        raise _MissingRequiredSeriesError(
            f"LDCT needs original DICOM CT series; the CT series in this study "
            f"({[s['name'] for s in tool_input['source_series']]}) are already converted "
            "to NIfTI, which carries no DICOM headers to simulate from."
        )

    log(f"Batch LDCT ({mode}): {case_id} -- {len(ct_series)} CT series")

    results, failures = [], []
    for source in ct_series:
        # `case_id` is "<patient_id>/<study_name>" -- flattened, and suffixed
        # with the series, because it ends up inside the simulated series'
        # SeriesDescription and has to tell two series of one study apart.
        case_label = f"{case_id.replace('/', '_')}_{source['name']}"
        series_out_dir = os.path.join(out_dir, source['name'])
        try:
            results.append(ldct_core(
                source['path'], series_out_dir, dose_factor,
                gpu_index=gpu_index, case_label=case_label,
            ))
        except Exception as e:  # noqa: BLE001 - one series must not lose the rest
            message = str(e) or e.__class__.__name__
            log(f"Batch LDCT ({mode}): {case_id}/{source['name']} failed: {message}")
            _write_case_note(series_out_dir, f"LDCT ({mode}) failed: {message}")
            failures.append(f"{source['name']}: {message}")

    if not results:
        raise RuntimeError(f"No CT series could be simulated. {' | '.join(failures)}")
    if failures:
        _write_case_note(
            out_dir, f"{len(failures)} of {len(ct_series)} CT series failed:\n" +
            '\n'.join(failures)
        )

    return ldct_batch_stats(results, failed=len(failures))


def _run_fsl_register_tool(job, case_id, entry_id, out_dir, gpu_index):
    """Register this case's T1 to MNI152, the batch equivalent of the
    per-study "FSL Register+QC" button. gpu_index is unused: FLIRT/FNIRT are
    CPU-only, and the run itself happens in the interact-gui container (see
    fsl_register_core) rather than here at all."""
    tool_input = _require_tool_input(job, case_id, entry_id)
    mode = job['params']['tools'][entry_id]['mode']
    log(f"Batch FSL registration ({mode}): {case_id}")
    # See _run_fastsurfer_tool's comment: keyed by mode, not entry_id.
    reg_scratch_dir = os.path.join(job['work_dir'], 'fsl_reg_output', case_id, mode)

    if tool_input['match_sets']:
        def run_one(match, series_out_dir):
            result = fsl_register_core(
                match['role_paths'][match['role']], os.path.join(reg_scratch_dir, match['label']), mode=mode
            )
            save_fsl_register_core_outputs(result, series_out_dir)
            return fsl_register_batch_stats(result)
        return _run_over_matches(tool_input['match_sets'], out_dir, run_one)

    result = fsl_register_core(tool_input['role_paths']['t1n'], reg_scratch_dir, mode=mode)
    save_fsl_register_core_outputs(result, out_dir)
    return fsl_register_batch_stats(result)


def _run_hippocampus_tool(job, case_id, entry_id, out_dir, gpu_index):
    """Segment L/R hippocampus with FSL FIRST, the batch equivalent of the
    per-study hippocampus button. gpu_index is unused -- FIRST is CPU-only
    (it is also the slowest tool here per case for that reason, roughly
    30-60 min, with no GPU step to offload)."""
    tool_input = _require_tool_input(job, case_id, entry_id)
    log(f"Batch hippocampus (FSL FIRST): {case_id}")
    hipp_scratch_dir = os.path.join(job['work_dir'], 'hippocampus_output', case_id)

    if tool_input['match_sets']:
        def run_one(match, series_out_dir):
            result = hippocampus_core(
                match['role_paths'][match['role']], os.path.join(hipp_scratch_dir, match['label'])
            )
            save_hippocampus_core_outputs(result, series_out_dir)
            return hippocampus_batch_stats(result)
        return _run_over_matches(tool_input['match_sets'], out_dir, run_one)

    result = hippocampus_core(tool_input['role_paths']['t1n'], hipp_scratch_dir)
    save_hippocampus_core_outputs(result, out_dir)
    return hippocampus_batch_stats(result)


def _run_screw_tool(job, case_id, entry_id, out_dir, gpu_index):
    """Run live screw-segmentation inference on this case's CT, the batch
    equivalent of the per-study "Screw Segmentation (live inference)"
    button. Unlike that route -- which always uses the GPU reserved for
    interactive work -- this pins the inference container to this worker
    slot's own GPU."""
    tool_input = _require_tool_input(job, case_id, entry_id)
    log(f"Batch screw inference: {case_id}")
    screw_scratch_dir = os.path.join(job['work_dir'], 'screw_output', case_id)

    if tool_input['match_sets']:
        def run_one(match, series_out_dir):
            result = screw_inference_core(
                match['role_paths'][match['role']], os.path.join(screw_scratch_dir, match['label']),
                gpu_index=gpu_index,
            )
            save_screw_inference_core_outputs(result, series_out_dir)
            return screw_inference_batch_stats(result)
        return _run_over_matches(tool_input['match_sets'], out_dir, run_one)

    result = screw_inference_core(
        tool_input['role_paths']['ct_screw'], screw_scratch_dir, gpu_index=gpu_index
    )
    save_screw_inference_core_outputs(result, out_dir)
    return screw_inference_batch_stats(result)


def _run_fastsurfer_long_tool(job, case_id, entry_id, out_dir, gpu_index):
    """Batch form of the "FastSurfer Longitudinal" button
    (fastsurfer_long_functions.fastsurfer_long_wrapper). Unlike every other
    batch tool, this looks past case_id's own study at the *other study
    folders the same patient has in this batch cohort* for its timepoints
    (plan_longitudinal_fastsurfer_batch), the same way the interactive
    button looks past the clicked study into the patient's PACS history --
    see that function's docstring.

    A patient's several studies are still submitted as separate cases
    (matching the batch tree's one-case-per-study shape), so a patient with
    several eligible studies in the cohort gets one longitudinal pass per
    study, each with itself as the "current" timepoint -- exactly what
    clicking the interactive button on each of those studies in turn would
    produce. A patient with only one dated study in the cohort has nothing
    to compare it to and this entry is skipped (_MissingRequiredSeriesError),
    same as a case missing a required series for any other tool.

    Deliberately skips the _require_tool_input fast-path every other runner
    starts with: that check requires *every* role in BATCH_TOOL_ROLES
    present (right for e.g. BraTS's t1n+t1c+t2+flair, all needed at once),
    whereas LONGITUDINAL_ROLES are *alternatives* -- a study normally
    matches only one of 't1n_acquired'/'t1n', never both, since
    map_series_to_roles assigns each series to at most one role. Requiring
    both would skip almost every real study. plan_longitudinal_fastsurfer_batch
    below already does the correct "any one of them" check itself."""
    patient_id, study_name = case_id.split('/', 1)
    root_dir = os.path.join(job['work_dir'], 'input')

    try:
        plan = plan_longitudinal_fastsurfer_batch(root_dir, patient_id, study_name)
    except ValueError as e:
        raise _MissingRequiredSeriesError(str(e))

    timepoints = plan['timepoints']
    log(f"Batch longitudinal FastSurfer: {case_id} ({len(timepoints)} timepoints)")

    long_scratch_dir = os.path.join(job['work_dir'], 'fastsurfer_long_output', case_id)
    input_dir = os.path.join(long_scratch_dir, 'input')
    materialize_timepoint_inputs(timepoints, _nifti_cache_dir(job), input_dir)

    result = fastsurfer_long_core(
        input_dir, os.path.join(long_scratch_dir, 'output'), timepoints, gpu_index=gpu_index
    )
    save_fastsurfer_long_core_outputs(result, plan, input_dir, out_dir)
    return fastsurfer_long_batch_stats(result)


def _run_siena_tool(job, case_id, entry_id, out_dir, gpu_index):
    """Batch form of the SIENA/SIENAX button (siena_functions.siena_wrapper).
    Like _run_fastsurfer_long_tool, looks past case_id's own study at the
    patient's other study folders already in this batch cohort for a
    longitudinal partner (plan_siena_analysis_batch, the local counterpart
    of siena_wrapper's plan_siena_analysis), and falls back to
    cross-sectional SIENAX when the patient has no other dated study with a
    matching series in this cohort -- exactly like the interactive button.
    gpu_index is unused -- FSL siena/sienax are CPU-only, like
    hippocampus/fsl_register.

    Deliberately skips the _require_tool_input fast-path (see
    _run_fastsurfer_long_tool's docstring) for the same reason: SIENA_ROLES
    are alternatives, not a simultaneous requirement, and
    plan_siena_analysis_batch below already checks that correctly."""
    patient_id, study_name = case_id.split('/', 1)
    root_dir = os.path.join(job['work_dir'], 'input')

    try:
        plan = plan_siena_analysis_batch(root_dir, patient_id, study_name)
    except ValueError as e:
        raise _MissingRequiredSeriesError(str(e))

    log(f"Batch {plan['mode'].upper()}: {case_id}")
    siena_scratch_dir = os.path.join(job['work_dir'], 'siena_output', case_id)
    stage_dir = _nifti_cache_dir(job)

    if plan['mode'] == 'siena':
        baseline, followup = plan['timepoints']
        paths = materialize_timepoint_inputs([baseline, followup], stage_dir, siena_scratch_dir)
        if 'baseline' not in paths or 'followup' not in paths:
            raise _MissingRequiredSeriesError(
                "SIENA baseline/follow-up series could not be converted to NIfTI."
            )
        result = siena_core(
            paths['baseline'], paths['followup'], siena_scratch_dir,
            is_t2=plan['is_t2'], bet_options=DEFAULT_BET_OPTIONS,
        )
        save_siena_core_outputs(result, plan, out_dir)
        return siena_batch_stats(result, plan)

    (current,) = plan['timepoints']
    paths = materialize_timepoint_inputs([current], stage_dir, siena_scratch_dir)
    if 'current' not in paths:
        raise _MissingRequiredSeriesError("SIENAX's matched series could not be converted to NIfTI.")
    result = sienax_core(paths['current'], siena_scratch_dir, is_t2=plan['is_t2'], bet_options=DEFAULT_BET_OPTIONS)
    save_sienax_core_outputs(result, paths['current'], out_dir)
    return sienax_batch_stats(result)


# tool type -> fn(job, case_id, entry_id, out_dir, gpu_index), each raising
# on failure (or _MissingRequiredSeriesError when the case just doesn't have
# what this tool needs) and, on success, returning a flat {metric_name:
# value} dict of that entry's quantitative results -- FastSurfer's own
# volumetry/recon stats reused as-is, BraTS/TotalSegmentator's per-segment
# volumes computed via backend_helpers.get_segment_volume() (see each
# module's *_batch_stats() helper) -- for _write_results_overview below to
# fold into results_overview.csv. Driven by _run_batch_case below, one call
# per selected (tool, mode) entry per case -- see that function's docstring
# on why the dispatch key is the tool *type* while entry_id (the case/params
# lookup key) also encodes the mode, so the same tool can run several modes
# (e.g. several BraTS algorithms) in one job.
TOOL_RUNNERS = {
    'fastsurfer': _run_fastsurfer_tool,
    'synthseg': _run_synthseg_tool,
    'totalsegmentator': _run_totalsegmentator_tool,
    'brats': _run_brats_tool,
    'ldct': _run_ldct_tool,
    'fsl_register': _run_fsl_register_tool,
    'hippocampus': _run_hippocampus_tool,
    'screw_infer': _run_screw_tool,
    'fastsurfer_long': _run_fastsurfer_long_tool,
    'siena': _run_siena_tool,
}


def _finish_tool(case, entry_id, out_dir, status, error, outcomes, stats=None):
    output_files = sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []
    # .update(), not a full replace: case['tools'][entry_id] already carries
    # 'tool'/'mode' from job creation (_start_batch_job) and callers (e.g.
    # GET /batch/status) rely on those staying put across every status
    # transition, not just the initial 'pending' one.
    case['tools'][entry_id].update({
        "status": status, "error": error, "output_files": output_files, "stats": stats or {},
    })
    outcomes[entry_id] = status


def _run_batch_case(job, case_id, gpu_index):
    """batch_jobs.py runner for kind='batch' (run several
    selected tools over one study in one job, rather than one tool per job
    -- extended to also allow several *modes* of the same tool, e.g.
    running several BraTS algorithms side by side). Runs every entry in
    job['params']['tools'] against this one case, in turn, under this one
    worker slot/GPU -- one entry failing (or being skipped for missing
    series) does not stop the rest, and does not abort the case or the
    batch.

    job['params']['tools'] is {entry_id: {"tool", "mode"}}, one entry per
    selected (tool, mode) pair -- entry_id (built by _parse_tools_field /
    _validate_tool_entries as f"{tool}__{mode}") is what's unique per
    selection, not the tool type alone, since e.g. 'brats' can appear
    several times with different `mode` (segmenter). TOOL_RUNNERS is
    dispatched on entry['tool'] (the tool type); everything else --
    job['cases'][case_id]['tool_inputs']/['tools'], job['params']['tools']
    itself -- is keyed by entry_id, so two modes of the same tool never
    collide. Each entry's outcome is recorded independently in
    job['cases'][case_id]['tools'][entry_id] and persisted
    (batch_jobs.update_case) as soon as that entry finishes, so a still-
    running case already shows which of its tools are done rather than
    nothing until the whole case completes. Each entry's output lands in
    its own result/<case_id>/<tool>/<mode>/ folder, so two entries' output
    files (e.g. TotalSegmentator's 'segmentation.nii.gz' run under two
    different tasks) can never collide.

    Returns (status, error) for batch_jobs.py to record as the case's own
    aggregate status: 'done' if every entry succeeded, 'failed' if none did
    (including every entry being skipped for missing series), otherwise
    'partial' -- or 'pending' if pause/stop was requested before every entry
    ran (see batch_jobs.halt_requested); entries already 'done'/'skipped'
    from such an earlier, interrupted run are kept, not rerun.
    """
    case = job['cases'][case_id]
    outcomes = {}

    for entry_id, entry in job['params']['tools'].items():
        # An earlier run of this case (interrupted by pause/stop, or a
        # container restart) may already have settled this entry -- keep
        # that rather than rerunning it. 'failed' is retried, same as a
        # failed case is on resume.
        previous = case['tools'][entry_id].get('status')
        if previous in ('done', 'skipped'):
            outcomes[entry_id] = previous
            continue
        # Pause/stop is honoured between entries, not just between cases:
        # with many entries per case, waiting for the whole case could take
        # hours. 'pending' hands the case back to batch_jobs.py, which
        # settles it as paused/stopped; a resume picks up at this entry.
        if batch_jobs.halt_requested(job['id']):
            logger.info("Case %s: pause/stop requested, not starting %s (%s)", case_id, entry['tool'], entry['mode'])
            return 'pending'

        out_dir = os.path.join(job['work_dir'], 'result', case_id, entry['tool'], entry['mode'])
        case['tools'][entry_id]['status'] = 'running'
        batch_jobs.update_case(job['id'], case_id, tools=case['tools'])

        logger.info("--- Case %s: %s (%s) on GPU %s -> %s", case_id, entry['tool'], entry['mode'], gpu_index, out_dir)
        logger.debug("Case %s: tool inputs %s", case_id, (case.get('tool_inputs') or {}).get(entry_id))
        try:
            with timed(f"{entry['tool']} ({entry['mode']}) on case {case_id}", log=logger):
                stats = TOOL_RUNNERS[entry['tool']](job, case_id, entry_id, out_dir, gpu_index)
            _finish_tool(case, entry_id, out_dir, 'done', None, outcomes, stats=stats)
        except _MissingRequiredSeriesError as e:
            logger.warning("Case %s: %s (%s) skipped: %s", case_id, entry['tool'], entry['mode'], e)
            _write_case_note(out_dir, f"Skipped: {e}")
            _finish_tool(case, entry_id, out_dir, 'skipped', str(e), outcomes)
        except Exception as e:
            message = str(e) or e.__class__.__name__
            logger.exception("Case %s: %s (%s) failed: %s", case_id, entry['tool'], entry['mode'], message)
            _write_case_note(out_dir, f"{entry['tool']} ({entry['mode']}) failed: {message}")
            _finish_tool(case, entry_id, out_dir, 'failed', message, outcomes)

        batch_jobs.update_case(job['id'], case_id, tools=case['tools'])

    succeeded = [t for t, o in outcomes.items() if o == 'done']
    unsuccessful = [t for t, o in outcomes.items() if o != 'done']
    if not unsuccessful:
        return 'done', None
    if not succeeded:
        return 'failed', f"No selected tool produced output: {', '.join(unsuccessful)}"
    return 'partial', f"Tool(s) did not complete: {', '.join(unsuccessful)}"


batch_jobs.register_runner('batch', _run_batch_case)


def _write_batch_manifest(job):
    """batch_jobs.py completion hook for kind='batch': write
    result/manifest.csv summarizing every (patient, study, tool, mode,
    status, output files) row run in this job, once every case has reached
    a final status -- so a 100-patient/several-tool(-mode) run is auditable
    at a glance without opening every case folder in the result ZIP. Built
    straight from the job's own in-memory case/tool records (already the
    source of truth GET /batch/status/<job_id> reports from), not a second
    filesystem walk."""
    manifest_path = os.path.join(job['work_dir'], 'result', 'manifest.csv')
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    with open(manifest_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['patient_id', 'study_name', 'tool', 'mode', 'status', 'error', 'output_files'])
        for case_id, case in sorted(job['cases'].items()):
            patient_id, _sep, study_name = case_id.partition('/')
            for entry_id, tool_result in sorted(case.get('tools', {}).items()):
                writer.writerow([
                    patient_id,
                    study_name,
                    tool_result.get('tool'),
                    tool_result.get('mode'),
                    tool_result.get('status'),
                    tool_result.get('error') or '',
                    '; '.join(tool_result.get('output_files') or []),
                ])
    log(f"Batch {job['id']}: wrote manifest.csv")


def _build_results_overview_rows(cases):
    """Shared by _write_results_overview (writes result/results_overview.csv
    to disk once a job settles) and batch_result_partial_route (builds the
    same thing fresh, in-memory, for a still-running job's "download
    finished so far" ZIP, which has no on-disk results_overview.csv to serve
    since the completion hook hasn't run yet): turns `cases` -- an iterable
    of (case_id, case) pairs, case shaped like a batch_jobs.py case record
    (only its 'tools' key is read here) -- into (fieldnames, rows) ready for
    csv.DictWriter, one row per (patient, study) case with every
    quantitative statistic every selected (tool, mode) entry produced for
    that case as its own column.

    Column names are f"{entry_id}__{metric_name}" (entry_id already encodes
    tool+mode, e.g. 'brats__goat', so two modes of the same tool -- or the
    same tool run twice with different modes -- never collide). Per-entry
    stats come from case['tools'][entry_id]['stats'], populated by
    _finish_tool() from whatever TOOL_RUNNERS[...] returned (see that dict's
    comment) -- empty for an entry that was skipped, failed, or simply
    hasn't run yet, so such a case just leaves those columns blank rather
    than losing the row.

    A 'study_date' column (from case['study_date'], see
    batch_io.compute_study_dates) is inserted right after 'study_name' the
    first time any case actually has one -- i.e. only for a batch that
    includes DICOM input, same lazy-column pattern as the per-entry stats
    below. A NIfTI-only case just leaves it blank, same as a missing stat."""
    fieldnames = ['patient_id', 'study_name']
    seen_columns = set(fieldnames)
    rows = []
    for case_id, case in sorted(cases):
        patient_id, _sep, study_name = case_id.partition('/')
        row = {'patient_id': patient_id, 'study_name': study_name}
        study_date = case.get('study_date')
        if study_date:
            row['study_date'] = study_date
            if 'study_date' not in seen_columns:
                seen_columns.add('study_date')
                fieldnames.insert(2, 'study_date')
        for entry_id, tool_result in sorted((case.get('tools') or {}).items()):
            for metric_name, value in (tool_result.get('stats') or {}).items():
                column = f'{entry_id}__{metric_name}'
                row[column] = value
                if column not in seen_columns:
                    seen_columns.add(column)
                    fieldnames.append(column)
        rows.append(row)
    return fieldnames, rows


def _write_results_overview(job):
    """batch_jobs.py completion hook for kind='batch': write
    result/results_overview.csv from _build_results_overview_rows (see its
    docstring) -- so a cohort's results can be pulled straight into
    SPSS/R/Excel instead of copied out of each case's own output folder by
    hand. Built from the job's own in-memory case/tool records, same as
    _write_batch_manifest, not a second filesystem walk."""
    fieldnames, rows = _build_results_overview_rows(job['cases'].items())

    overview_path = os.path.join(job['work_dir'], 'result', 'results_overview.csv')
    os.makedirs(os.path.dirname(overview_path), exist_ok=True)
    with open(overview_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval='')
        writer.writeheader()
        writer.writerows(rows)
    log(f"Batch {job['id']}: wrote results_overview.csv")


def _write_batch_summaries(job):
    """Single completion hook for kind='batch' (batch_jobs.py only keeps one
    hook per kind, see register_completion_hook) -- runs both summary
    writers once every case has reached a final status."""
    _write_batch_manifest(job)
    _write_results_overview(job)


batch_jobs.register_completion_hook('batch', _write_batch_summaries)


def _validate_tool_entries(tools_in):
    """Validate an already-JSON-decoded list of {"tool", "mode"} objects and
    return {entry_id: {"tool": tool_id, "mode": mode}}, one entry per
    selected (tool, mode) *pair* -- entry_id is f"{tool_id}__{mode}", not
    just tool_id, so the same tool can be selected several times with
    different modes in one job (e.g. running several BraTS algorithms side
    by side). Filesystem-safe on purpose (letters/digits/underscore only,
    given tool_id/mode are themselves drawn from a small fixed vocabulary --
    see BATCH_TOOL_ROLES/TASK_REQUIRED_SERIES): entry_id ends up as a
    directory name in more than one place (batch_io.prepare_batch_for_tools'
    per-entry scratch dir, _run_batch_case's result/<case_id>/<tool>/<mode>/
    is built from the tool/mode fields instead, but earlier internal
    scratch paths are not), and a ':' there previously broke docker's
    `-v host:container:ro` mount-spec parsing ("too many colons") the
    moment such a path was bind-mounted into a container.

    Raises ValueError with a user-facing message for a malformed list, an
    empty list, the exact same (tool, mode) pair given twice, or an unknown
    tool/invalid mode (via _batch_tool_spec).

    Shared by _parse_tools_field (POST /batch/run's form-encoded `tools`
    field, a JSON *string*) and batch_plan_route (POST /batch/plan's JSON
    body, where `tools` arrives already decoded)."""
    if not isinstance(tools_in, list) or not tools_in:
        raise ValueError("Choose at least one tool to run.")

    entries = {}
    for entry in tools_in:
        if not isinstance(entry, dict) or 'tool' not in entry:
            raise ValueError('Each entry in \'tools\' must have a "tool" (and "mode").')
        tool_id, mode = entry['tool'], entry.get('mode')
        entry_id = f"{tool_id}__{mode}"
        if entry_id in entries:
            raise ValueError(f"'{tool_id}' ({mode}) was selected more than once.")
        _batch_tool_spec(tool_id, mode)  # validates tool/mode; raises ValueError if bad
        entries[entry_id] = {"tool": tool_id, "mode": mode}
    return entries


def _parse_tools_field(raw):
    """Parse the `tools` form field for POST /batch/run: a JSON *string*
    holding an array of {"tool", "mode"} objects, e.g.
    '[{"tool":"fastsurfer","mode":"seg"},{"tool":"brats","mode":"goat"},
      {"tool":"brats","mode":"meningioma"}]'
    (mode is FastSurfer's mode / TotalSegmentator's task_name / BraTS's
    segmenter_name). See _validate_tool_entries for the returned shape and
    validation rules; raises ValueError with a user-facing message for
    malformed JSON on top of what that function already checks."""
    try:
        tools_in = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        raise ValueError('\'tools\' must be a JSON array of {"tool": ..., "mode": ...} objects.')
    return _validate_tool_entries(tools_in)


def _start_batch_job(entries, label):
    """Shared body of POST /batch/run (run several selected tools -- and
    several modes of the same tool, e.g. several BraTS algorithms -- over
    one study in one job, superseding the earlier one-job-per-tool
    design): save the uploaded cohort, resolve
    series->role mapping per selected (tool, mode) entry via
    batch_io.prepare_batch_for_tools (each entry gets
    its own resolution, since two tools can want the same role at
    different modalities; two modes of the *same* tool always share one
    role list/modality, so this duplicates the (cheap) role-matching work
    for them but not the choice itself), and launch the batch job through
    batch_jobs.py's bounded, GPU-pinned worker pool.

    `entries` is {entry_id: {"tool": tool_id, "mode": mode}} (already
    validated by the caller via _parse_tools_field/_validate_tool_entries),
    one entry per selected (tool, mode) pair -- see _run_batch_case's
    docstring for why entry_id, not tool_id, is the unique key throughout
    this job. `label` is used only in the response message.

    Takes its cohort either way round:

    - `input_path` (form field): a directory the caller has already staged
      under batch_io.BATCH_STAGE_ROOT, on the API_storage mount both this
      container and docker-context-gui share at the same host path. Moved
      (not copied) into the job's work dir, so a cohort never crosses the
      network twice. This is what the GUI uses -- it removed a full extra
      copy of every cohort (built as an in-memory ZIP, POSTed, unpacked
      again) from the critical path. See resolve_staged_input_dir on why
      the path is confined to that root.
    - `file` (multipart): a ZIP of the same tree, unpacked here. Kept for
      any caller that has no access to the shared mount.

    Either way the tree's top level is patient_id directories, each
    containing study_name/series_name subdirectories (series may be DICOM or
    already-NIfTI, per batch_io.walk_batch_tree). It lands in a persistent
    work dir under API_storage/batch_jobs/<job_id>/ (not /tmp), so a
    container restart mid-batch resumes rather than losing or repeating
    already-completed cases. A (case, tool, mode) missing that tool's
    required series, or whose run fails, gets an error.txt in its own
    result/<case_id>/<tool>/<mode>/ folder and does not abort that case's
    other tools or the rest of the batch (see _run_batch_case).

    Poll GET /batch/status/<job_id> for live per-case, per-tool progress and
    GET /batch/result/<job_id> for the output ZIP (including a
    result/manifest.csv summary -- see _write_batch_manifest) once done.

    Two optional form fields carry GUI-only bookkeeping through onto the job
    record (this API has no auth of its own, so these are trusted from
    whoever calls the route -- see start_batch_job's docstring): `owner`
    (stamped by the GUI from the logged-in Keycloak user, so its "Your Batch
    Jobs" list -- GET /batch/jobs?owner=<owner> -- can filter to one user's
    jobs) and `output_mode`/`irods_output_path` (so the GUI can restore
    whether to offer "push to iRODS" for a job resumed after logout/login,
    since that choice would otherwise only have lived in the submitting
    browser tab's memory).

    A third optional field, `use_all_series`, is the batch form's "use every
    matching series" checkbox: when '1', every entry whose tool can support
    it (see the tool_specs loop below) is switched from the default
    'roles' series_selection to 'all_matching' (batch_io.prepare_batch_for_tool),
    keeping every series that passes a role's must-criteria filter instead of
    only the single best-scoring one. BraTS, FastSurfer Longitudinal and
    SIENA/SIENAX are excluded regardless of this flag: BraTS genuinely needs
    one T1+T1c+T2+FLAIR combination at once (there is no single role here to
    fan out without combining across roles), and the other two pick a series
    from *other* studies in the cohort entirely, not from this study's
    candidates (see _run_fastsurfer_long_tool/_run_siena_tool).
    """
    uploaded_file = request.files.get('file')
    staged_input_path = (request.form.get('input_path') or '').strip()
    if staged_input_path:
        try:
            staged_input_path = resolve_staged_input_dir(staged_input_path)
        except ValueError as e:
            return cors_json({"error": f"Invalid input_path: {e}"}, status=400)
    elif uploaded_file is None or not uploaded_file.filename:
        return cors_json(
            {"error": "No cohort provided (expected form field 'input_path', or multipart field 'file')."},
            status=400,
        )

    owner = request.form.get('owner') or None
    full_params = {"tools": entries}
    if request.form.get('output_mode'):
        full_params['output_mode'] = request.form['output_mode']
    if request.form.get('irods_output_path'):
        full_params['irods_output_path'] = request.form['irods_output_path']

    job_id = uuid.uuid4().hex
    work_dir = batch_jobs.job_work_dir(job_id)
    os.makedirs(work_dir, exist_ok=True)

    input_root = os.path.join(work_dir, 'input')
    if staged_input_path:
        try:
            # Same filesystem (both are under API_storage), so this is a
            # rename: instant, and no second copy of the cohort on disk.
            shutil.move(staged_input_path, input_root)
        except OSError as e:
            shutil.rmtree(work_dir, ignore_errors=True)
            return cors_json({"error": f"Could not take over staged input: {e}"}, status=400)
    else:
        upload_path = os.path.join(work_dir, 'upload.zip')
        uploaded_file.save(upload_path)
        try:
            unpack_batch_zip(upload_path, input_root)
        except (zipfile.BadZipFile, ValueError) as e:
            shutil.rmtree(work_dir, ignore_errors=True)
            return cors_json({"error": f"Invalid batch ZIP: {e}"}, status=400)

    use_all_series = request.form.get('use_all_series') == '1'

    # input_kind decides whether the matched series is converted to
    # <role>.nii.gz or handed over as its original DICOM folder;
    # series_selection whether the tool gets one series per required role,
    # every series of its modality, or every series matching a role's
    # must-criteria. See batch_io's TOOL_INPUT_KIND / TOOL_SERIES_SELECTION.
    tool_specs = {}
    for entry_id, entry in entries.items():
        required_roles, tool_modality, _label = _batch_tool_spec(entry['tool'], entry['mode'])
        series_selection = TOOL_SERIES_SELECTION.get(entry['tool'], 'roles')
        # The checkbox only widens the default 'roles' selection. A tool with
        # its own hardcoded selection (LDCT's 'every') keeps it: 'all_matching'
        # is a NIfTI-path mode that never fills `source_series`, so it would
        # leave a 'dicom' tool with no series to run on at all.
        if (use_all_series and series_selection == 'roles'
                and entry['tool'] not in ALL_MATCHING_EXCLUDED_TOOLS):
            series_selection = 'all_matching'
        tool_specs[entry_id] = {
            'required_roles': required_roles,
            'tool_modality': tool_modality,
            'input_kind': TOOL_INPUT_KIND.get(entry['tool'], 'nifti'),
            'series_selection': series_selection,
        }
    studies = prepare_batch_for_tools(input_root, tool_specs)
    if not studies:
        shutil.rmtree(work_dir, ignore_errors=True)
        return cors_json({"error": "No patient/study/series folders found in the batch input."}, status=400)

    # Computed once here (not per tool) since study_date() only reads a
    # DICOM series' header, independent of which tools/roles were matched --
    # see compute_study_dates. Formatted to an ISO string up front because
    # the case record round-trips through batch_jobs.py's plain json.dump,
    # which can't serialize a date object.
    study_dates = {
        key: date.isoformat() if date else None
        for key, date in compute_study_dates(input_root).items()
    }

    cases = {
        f"{patient_id}/{study_name}": {
            "tool_inputs": tool_inputs,
            "study_date": study_dates.get((patient_id, study_name)),
            "tools": {
                entry_id: {
                    "tool": entries[entry_id]['tool'], "mode": entries[entry_id]['mode'],
                    "status": "pending", "error": None, "output_files": [], "stats": {},
                }
                for entry_id in tool_inputs
            },
        }
        for (patient_id, study_name), tool_inputs in studies.items()
    }

    batch_jobs.start_batch_job(
        job_id, kind='batch', cases=cases, work_dir=work_dir, params=full_params, owner=owner,
    )

    return cors_json({
        "job_id": job_id,
        "status": "pending",
        "message": f"Batch [{label}] started for {len(cases)} case(s).",
        "status_url": f"/batch/status/{job_id}",
    }, status=202)


@main.route('/batch/plan', methods=['POST'])
def batch_plan_route():
    """Say which series folders a batch of one or more (tool, mode)
    selections actually needs, from series *names* alone -- before any of
    them have been transferred.

    A caller staging a cohort out of iRODS (docker-context-gui's stage-in)
    knows every patient/study/series name from the catalog, but not which of
    them the tools will use; a real study folder holds 13-18 series and
    BraTS opens 4 of them. Asking here keeps SERIES_CRITERIA
    (backend_helpers.py) the single source of truth for that decision rather
    than growing a second copy of the keyword table in the GUI. See
    batch_io.plan_series_for_roles -- the answer is deliberately permissive,
    and the authoritative mapping still runs on real DICOM headers once the
    data has landed.

    Request JSON:
      {"tools": [{"tool": "brats", "mode": "goat"}, {"tool": "brats", "mode": "meningioma"},
                 {"tool": "fastsurfer", "mode": "seg"}],
       "studies": [{"patient_id": "P1", "study_name": "S1",
                    "series": ["11_t1_mprage...", ...]}, ...]}
    (the same tool can appear more than once with a different mode, e.g.
    running several BraTS algorithms in one job -- see
    _validate_tool_entries). Response JSON: the same studies, each with
    `series_needed` (the union across every selected (tool, mode) entry --
    subset of `series`, may be empty) and `missing_roles` (keyed by
    entry_id, only present for an entry actually missing one), plus
    batch-wide totals. Pure computation on the names given -- touches no
    files and starts no job.
    """
    payload = request.get_json(silent=True) or {}
    try:
        entries = _validate_tool_entries(payload.get('tools'))
    except ValueError as e:
        return cors_json({"error": str(e)}, status=400)

    tool_specs = {
        entry_id: _batch_tool_spec(entry['tool'], entry['mode'])[:2]  # (required_roles, tool_modality)
        for entry_id, entry in entries.items()
    }

    studies_in = payload.get('studies')
    if not isinstance(studies_in, list):
        return cors_json({"error": "Expected a 'studies' list."}, status=400)

    studies_out = []
    series_total = 0
    series_needed_total = 0
    for study in studies_in:
        series_names = study.get('series') or []
        series_total += len(series_names)

        needed = set()
        missing_roles = {}
        for entry_id, (required_roles, tool_modality) in tool_specs.items():
            plan = plan_series_for_roles(series_names, required_roles, tool_modality)
            needed.update(plan['series_needed'])
            if plan['missing_roles']:
                missing_roles[entry_id] = plan['missing_roles']

        needed_ordered = [name for name in series_names if name in needed]  # preserve input order
        series_needed_total += len(needed_ordered)
        studies_out.append({
            "patient_id": study.get('patient_id'),
            "study_name": study.get('study_name'),
            "series_needed": needed_ordered,
            "missing_roles": missing_roles,
        })

    return cors_json({
        "tools": list(tool_specs.keys()),
        "studies": studies_out,
        "series_total": series_total,
        "series_needed_total": series_needed_total,
    })


@main.route('/batch/run', methods=['POST'])
def batch_run_route():
    """Kick off an async batch run of one or more selected tools -- and one
    or more modes of the same tool, e.g. several BraTS algorithms run side
    by side -- over a cohort, as a single job (superseding the old
    one-job-per-tool
    /batch/fastsurfer|totalsegmentator|brats routes). See _start_batch_job's
    docstring for the general batch flow.

    `tools` (form field): JSON array of {"tool", "mode"} objects -- see
    _parse_tools_field. The same (tool, mode) pair can only be selected
    once per job; the same tool with a *different* mode each time is fine.
    """
    try:
        entries = _parse_tools_field(request.form.get('tools'))
    except ValueError as e:
        return cors_json({"error": str(e)}, status=400)

    labels = []
    for entry in entries.values():
        _required_roles, _tool_modality, label = _batch_tool_spec(entry['tool'], entry['mode'])  # re-validated, cheap
        labels.append(label)

    # Pull every selected BraTS segmenter's docker image, and fetch the
    # shared brainles_preprocessing atlas/SynthStrip assets every BraTS case
    # needs before it even reaches a segmenter, now, synchronously, before
    # batch_jobs.py's worker pool starts running cases concurrently -- see
    # ensure_segmenter_image_pulled's and
    # ensure_brats_preprocessing_assets_pulled's docstrings. Without this,
    # the first batch to touch either not-yet-cached asset on this host can
    # crash almost every case with rich.errors.LiveError("Only one live
    # display may be active at once") if several worker threads race to
    # fetch the same not-yet-cached asset at once -- which, with several
    # BraTS modes now selectable in one job, could otherwise also happen
    # *within* this same job's first run (two different segmenters, both
    # uncached, both starting at once).
    brats_segmenters = {entry['mode'] for entry in entries.values() if entry['tool'] == 'brats'}
    if brats_segmenters:
        try:
            ensure_brats_preprocessing_assets_pulled()
        except Exception as e:
            return cors_json({"error": f"Could not prepare BraTS preprocessing assets: {e}"}, status=502)
    for segmenter_name in brats_segmenters:
        try:
            ensure_segmenter_image_pulled(segmenter_name)
        except Exception as e:
            return cors_json(
                {"error": f"Could not prepare BraTS segmenter '{segmenter_name}': {e}"}, status=502,
            )

    return _start_batch_job(entries, ', '.join(labels))


@main.route('/batch/jobs', methods=['GET'])
def batch_jobs_list_route():
    """List batch jobs (summary only -- no per-case detail, see
    GET /batch/status/<job_id> for that), newest first. Filtered to
    ?owner=<owner> when given -- the GUI's "Your Batch Jobs" list uses this
    to show one user's jobs. See start_batch_job's docstring: `owner` is
    bookkeeping, not access control -- this API has no auth of its own."""
    return batch_jobs.batch_jobs_list_response(owner=request.args.get('owner') or None)


@main.route('/batch/status/<job_id>', methods=['GET'])
def batch_status_route(job_id):
    """Report a batch job's overall status plus per-case status/error."""
    return batch_jobs.batch_job_status_response(job_id)


@main.route('/batch/jobs/<job_id>', methods=['DELETE'])
def batch_delete_job_route(job_id):
    """Delete a finished (or paused/stopped) batch job's record and on-disk
    result (the GUI's "clear history" / per-job delete buttons). No auth of
    its own, same as every other /batch/* route -- see batch_status_route --
    so the GUI blueprint checks `owner` against the caller's identity before
    ever reaching this route (see push_to_irods in docker-context-gui's
    app/routes/batch.py)."""
    outcome = batch_jobs.delete_job(job_id)
    if outcome == "not_found":
        return cors_json({"error": "Unknown batch job id."}, status=404)
    if outcome == "running":
        return cors_json({"error": "Batch job is still running - stop it first, or wait for it to finish, before deleting it."}, status=409)
    return cors_json({"deleted": job_id})


@main.route('/batch/jobs/<job_id>/pause', methods=['POST'])
def batch_pause_job_route(job_id):
    """Pause a running batch job (the GUI's per-job pause button) -- see
    batch_jobs.pause_job's docstring for what "pause" actually means here
    (in-flight cases finish; only queued-but-not-started ones wait)."""
    outcome = batch_jobs.pause_job(job_id)
    if outcome == "not_found":
        return cors_json({"error": "Unknown batch job id."}, status=404)
    if outcome == "invalid":
        return cors_json({"error": "Batch job is not running, so it can't be paused."}, status=409)
    return cors_json({"job_id": job_id, "status": outcome})


@main.route('/batch/jobs/<job_id>/resume', methods=['POST'])
def batch_resume_job_route(job_id):
    """Resume a paused batch job (the GUI's per-job resume button)."""
    outcome = batch_jobs.resume_job(job_id)
    if outcome == "not_found":
        return cors_json({"error": "Unknown batch job id."}, status=404)
    if outcome == "invalid":
        return cors_json({"error": "Batch job is not paused, so it can't be resumed."}, status=409)
    return cors_json({"job_id": job_id, "status": "running"})


@main.route('/batch/jobs/<job_id>/stop', methods=['POST'])
def batch_stop_job_route(job_id):
    """Stop a batch job for good (the GUI's per-job stop button) -- unlike
    delete, this keeps the job's record and whatever cases already finished;
    see batch_jobs.stop_job's docstring for why an in-flight case still runs
    to completion rather than being killed outright."""
    outcome = batch_jobs.stop_job(job_id)
    if outcome == "not_found":
        return cors_json({"error": "Unknown batch job id."}, status=404)
    if outcome == "invalid":
        return cors_json({"error": "Batch job has already finished or been stopped."}, status=409)
    return cors_json({"job_id": job_id, "status": outcome})


@main.route('/batch/result/<job_id>', methods=['GET'])
def batch_result_route(job_id):
    """Download a batch job's output tree as a ZIP: the full result once
    'done', or whatever cases finished before someone paused/stopped it
    (batch_jobs.pause_job/stop_job -- both settle their preliminary
    manifest.csv/results_overview.csv the same way a normal finish does, see
    _run_completion_hook, before ever reaching 'paused'/'stopped').

    Refused for every other status: 'pending'/'running'/'pausing'/'stopping'
    all mean a worker thread may still be writing into result_root, and
    zipping that concurrently could pack a half-written file."""
    status = batch_jobs.get_batch_job_status(job_id)
    if status is None:
        return cors_json({"error": "Unknown batch job id."}, status=404)
    if status['status'] not in ('done', 'paused', 'stopped'):
        return cors_json({"error": f"Batch job is not finished yet (status: {status['status']})."}, status=409)

    result_root = os.path.join(batch_jobs.job_work_dir(job_id), 'result')
    archive_buffer = zip_output_tree(result_root)
    return cors_file(archive_buffer, f"batch_{job_id}_result.zip", "application/zip")


@main.route('/batch/result/<job_id>/partial', methods=['GET'])
def batch_result_partial_route(job_id):
    """Download whichever cases have already finished in a batch job that's
    still actively running -- the GUI's "Download finished so far" button
    next to Pause, for spot-checking a large cohort partway through without
    pausing or waiting for it.

    Unlike batch_result_route above, this works at *any* job status
    (including 'pending'/'running'/'pausing'/'stopping'), because it never
    risks packing a half-written file: a case only ever gets a terminal
    status ('done'/'partial'/'failed') once its runner call has already
    returned (see _run_worker_pool in batch_jobs.py), by which point every
    file that case was going to write is already on disk. A case that's
    still 'pending' or 'running' is simply left out of the ZIP rather than
    risking that race -- see zip_output_tree's `only_case_ids`.

    Includes a results_overview.csv built fresh from the job's current
    per-case records (see _build_results_overview_rows) -- covering every
    case, not just the finished ones, same as the real one _write_
    results_overview writes once the job settles, just built in-memory here
    since that hasn't run yet. No manifest.csv though: nobody asked for it
    here yet, and results_overview.csv is the one people actually want to
    watch progress from (SPSS/R/Excel), not a per-tool audit trail."""
    status = batch_jobs.get_batch_job_status(job_id)
    if status is None:
        return cors_json({"error": "Unknown batch job id."}, status=404)

    finished_case_ids = [c['id'] for c in status['per_case'] if c['status'] in ('done', 'partial', 'failed')]
    if not finished_case_ids:
        return cors_json({"error": "No studies have finished yet."}, status=409)

    fieldnames, rows = _build_results_overview_rows((c['id'], c) for c in status['per_case'])
    overview_buf = io.StringIO()
    writer = csv.DictWriter(overview_buf, fieldnames=fieldnames, restval='')
    writer.writeheader()
    writer.writerows(rows)

    result_root = os.path.join(batch_jobs.job_work_dir(job_id), 'result')
    archive_buffer = zip_output_tree(
        result_root, only_case_ids=finished_case_ids,
        extra_files={'results_overview.csv': overview_buf.getvalue()},
    )
    return cors_file(archive_buffer, f"batch_{job_id}_partial_result.zip", "application/zip")


@main.route('/fsl_register_qc/<studyInstanceUID>', methods=['GET'])
def fsl_register_qc_route(studyInstanceUID):
    """Register the study's T1 series to MNI152 (default params) and upload a QC snapshot, in the background."""
    work = logged_work(
        f"FSL registration+QC for study {studyInstanceUID}",
        lambda: fsl_register_qc_wrapper(studyInstanceUID)
        or f"FSL registration+QC completed and uploaded for study {studyInstanceUID}.",
    )
    return start_bg_job(
        kind='fsl_register_qc',
        work=work,
        message=f"FSL registration+QC started for study {studyInstanceUID}.",
    )

@main.route('/dti/tensor/<studyInstanceUID>', methods=['GET'])
def dti_tensor_route(studyInstanceUID):
    """Fit the diffusion tensor for the study's DTI series and upload FA, MD,
    colour FA, a QC series, a metrics table and the whole-brain fibre field,
    in the background."""
    work = logged_work(
        f"DTI tensor pipeline for study {studyInstanceUID}",
        lambda: dti_tensor_wrapper(studyInstanceUID)
        or f"DTI tensor completed and uploaded for study {studyInstanceUID}.",
    )
    return start_bg_job(
        kind='dti:tensor',
        work=work,
        message=f"DTI tensor pipeline started for study {studyInstanceUID}.",
    )

@main.route('/dti/tractography/<studyInstanceUID>', methods=['GET'])
def dti_tractography_route(studyInstanceUID):
    """Run bedpostx + XTRACT for the study and upload the reconstructed tracts
    as colour overlays on FA, in the background.

    Runs the tensor stage first if this study has not had it (see
    dti_tractography_wrapper), so the button does not depend on the order the
    two were pressed in. Expect 45-60 minutes on top of the tensor stage --
    the frontend entry carries a matching pollTimeoutMinutes."""
    work = logged_work(
        f"DTI tractography pipeline for study {studyInstanceUID}",
        lambda: dti_tractography_wrapper(studyInstanceUID)
        or f"DTI tractography completed and uploaded for study {studyInstanceUID}.",
    )
    return start_bg_job(
        kind='dti:tractography',
        work=work,
        message=f"DTI tractography started for study {studyInstanceUID}. "
                "This takes about an hour.",
    )

@main.route('/dti/reupload/<studyInstanceUID>', methods=['GET'])
def dti_reupload_route(studyInstanceUID):
    """Write the study's DTI image series again from its existing work
    directory, without recomputing the pipeline, in the background.

    A maintenance endpoint rather than a viewer button: it exists for fixing
    an upload after the fact (a corrected DICOM layout, say) on a study whose
    FSL outputs are still on disk. It adds new series rather than replacing
    the old ones -- see dti_reupload_wrapper.

    Pass ?tables=1 to rewrite the two tractography tables as well, for when
    the tract results changed rather than only the way they were written."""
    include_tables = request.args.get('tables', '').lower() in ('1', 'true', 'yes')
    work = logged_work(
        f"DTI re-upload for study {studyInstanceUID}",
        lambda: dti_reupload_wrapper(studyInstanceUID, include_tables=include_tables)
        or f"DTI series re-uploaded for study {studyInstanceUID}.",
    )
    return start_bg_job(
        kind='dti:reupload',
        work=work,
        message=f"DTI re-upload started for study {studyInstanceUID}.",
    )

DTI_DOWNLOADABLE_FILES = {
    # Interactive whole-brain fibre viewer, opened inline so it renders as a
    # page rather than downloading.
    'streamlines_3d.html': 'text/html',
    # Legacy-VTK export of the same streamlines, for 3D Slicer (core Models,
    # or a Fiber Bundle with the SlicerDMRI extension), ParaView, MI-Brain,
    # etc. -- see dti_streamlines.write_streamline_vtk.
    'streamlines.vtk': 'application/octet-stream',
}


@main.route('/dti_files/<studyInstanceUID>/<filename>', methods=['GET'])
def dti_files_route(studyInstanceUID, filename):
    """Serve a file from a study's DTI work directory directly to the
    browser -- the interactive whole-brain fibre viewer and its VTK export
    (both written by dti_functions._generate_streamline_cache and linked
    from the tensor and tractography wrappers' return messages).

    Whitelisted to fixed filenames, the same reasoning as
    hippocampus_files_route's DOWNLOADABLE_FILES: studyInstanceUID and
    filename both come straight from the URL, so anything less would let a
    request read arbitrary paths out of the work directory.
    """
    mimetype = DTI_DOWNLOADABLE_FILES.get(filename)
    if mimetype is None:
        return cors_json({"error": f"Unknown filename. Must be one of: "
                                   f"{list(DTI_DOWNLOADABLE_FILES)}"}, status=400)
    path = os.path.join(DTI_STORAGE_ROOT, studyInstanceUID, filename)
    if not os.path.exists(path):
        return cors_json({"error": "No fibre-field output of this kind for this study "
                                   "-- run the DTI tensor pipeline for it first."}, status=404)
    response = send_file(path, mimetype=mimetype)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

@main.route('/screw_infer/<studyInstanceUID>', methods=['GET'])
def screw_inference_route(studyInstanceUID):
    """Run live screw-segmentation inference on the study's own CT and upload a DICOM SEG, in the background."""
    work = logged_work(
        f"live screw inference for study {studyInstanceUID}",
        lambda: screw_inference_wrapper(studyInstanceUID)
        or f"Screw inference completed and uploaded for study {studyInstanceUID}.",
    )
    return start_bg_job(
        kind='screw_infer',
        work=work,
        message=f"Screw inference started for study {studyInstanceUID}.",
    )

@main.route('/hippocampus/<studyInstanceUID>', methods=['GET'])
def hippocampus_route(studyInstanceUID):
    """Run FSL FIRST hippocampus segmentation in the background and return a job id."""
    work = logged_work(
        f"hippocampus segmentation for study {studyInstanceUID}",
        lambda: hippocampus_wrapper(studyInstanceUID)
        or f"Hippocampus segmentation completed and uploaded for study {studyInstanceUID}.",
    )
    return start_bg_job(
        kind='hippocampus',
        work=work,
        message=f"Hippocampus segmentation started for study {studyInstanceUID}.",
    )

@main.route('/siena/<studyInstanceUID>', methods=['GET'])
def siena_route(studyInstanceUID):
    """Run FSL brain atrophy measurement in the background and return a job id.

    One route for both analyses: siena_wrapper picks longitudinal SIENA when
    the patient has a second MR study carrying the same structural sequence,
    and cross-sectional SIENAX otherwise -- see plan_siena_analysis.

    Optional '?bet=<options>' overrides the BET brain-extraction options
    (default DEFAULT_BET_OPTIONS). Brain extraction is the dominant error
    source for both tools and a re-run costs 15+ minutes, so this is tunable
    per request rather than only by editing the default; the value is
    whitelisted by validate_bet_options. Example: ?bet=-B%20-f%200.4"""
    try:
        bet_options = validate_bet_options(request.args.get('bet', DEFAULT_BET_OPTIONS))
    except ValueError as error:
        return cors_json({"error": str(error)}, status=400)

    work = logged_work(
        f"SIENA/SIENAX brain atrophy for study {studyInstanceUID} (bet: {bet_options or 'FSL default'})",
        lambda: siena_wrapper(studyInstanceUID, bet_options=bet_options)
        or f"Brain atrophy analysis completed and uploaded for study {studyInstanceUID}.",
    )
    return start_bg_job(
        kind='siena',
        work=work,
        message=f"Brain atrophy analysis started for study {studyInstanceUID}.",
    )

@main.route('/hippocampus_reoriented/<studyInstanceUID>', methods=['GET'])
def hippocampus_reoriented_route(studyInstanceUID):
    """Upload a standard-space (axis-aligned) copy of the T1 + hippocampus SEG
    for a study that already has hippocampus segmentation results, so plain
    axial/coronal/sagittal viewing shows true anatomical planes instead of
    the source series' oblique acquisition geometry."""
    work = logged_work(
        f"reoriented hippocampus series for study {studyInstanceUID}",
        lambda: upload_reoriented_hippocampus_series(studyInstanceUID)
        or f"Reoriented hippocampus series completed and uploaded for study {studyInstanceUID}.",
    )
    return start_bg_job(
        kind='hippocampus_reoriented',
        work=work,
        message=f"Reoriented hippocampus series started for study {studyInstanceUID}.",
    )

@main.route('/hippocampus_files/<studyInstanceUID>/<filename>', methods=['GET'])
def hippocampus_files_route(studyInstanceUID, filename):
    """Download the standard-space T1 or hippocampus segmentation NIfTI for
    a study hippocampus_wrapper() has already run on -- e.g. to inspect in
    3D Slicer, which shows true MPR directly without needing the extra
    reorientation step OHIF's viewer would (see hippocampus_wrapper's
    return message, which links here)."""
    if filename not in DOWNLOADABLE_FILES:
        return cors_json({"error": f"Unknown filename. Must be one of: {list(DOWNLOADABLE_FILES)}"}, status=400)
    path = f"{API_STORAGE_DIR}/hippocampus/{studyInstanceUID}/{filename}"
    if not os.path.exists(path):
        return cors_json({"error": "File not found -- run hippocampus segmentation for this study first."}, status=404)
    return cors_file(path, filename, "application/gzip")

_HIPPOCAMPUS_STORAGE_DIR = f"{API_STORAGE_DIR}/hippocampus"


def _lookup_patient_label(studyInstanceUID):
    """Best-effort 'Name (ID)' for a study, so the index page is browsable
    without already knowing which UID belongs to which patient. Falls back
    to the bare UID if dcm4chee can't be reached or the study was deleted
    from PACS after segmentation ran."""
    try:
        results = client.search_for_studies(search_filters={'StudyInstanceUID': studyInstanceUID})
        if results:
            name = results[0].get('00100010', {}).get('Value', [{}])[0].get('Alphabetic', '')
            pid = results[0].get('00100020', {}).get('Value', [''])[0]
            if name or pid:
                return f"{name or '?'} (ID {pid or '?'})"
    except Exception:
        pass
    return studyInstanceUID


@main.route('/hippocampus_links/', methods=['GET'], defaults={'studyInstanceUID': None})
@main.route('/hippocampus_links/<studyInstanceUID>', methods=['GET'])
def hippocampus_links_route(studyInstanceUID):
    """Plain HTML page(s) listing the same 3D Slicer download URLs as the
    persistent DICOM SC card hippocampus_wrapper() uploads into each study --
    but as real, selectable text, since DICOM pixel data (what the card
    actually is) can't be copy-pasted.

    With no UID (/api-proxy/hippocampus_links/), lists every study that has
    results, most recently segmented first, so nothing needs to be typed or
    copied from OHIF first. With a UID, shows that study's two links
    directly -- kept for deep-linking from the SC card / job toast."""
    if studyInstanceUID is None:
        if not os.path.isdir(_HIPPOCAMPUS_STORAGE_DIR):
            entries = []
        else:
            entries = sorted(
                (
                    (os.path.getmtime(os.path.join(_HIPPOCAMPUS_STORAGE_DIR, name)), name)
                    for name in os.listdir(_HIPPOCAMPUS_STORAGE_DIR)
                    if os.path.exists(os.path.join(_HIPPOCAMPUS_STORAGE_DIR, name, 'hippocampus_segmentation_std.nii.gz'))
                ),
                reverse=True,
            )
        # Relative hrefs (no leading "/"): this page is served through
        # /api-proxy/hippocampus_links/, and a root-relative link would drop
        # that prefix and 404 against the GUI app instead of the proxy.
        rows = "".join(
            f'<tr><td>{html.escape(_lookup_patient_label(uid))}</td>'
            f'<td>{datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")}</td>'
            f'<td><a href="{html.escape(uid)}">Open links</a></td></tr>'
            for mtime, uid in entries
        ) or '<tr><td colspan="3">No hippocampus segmentation results yet.</td></tr>'
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Hippocampus 3D Slicer links</title></head>
<body style="font-family:sans-serif; max-width:900px; margin:2em auto;">
<h3>Studies with FSL FIRST hippocampus results</h3>
<table border="1" cellpadding="6" style="border-collapse:collapse; width:100%;">
<tr><th align="left">Patient</th><th align="left">Segmented</th><th></th></tr>
{rows}
</table>
</body></html>"""

    storage_dir = os.path.join(_HIPPOCAMPUS_STORAGE_DIR, studyInstanceUID)
    if not os.path.isdir(storage_dir):
        return "<p>No hippocampus segmentation results for this study yet -- run it first.</p>", 404

    uid = html.escape(studyInstanceUID)
    t1_url = html.escape(f"{FILES_BASE_URL}/hippocampus_files/{studyInstanceUID}/t1n_std.nii.gz")
    seg_url = html.escape(f"{FILES_BASE_URL}/hippocampus_files/{studyInstanceUID}/hippocampus_segmentation_std.nii.gz")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Hippocampus 3D Slicer links</title></head>
<body style="font-family:sans-serif; max-width:800px; margin:2em auto;">
<p><a href="./">&larr; all studies</a></p>
<h3>FSL FIRST Hippocampus -- 3D Slicer download links</h3>
<p>Study {uid}</p>
<p>T1 (Standard Space) -- load in 3D Slicer as <b>Volume</b>:</p>
<pre style="white-space:pre-wrap; word-break:break-all; background:#f0f0f0; padding:10px; border-radius:4px;">{t1_url}</pre>
<p>Hippocampus Segmentation (Standard Space) -- load as <b>Labelmap</b> (17=Left, 53=Right):</p>
<pre style="white-space:pre-wrap; word-break:break-all; background:#f0f0f0; padding:10px; border-radius:4px;">{seg_url}</pre>
</body></html>"""

@main.route('/new_study_uid', methods=['GET'])
def new_study_uid_route():
    """Mint a StudyInstanceUID the frontend can reuse across every file in
    one upload batch (NIfTI and/or NRRD), so they land in the same study as
    separate series."""
    return cors_json({"studyInstanceUID": pydicom.uid.generate_uid()})

@main.route('/volume_upload', methods=['POST'])
def volume_upload_route():
    """Convert one uploaded volume file (.nii/.nii.gz/.nrrd) to a DICOM
    series in the background and STOW it to dcm4chee, using caller-supplied
    patient/study metadata (the source file has no DICOM header to source
    those from).

    Expects multipart form-data: `file`, `metadata` (JSON string of the
    patient/study fields), and optionally `studyInstanceUID` (to batch
    several files into one study) and `seriesNumber`.
    """
    uploaded_file = request.files.get('file')
    if uploaded_file is None or not uploaded_file.filename:
        return cors_json({"error": "No file uploaded."}, status=400)

    filename = secure_filename(uploaded_file.filename)
    converter = _volume_converter_for_filename(filename)
    if converter is None:
        return cors_json({"error": "Only .nii, .nii.gz, or .nrrd files are supported."}, status=400)

    try:
        metadata = json.loads(request.form.get('metadata') or '{}')
    except ValueError:
        return cors_json({"error": "Invalid metadata JSON."}, status=400)

    study_instance_uid = request.form.get('studyInstanceUID') or None
    try:
        series_number = int(request.form.get('seriesNumber', 1))
    except ValueError:
        series_number = 1

    work_dir = tempfile.mkdtemp(prefix='volume_upload_src_')
    source_path = os.path.join(work_dir, filename)
    uploaded_file.save(source_path)

    def do_work():
        try:
            study_uid, series_uid = converter(
                source_path, metadata,
                study_instance_uid=study_instance_uid,
                series_number=series_number,
            )
            return f"Series uploaded (Study {study_uid}, Series {series_uid})."
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    work = logged_work(f"Volume-to-DICOM conversion for '{filename}'", do_work)
    return start_bg_job(
        kind='volume_upload',
        work=work,
        message=f"Uploading and converting '{filename}'...",
    )


@main.route('/segmentation_upload/<studyInstanceUID>/<seriesInstanceUID>', methods=['POST'])
def segmentation_upload_route(studyInstanceUID, seriesInstanceUID):
    """Store a segmentation mask made outside this API as a DICOM SEG in the
    study it was drawn on.

    This is the write-back path for the interactive tools: SAMinRAD's viewer
    (docker-context-saminrad/) imports a series from dcm4chee, the person
    segments it by hand with SAM2, and the resulting mask comes back here to
    become a SEG on the same study - so the annotation ends up next to the
    images it belongs to instead of only in a ZIP on somebody's laptop.

    Expects multipart form-data with `file` (the mask as .nii/.nii.gz, on the
    source series' own voxel grid - see mask_array_for_series) and optionally
    `segmentLabel`, `seriesDescription`, `seriesNumber` and `modelName`.

    Reached from a browser through the GUI's /api-proxy/, which is what
    attributes the store to a person: this API has no authentication of its
    own. Both UIDs sit in the path rather than the form so that proxy can
    record *which* study was written to.
    """
    uploaded_file = request.files.get('file')
    if uploaded_file is None or not uploaded_file.filename:
        return cors_json({"error": "No mask file uploaded."}, status=400)

    filename = secure_filename(uploaded_file.filename)
    if not filename.lower().endswith(('.nii', '.nii.gz')):
        return cors_json({"error": "The segmentation mask must be a .nii or .nii.gz file."}, status=400)

    segment_label = request.form.get('segmentLabel') or 'Segmentation'
    seg_description = request.form.get('seriesDescription') or 'Interactive Segmentation'
    model_name = request.form.get('modelName') or 'ThresholdSegmenter'
    try:
        series_number = int(request.form.get('seriesNumber', 5000))
    except ValueError:
        series_number = 5000

    work_dir = tempfile.mkdtemp(prefix='segmentation_upload_')
    mask_path = os.path.join(work_dir, filename)
    uploaded_file.save(mask_path)

    def do_work():
        try:
            _seg, labels, values = upload_mask_file_as_segmentation(
                mask_path, studyInstanceUID, seriesInstanceUID,
                segment_label=segment_label,
                seg_description=seg_description,
                seg_series_number=series_number,
                model_name=model_name,
            )
            return (f"Segmentation stored as series {series_number} "
                    f"('{seg_description}', {len(labels)} segment(s): {', '.join(labels)}).")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    work = logged_work(
        f"SEG upload for series {seriesInstanceUID} of study {studyInstanceUID}", do_work)
    return start_bg_job(
        kind='segmentation_upload',
        work=work,
        message=f"Storing '{seg_description}' as a DICOM SEG...",
    )
