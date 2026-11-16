"""
FastSurfer longitudinal processing (the FreeSurfer longitudinal stream, as
implemented by FastSurfer's own long_fastsurfer.sh).

Where fastsurfer_functions.py segments one study on its own, this builds an
unbiased person-specific template from *every* timepoint the patient has,
segments that template, and then initialises each timepoint's segmentation and
surfaces from it. That shared initialisation is the whole point: a
cross-sectional run of the same subject twice differs by the network's own
run-to-run behaviour and by however the two acquisitions happened to conform,
and that difference lands on top of whatever real change is being looked for.
The longitudinal stream removes it, which is what makes a per-structure
change-over-time number worth quoting at all.

It is deliberately a *separate* button from the cross-sectional one and never
a fallback for it: it only applies when the patient has more than one MR study
with a matching T1, it costs several hours rather than one, and its per-
timepoint numbers are not the same quantity as a cross-sectional run's (they
are estimated in the template's space), so mixing the two in one series of
results would be misleading.

Reference: Reuter M, Schmansky NJ, Rosas HD, Fischl B. Within-subject template
estimation for unbiased longitudinal image analysis, NeuroImage 61:4 (2012).
"""
import os
import shutil

import nibabel as nib
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from PIL import Image

from .job_logging import run_logged
from .backend_helpers import (
    choose_gpu_index, client, log, rasterize_figure, store_series_as_nifti,
    upload_dataframe_as_sc, upload_images_as_sc, upload_segmentation,
)
from .batch_io import classify_studies_for_patient, series_label
from .fastsurfer_functions import (
    FASTSURFER_IMAGE, FASTSURFER_LICENSE_DIR, ensure_fs_license,
    _cortical_thickness_table, _hemisphere_volume_table,
    _require_surface_outputs, _resample_segmentation_to_t1_grid,
    _segmentation_labels_from_aseg_stats, _select_segmentation_and_stats,
    _view_aggregation_device,
)
# SIENA is the other pipeline that has to look past the clicked study at the
# patient's whole history, and it already answers the three questions this one
# asks -- who is the patient, which of their studies could hold an MR series,
# and which series in a given study best fills a role. Reused rather than
# reimplemented so the two agree on what counts as a comparable timepoint;
# same-package private import as screw_inference_functions does of
# screw_functions.
from .siena_functions import _best_series_for_roles, _current_study, _patient_studies
from .storage_paths import API_STORAGE_DIR

STORAGE_ROOT = f'{API_STORAGE_DIR}/fastsurfer_long'

# Structural series a longitudinal run can be built on, in preference order,
# and matched *by role across the timepoints* the same way SIENA does -- see
# plan_siena_analysis. The longitudinal stream co-registers the timepoints
# into a mid-space with a rigid transform only, so it assumes the same
# sequence at every timepoint; long_prepare_template.sh says as much itself,
# warning "Image parameters differ across time ... can potentially bias a
# longitudinal study" when the geometries disagree.
#
# 't1n_acquired' before 't1n' for the reason spelled out on those two entries
# in backend_helpers.SERIES_CRITERIA: 't1n' prefers the console's MPR
# reformat, whose reconstruction box and angulation are chosen per scan, so
# two timepoints' reformats differ in ways the original acquisitions do not.
# The cross-sectional button resolves T1 through 't1n' and is left alone; here
# the reformat's per-scan geometry is exactly what has to be avoided.
LONGITUDINAL_ROLES = ('t1n_acquired', 't1n')

# Cap on how many timepoints go into one run. Every extra timepoint costs a
# full segmentation + surface reconstruction (roughly an hour on this box) on
# top of the shared template, so an unbounded patient history would produce a
# job nobody waits for -- the frontend's poll gives up long before it. Four
# keeps the worst case near six hours; _select_timepoints keeps the earliest
# and latest (which define the interval the change is measured over) and
# spreads the rest evenly in between.
MAX_TIMEPOINTS = 4

# The person-specific template's subject id inside the output tree. Fixed
# rather than derived: the tree is per-study scratch (see fastsurfer_long_core)
# and holds exactly one template.
TEMPLATE_ID = 'base'

_TIMEPOINTS_SERIES_NUMBER = 6100
_VOLUME_SERIES_NUMBER = 6101
_THICKNESS_SERIES_NUMBER = 6102
_RENDER_SERIES_NUMBER = 6103
_SEG_SERIES_NUMBER = 6104

# Rows whose annualised change exceeds this on either side are highlighted in
# the rendered trajectory tables. Both are reporting conventions in the same
# spirit as fastsurfer_functions.ASYMMETRY_HIGHLIGHT_PERCENT and SIENA's
# _NORMAL_ANNUAL_PBVC_RANGE -- coarse, age-unadjusted, and not diagnostic
# cut-offs. Whole-brain volume loss runs about 0.2-0.5 %/year in healthy
# ageing and cortical thinning about 0.5 %/year, with individual structures
# (hippocampus above all) moving several times faster in neurodegeneration;
# the thresholds sit above the healthy-ageing band so the highlight means
# "worth a second look", not "abnormal". The numbers stay in the table
# either way.
VOLUME_ANNUAL_HIGHLIGHT_PERCENT = 2.0
THICKNESS_ANNUAL_HIGHLIGHT_PERCENT = 1.0

# Gaps shorter than this are highlighted in the timepoints table. Nothing is
# refused over it -- two scans a month apart are a perfectly valid pair, and
# their per-timepoint volumes in the trajectory tables are unaffected -- but
# the annualised columns divide by the interval, so a short one multiplies
# whatever measurement noise the pair carries by the same factor it multiplies
# the real change. Saying so on the provenance table is the honest way to
# report it.
SHORT_INTERVAL_DAYS = 90

# Structures plotted in the trajectory renders, by the region name
# _hemisphere_volume_table produces (FreeSurfer's 'Left-'/'Right-' prefix
# already stripped). Chosen for what a dementia/epilepsy reader actually looks
# at -- medial temporal structures and the ventricular compartments that
# enlarge as they shrink -- rather than for the largest numbers. Anything not
# present in this subject's parcellation is silently skipped, so this list can
# name structures FastSurfer renamed between versions without breaking a run.
PLOTTED_STRUCTURES = (
    'Hippocampus',
    'Amygdala',
    'Inf-Lat-Vent',
    'Lateral-Ventricle',
    'Thalamus',
    'Thalamus-Proper',
    'Putamen',
    'Caudate',
    'Accumbens-area',
    'Cerebellum-Cortex',
)


### ---------------------------------------------------------------------- ###
### deciding what to run: the patient's studies -> an ordered timepoint set ###
### ---------------------------------------------------------------------- ###

def _timepoint_id(index, date):
    """Subject id for one timepoint inside the output tree. The date is in the
    name purely so the tree is readable when a run has to be debugged; the
    index keeps it unique and orderable even if two studies somehow share a
    date. Must not start with '-': long_fastsurfer.sh's --tpids parser reads
    values until the next argument that does."""
    return f'tp{index}_{date:%Y%m%d}'


def _select_timepoints(studies, max_timepoints=MAX_TIMEPOINTS):
    """Trim a chronological study list down to at most `max_timepoints`,
    always keeping the first and the last and spreading the rest evenly
    between them.

    The endpoints are kept because they define the interval every change
    number is measured over -- dropping either shortens the interval and so
    shrinks the change-to-noise ratio, which is the opposite of what the extra
    timepoints are for. The ones in between only refine the trajectory's
    shape, so which of them survives matters much less than that they stay
    spread out."""
    if len(studies) <= max_timepoints:
        return studies
    indices = sorted({int(round(i)) for i in np.linspace(0, len(studies) - 1, max_timepoints)})
    return [studies[i] for i in indices]


def plan_longitudinal_fastsurfer(studyInstanceUID):
    """Work out which of the patient's studies this longitudinal run covers,
    and on which series.

    The rules, in order:

    1. Only a role both the clicked study *and* at least one other study carry
       is usable, and every timepoint contributes the series matching that one
       role -- see LONGITUDINAL_ROLES for why "same sequence at every
       timepoint" is a hard requirement rather than a preference.
    2. LONGITUDINAL_ROLES order breaks ties between two roles that both have
       more than one timepoint.
    3. Studies without a parseable StudyDate are dropped: the trajectory is
       plotted against time and the change is annualised, neither of which
       means anything without one.
    4. At most one study per date -- the clicked one if it is among them,
       otherwise the first the server returned. A same-day repeat measures
       scan-rescan reproducibility rather than change over time, and would
       collide with its sibling in the per-date table columns.
    5. The clicked study is always one of the timepoints -- results are
       uploaded there -- and the set is capped by _select_timepoints.

    Returns a dict with 'role' and 'timepoints': a chronologically ordered
    list of at least two {'uid', 'series_uid', 'series_description', 'date',
    'tpid', 'label', 'years', 'is_current'} entries, where 'years' is the
    interval from the baseline timepoint.
    """
    current, patient_id = _current_study(studyInstanceUID)
    if current['date'] is None:
        raise ValueError(
            f"Study {studyInstanceUID} has no usable StudyDate, so it cannot be placed on a "
            "timeline against the patient's other studies."
        )

    others = [s for s in (_patient_studies(patient_id) if patient_id else [])
              if s['uid'] != studyInstanceUID and s['date'] is not None]
    if not others:
        raise ValueError(
            f"Patient of study {studyInstanceUID} has no other dated MR study, so there is "
            "nothing to process longitudinally. Use the cross-sectional FastSurfer button "
            "for a single study."
        )

    studies = sorted([current] + others, key=lambda s: s['date'])
    roles_by_study = {s['uid']: _best_series_for_roles(s['uid'], LONGITUDINAL_ROLES)
                      for s in studies}

    for role in LONGITUDINAL_ROLES:
        if role not in roles_by_study[studyInstanceUID]:
            continue

        # One study per date, earliest first (rule 4). The clicked study wins
        # its own date outright, so a same-day sibling can never displace it.
        by_date = {}
        for study in studies:
            if role not in roles_by_study[study['uid']]:
                continue
            if study['date'] not in by_date or study['uid'] == studyInstanceUID:
                by_date[study['date']] = study
        candidates = [by_date[date] for date in sorted(by_date)]
        if len(candidates) < 2:
            continue

        selected = _select_timepoints(candidates)
        if not any(s['uid'] == studyInstanceUID for s in selected):
            # The cap dropped the clicked study; put it back in place of the
            # nearest interior timepoint, since results are uploaded to it and
            # it therefore has to be one of the ones actually processed.
            replaceable = list(range(1, len(selected) - 1)) or list(range(len(selected)))
            replace = min(
                replaceable,
                key=lambda i: abs((selected[i]['date'] - current['date']).days),
            )
            selected[replace] = current
            selected = sorted(selected, key=lambda s: s['date'])

        baseline_date = selected[0]['date']
        timepoints = []
        for index, study in enumerate(selected, start=1):
            series_uid, series_description = roles_by_study[study['uid']][role]
            timepoints.append({
                'uid': study['uid'],
                'series_uid': series_uid,
                'series_description': series_description,
                'date': study['date'],
                'tpid': _timepoint_id(index, study['date']),
                'label': study['date'].isoformat(),
                'years': (study['date'] - baseline_date).days / 365.25,
                'is_current': study['uid'] == studyInstanceUID,
            })

        log(
            f"Longitudinal FastSurfer: patient {patient_id} has {len(candidates)} dated "
            f"studies with a '{role}' series; processing {len(timepoints)} of them "
            f"({timepoints[0]['label']} -> {timepoints[-1]['label']})"
        )
        return {'role': role, 'timepoints': timepoints}

    raise ValueError(
        f"Study {studyInstanceUID} and the patient's other studies have no T1 series in "
        "common for a longitudinal FastSurfer run (every timepoint must carry the same "
        "sequence)."
    )


def plan_longitudinal_fastsurfer_batch(root_dir, patient_id, study_name):
    """plan_longitudinal_fastsurfer's counterpart for the batch pipeline: pick
    timepoints from the *other study folders this patient already has in the
    uploaded batch cohort*, instead of the patient's PACS history -- the
    batch tree has no dcm4chee study behind it to query. Same five rules
    (see plan_longitudinal_fastsurfer's docstring), applied to
    batch_io.classify_studies_for_patient's local role/date resolution.
    Kept as a separate function rather than a shared helper so the
    interactive, dcm4chee-backed route is untouched by batch-only changes.

    `study_name` is the batch tree's folder name for the case this is being
    planned for (routes.py's _run_fastsurfer_long_tool), playing the role
    studyInstanceUID plays above.

    Returns the same {'role', 'timepoints'} shape as plan_longitudinal_fastsurfer,
    except each timepoint dict carries 'study_name' + 'candidate' (a
    batch_io.classify_series() dict, for batch_io.materialize_timepoint_inputs)
    instead of 'uid'/'series_uid'.
    """
    studies = classify_studies_for_patient(root_dir, patient_id, LONGITUDINAL_ROLES, 'MR')
    current = next((s for s in studies if s['study_name'] == study_name), None)
    if current is None or current['date'] is None:
        raise ValueError(
            f"Study '{study_name}' has no usable StudyDate in its DICOM series, so it cannot "
            "be placed on a timeline against the patient's other studies in this cohort."
        )

    others = [s for s in studies if s['study_name'] != study_name and s['date'] is not None]
    if not others:
        raise ValueError(
            f"Patient '{patient_id}' has no other dated MR study in this batch cohort, so "
            f"there is nothing to process longitudinally for study '{study_name}'."
        )

    all_studies = sorted([current] + others, key=lambda s: s['date'])

    for role in LONGITUDINAL_ROLES:
        if role not in current['role_map']:
            continue

        # One study per date, earliest first (rule 4) -- see
        # plan_longitudinal_fastsurfer's identical step.
        by_date = {}
        for study in all_studies:
            if role not in study['role_map']:
                continue
            if study['date'] not in by_date or study['study_name'] == study_name:
                by_date[study['date']] = study
        candidates = [by_date[date] for date in sorted(by_date)]
        if len(candidates) < 2:
            continue

        selected = _select_timepoints(candidates)
        if not any(s['study_name'] == study_name for s in selected):
            replaceable = list(range(1, len(selected) - 1)) or list(range(len(selected)))
            replace = min(
                replaceable,
                key=lambda i: abs((selected[i]['date'] - current['date']).days),
            )
            selected[replace] = current
            selected = sorted(selected, key=lambda s: s['date'])

        baseline_date = selected[0]['date']
        timepoints = []
        for index, study in enumerate(selected, start=1):
            candidate = study['role_map'][role]
            timepoints.append({
                'study_name': study['study_name'],
                'candidate': candidate,
                'series_description': series_label(candidate),
                'date': study['date'],
                'tpid': _timepoint_id(index, study['date']),
                'label': study['date'].isoformat(),
                'years': (study['date'] - baseline_date).days / 365.25,
                'is_current': study['study_name'] == study_name,
            })

        log(
            f"Batch longitudinal FastSurfer: patient '{patient_id}' has {len(candidates)} "
            f"dated studies with a '{role}' series in this cohort; processing "
            f"{len(timepoints)} of them ({timepoints[0]['label']} -> {timepoints[-1]['label']})"
        )
        return {'role': role, 'timepoints': timepoints}

    raise ValueError(
        f"Study '{study_name}' and this patient's other studies in the cohort have no T1 "
        "series in common for a longitudinal FastSurfer run (every timepoint must carry the "
        "same sequence)."
    )


### ---------------------------------------------------------------------- ###
### pure local-file compute -- no dcm4chee access below this point          ###
### ---------------------------------------------------------------------- ###

def _fastsurfer_docker_command(entrypoint, output_dir, input_dir=None, gpu_index=None):
    """`docker run` prefix for one stage of the longitudinal pipeline, up to
    and including the image name. Mirrors fastsurfer_functions'
    _run_fastsurfer_container's mounts, except that /input holds one T1 per
    timepoint rather than a single t1n.nii.gz, and that the entrypoint is
    chosen per stage (see _run_longitudinal_pipeline).

    FS_LICENSE is set in the environment as well as passed as --fs_license,
    because _map_segmentation_to_native runs a FreeSurfer binary (mri_convert)
    directly rather than through one of the FastSurfer wrapper scripts, and
    only those scripts turn --fs_license into the environment variable the
    binaries themselves look for. Without it mri_convert exits 255 on "license
    file /opt/freesurfer/.license not found" before reading its input."""
    ensure_fs_license()
    gpus_arg = 'all' if gpu_index is None else f'device={gpu_index}'
    cmd = [
        'docker', 'run', '--rm', '--gpus', gpus_arg,
        '--user', f'{os.getuid()}:{os.getgid()}',
        '-e', 'FS_LICENSE=/fs_license/license.txt',
        '-v', f'{output_dir}:/output',
        '-v', f'{FASTSURFER_LICENSE_DIR}:/fs_license:ro',
    ]
    if input_dir is not None:
        cmd += ['-v', f'{input_dir}:/input:ro']
    return cmd + ['--entrypoint', entrypoint, FASTSURFER_IMAGE]


def _run_stage(label, cmd):
    log(f"Longitudinal FastSurfer: {label}")
    result = run_logged(cmd, name='fastsurfer_long')
    if result.returncode != 0:
        raise RuntimeError(
            f"FastSurfer longitudinal stage '{label}' failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def _run_longitudinal_pipeline(input_dir, output_dir, tpids, viewagg_device=None,
                               gpu_index=None):
    """Drive long_fastsurfer.sh's five stages over `tpids` (chronological),
    reading /input/<tpid>.nii.gz for each and writing every subject directory
    under output_dir.

    Three of the five stages go through long_fastsurfer.sh itself; the two
    person-template ones are invoked as plain run_fastsurfer.sh calls that do
    exactly what long_fastsurfer.sh would have run for them. That split is
    forced by how it handles --allow_root: its argument parser matches
    --allow_root in its own case arm and appends it to `brun_flags` only, so
    the flag reaches brun_fastsurfer.sh (and through it the long_seg/long_surf
    time point runs) but never the two `run_fastsurfer.sh ... --base` calls it
    makes directly. run_fastsurfer.sh calls check_allow_root and exits 1 as
    root without it, and this API container runs as root (no USER in
    API/Dockerfile), so driving those two stages here is what makes a
    longitudinal run possible at all under this deployment. The prepare stage
    needs no such treatment: long_prepare_template.sh accepts --allow_root as
    a no-op and never checks the uid.

    Note long_fastsurfer.sh lower-cases every argument it does not recognise
    before passing it on, so only all-lowercase values may be handed to it
    positionally -- true of everything in `passthrough` below.
    """
    # run_fastsurfer.sh options shared by every stage. --3T and --threads
    # match the cross-sectional route's container call so the two produce
    # comparable segmentations; --viewagg_device is the same GPU-headroom
    # preflight, decided once for the whole run.
    passthrough = ['--fs_license', '/fs_license/license.txt', '--3T', '--threads', '4']
    if viewagg_device is not None:
        passthrough += ['--viewagg_device', viewagg_device]
    if os.getuid() == 0:
        passthrough.append('--allow_root')

    t1s = [f'/input/{tpid}.nii.gz' for tpid in tpids]
    long_driver = _fastsurfer_docker_command(
        '/fastsurfer/long_fastsurfer.sh', output_dir, input_dir=input_dir, gpu_index=gpu_index
    )
    run_fastsurfer = _fastsurfer_docker_command(
        '/fastsurfer/run_fastsurfer.sh', output_dir, input_dir=input_dir, gpu_index=gpu_index
    )

    # 1. Co-register every timepoint into an unbiased mid-space and build the
    #    person-specific template from them (mri_robust_template).
    _run_stage(
        f'building the person-specific template from {len(tpids)} timepoints',
        long_driver + ['--stage', 'prepare', '--tid', TEMPLATE_ID, '--sd', '/output',
                       '--tpids', *tpids, '--t1s', *t1s] + passthrough,
    )
    # 2./3. Segment and reconstruct the template itself. Everything the
    #    timepoints do afterwards is initialised from these.
    _run_stage(
        'segmenting the person-specific template',
        run_fastsurfer + ['--sid', TEMPLATE_ID, '--sd', '/output', '--base', '--seg_only']
        + passthrough,
    )
    _run_stage(
        'reconstructing surfaces for the person-specific template',
        run_fastsurfer + ['--sid', TEMPLATE_ID, '--sd', '/output', '--base', '--surf_only']
        + passthrough,
    )
    # 4./5. Segment and reconstruct each timepoint, initialised from the
    #    template -- the step that removes the run-to-run variability a
    #    cross-sectional pair of runs would carry.
    _run_stage(
        f'segmenting and reconstructing {len(tpids)} timepoints from the template',
        long_driver + ['--stage', 'long_seg', '--stage', 'long_surf',
                       '--tid', TEMPLATE_ID, '--sd', '/output', '--tpids', *tpids]
        + passthrough,
    )


def _map_segmentation_to_native(output_dir, tpid, gpu_index=None):
    """Map one timepoint's longitudinal segmentation out of the template's
    mid-space and back onto that timepoint's own acquisition geometry,
    returning the path written.

    Necessary because every longitudinal output lives in the template's space:
    long_prepare_template.sh resamples each timepoint through
    <tpid>_to_<tid>.lta before FastSurfer ever sees it, so the segmentation's
    vox2ras describes the mid-space, not the scanner RAS the study's DICOM
    series is in -- resampling it straight onto the T1 grid would land it in
    the wrong place by however much the rigid co-registration moved that
    timepoint. long_prepare_template.sh writes the inverse transform next to
    the forward one, so this is a single mri_convert with it (nearest
    neighbour, to preserve label values)."""
    transform = os.path.join(
        output_dir, TEMPLATE_ID, 'mri', 'transforms', f'{TEMPLATE_ID}_to_{tpid}.lta'
    )
    if not os.path.exists(transform):
        raise FileNotFoundError(
            f"Longitudinal run produced no template->timepoint transform at {transform}, "
            "so its segmentation cannot be mapped back onto the study's own geometry."
        )

    subject_dir = os.path.join(output_dir, tpid)
    seg_path, _ = _select_segmentation_and_stats(
        os.path.join(subject_dir, 'mri'), os.path.join(subject_dir, 'stats')
    )
    native_path = os.path.join(subject_dir, 'mri', 'native_space_aseg.nii.gz')

    # Paths are handed to the container in its own /output namespace.
    def in_container(path):
        return os.path.join('/output', os.path.relpath(path, output_dir))

    _run_stage(
        f'mapping {tpid} segmentation back to its acquisition geometry',
        _fastsurfer_docker_command(
            '/fastsurfer/tools/Docker/entrypoint.sh', output_dir, gpu_index=gpu_index
        ) + ['mri_convert', '-at', in_container(transform), '-rt', 'nearest',
             in_container(seg_path), in_container(native_path)],
    )
    return native_path


def _timepoint_tables(output_dir, tpid):
    """Volume and cortical-thickness tables for one processed timepoint, read
    the same way the cross-sectional pipeline reads them so the two produce
    directly comparable columns.

    include_renders=False: the longitudinal products are trajectories, not
    per-timepoint pial surface renders (those are what the cross-sectional
    button is for), so only the two mapped .stats files have to exist."""
    subject_dir = os.path.join(output_dir, tpid)
    stats_dir = os.path.join(subject_dir, 'stats')
    mri_dir = os.path.join(subject_dir, 'mri')

    _require_surface_outputs(
        subject_dir, stats_dir, os.path.join(subject_dir, 'surf'), include_renders=False
    )
    _, aseg_stats_path = _select_segmentation_and_stats(mri_dir, stats_dir)
    return {
        'volume_table': _hemisphere_volume_table(aseg_stats_path),
        'thickness_table': _cortical_thickness_table(stats_dir),
        'aseg_stats_path': aseg_stats_path,
    }


def _annual_percent_change(baseline, followup, years):
    """Change from baseline to follow-up as a percentage of baseline, divided
    by the interval -- the same linear annualisation siena_functions applies
    to PBVC, so the two pipelines' "%/year" mean the same thing. Elementwise
    over DataFrame columns; a zero or missing baseline yields NaN rather than
    a divide-by-zero."""
    with np.errstate(divide='ignore', invalid='ignore'):
        return np.where(baseline > 0, (followup - baseline) / baseline * 100 / years, np.nan)


def _trajectory_table(tables, timepoints, left_column, right_column):
    """One row per region, one column pair per timepoint, plus the annualised
    percentage change from the first timepoint to the last on each side.

    Inner-joined on Region across the timepoints on purpose: a region the
    parcellation only found at some of them has no trajectory, and carrying it
    with holes would put a blank cell in the middle of a change column that
    the reader has to work out the meaning of."""
    merged = None
    for table, timepoint in zip(tables, timepoints):
        columns = table[['Region', left_column, right_column]].rename(columns={
            left_column: f'{timepoint["label"]}_Left',
            right_column: f'{timepoint["label"]}_Right',
        })
        merged = columns if merged is None else merged.merge(columns, on='Region')

    baseline, last = timepoints[0], timepoints[-1]
    for side in ('Left', 'Right'):
        merged[f'Annual_%_{side}'] = _annual_percent_change(
            merged[f'{baseline["label"]}_{side}'],
            merged[f'{last["label"]}_{side}'],
            last['years'],
        )
    return merged.round(2)


def _annual_change_highlight(table, threshold):
    """Per-row booleans for upload_dataframe_as_sc()'s highlight_rows: flag a
    region whose annualised change passes the threshold on either side, in
    either direction (a ventricle enlarging is the same finding as the tissue
    around it shrinking). A NaN compares False, so a region missing on one
    side never lights up."""
    flagged = (table['Annual_%_Left'].abs() > threshold) | (table['Annual_%_Right'].abs() > threshold)
    return flagged.tolist()


def _highlight_note(threshold, quantity):
    """Caption drawn under the highlighted table. The rendered image is
    uploaded to PACS on its own, away from any other context, so it has to say
    what the colour means and how little it claims."""
    return (f'Highlighted rows: annualised {quantity} change beyond {threshold} %/year. '
            'A reporting convention for "worth a second look", not a diagnostic cut-off.')


def _timepoints_table(timepoints, role):
    """What was actually processed, as a table uploaded alongside the results.

    This is the run's provenance and its first QC step in one: which studies
    became timepoints, which series each contributed, and over what interval
    the change columns were measured. Worth a series of its own because the
    trajectory tables' column headers carry only dates -- and because a
    longitudinal number is only as good as the assumption that the same
    sequence was compared, which is exactly what the series-description column
    lets a reader check."""
    return pd.DataFrame({
        'Timepoint': [f'{i}{" (current study)" if t["is_current"] else ""}'
                      for i, t in enumerate(timepoints, start=1)],
        'Study date': [t['label'] for t in timepoints],
        'Years from baseline': [round(t['years'], 2) for t in timepoints],
        'Series': [t['series_description'] for t in timepoints],
        'Matched role': [role] * len(timepoints),
    })


def _short_interval_highlight(timepoints):
    """Per-row booleans flagging a timepoint that follows less than
    SHORT_INTERVAL_DAYS after the one before it. The baseline row is never
    flagged -- it has no preceding gap."""
    return [False] + [
        (later['date'] - earlier['date']).days < SHORT_INTERVAL_DAYS
        for earlier, later in zip(timepoints, timepoints[1:])
    ]


def _plotted_regions(table):
    """The PLOTTED_STRUCTURES this subject's parcellation actually has, in the
    order they are listed, so the renders stay readable instead of growing a
    line per region."""
    present = set(table['Region'])
    return [region for region in PLOTTED_STRUCTURES if region in present]


def _annual_change_figure(table, regions, quantity, unit):
    """Horizontal paired bars of annualised change per structure. Reads at a
    glance for any number of timepoints, including the two-timepoint case
    where a trajectory plot is just a straight line."""
    rows = table[table['Region'].isin(regions)].set_index('Region').loc[regions]
    positions = np.arange(len(regions))

    fig, ax = plt.subplots(figsize=(9, max(3.0, 0.5 * len(regions) + 1.5)))
    ax.barh(positions + 0.2, rows['Annual_%_Left'], height=0.38, label='Left', color='#3b6ea5')
    ax.barh(positions - 0.2, rows['Annual_%_Right'], height=0.38, label='Right', color='#a55b3b')
    ax.set_yticks(positions)
    ax.set_yticklabels(regions)
    ax.invert_yaxis()
    ax.axvline(0, color='black', linewidth=0.8)
    ax.set_xlabel(f'Annualised {quantity} change (%/year)')
    ax.set_title(f'FastSurfer longitudinal: annualised {quantity} change ({unit})')
    ax.legend(loc='lower right')
    ax.grid(axis='x', linestyle=':', linewidth=0.6)
    fig.tight_layout()
    return rasterize_figure(fig, mode='RGB')


def _trajectory_figure(table, timepoints, regions, quantity):
    """Percentage change from baseline against time, one line per structure
    and side. Plotted as change rather than as the raw value so structures
    that differ in size by two orders of magnitude share one axis, and so the
    baseline sits at 0 for all of them and the slopes can be compared."""
    rows = table[table['Region'].isin(regions)].set_index('Region').loc[regions]
    years = [timepoint['years'] for timepoint in timepoints]
    colors = plt.cm.tab10(np.linspace(0, 1, max(len(regions), 1), endpoint=False))

    fig, ax = plt.subplots(figsize=(9, 6))
    for region, color in zip(regions, colors):
        for side, style in (('Left', '-'), ('Right', '--')):
            values = np.array([rows.loc[region, f'{t["label"]}_{side}'] for t in timepoints],
                              dtype=float)
            if not values[0]:
                continue
            ax.plot(years, (values - values[0]) / values[0] * 100, style, marker='o',
                    color=color, markersize=4, label=f'{region} {side}')

    ax.axhline(0, color='black', linewidth=0.8)
    ax.set_xlabel('Years from baseline')
    ax.set_ylabel(f'{quantity} change from baseline (%)')
    ax.set_title(f'FastSurfer longitudinal: {quantity} trajectories '
                 f'({len(timepoints)} timepoints, solid = left, dashed = right)')
    ax.grid(linestyle=':', linewidth=0.6)
    ax.legend(fontsize=7, ncol=2, loc='best')
    fig.tight_layout()
    return rasterize_figure(fig, mode='RGB')


def fastsurfer_long_core(input_dir, output_dir, timepoints, gpu_index=None):
    """Run FastSurfer's longitudinal stream over already-staged local NIfTI
    inputs and parse its outputs into local-file products, without touching
    dcm4chee.

    input_dir must hold one '<tpid>.nii.gz' per entry of `timepoints`, which
    is the chronologically ordered timepoint list plan_longitudinal_fastsurfer
    produces -- only its 'tpid', 'label', 'years' and 'is_current' keys are
    read here, so this stays pure local compute. Returns a dict:
      - 'volume_trajectory' / 'thickness_trajectory': per-region tables with a
        column pair per timepoint plus annualised change on each side
      - 'renders': RGB uint8 arrays of the change and trajectory plots
      - 'current': the clicked study's timepoint entry merged with its
        per-timepoint tables, plus 'segmentation' (an array already resampled
        onto that study's own T1 grid) and its 'segmentation_labels' /
        'segmentation_values' -- those three absent when the mapping failed
      - 'segmentation_error': why they are absent, when they are

    The whole output tree is wiped first, for the reason
    fastsurfer_functions._clear_previous_subject_dir gives for the single
    subject dir -- with the addition that a repeat click may well cover a
    different set of timepoints (the patient acquired another study), which
    would leave the template and the timepoints describing different runs.
    """
    if os.path.exists(output_dir):
        log(f"Removing previous longitudinal FastSurfer output at {output_dir}")
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    tpids = [timepoint['tpid'] for timepoint in timepoints]
    gpu_index = choose_gpu_index(gpu_index)
    viewagg_device = _view_aggregation_device(
        [os.path.join(input_dir, f'{tpid}.nii.gz') for tpid in tpids], gpu_index
    )
    _run_longitudinal_pipeline(
        input_dir, output_dir, tpids, viewagg_device=viewagg_device, gpu_index=gpu_index
    )

    tables = [_timepoint_tables(output_dir, tpid) for tpid in tpids]
    volume_trajectory = _trajectory_table(
        [t['volume_table'] for t in tables], timepoints, 'Volume_mm3_Left', 'Volume_mm3_Right'
    )
    thickness_trajectory = _trajectory_table(
        [t['thickness_table'] for t in tables], timepoints, 'ThickAvg_Left', 'ThickAvg_Right'
    )

    regions = _plotted_regions(volume_trajectory)
    renders = []
    if regions:
        renders = [
            _annual_change_figure(volume_trajectory, regions, 'volume', 'mm3'),
            _trajectory_figure(volume_trajectory, timepoints, regions, 'Volume'),
        ]

    current_index = next(i for i, t in enumerate(timepoints) if t['is_current'])
    current = {**timepoints[current_index], **tables[current_index]}

    # Best-effort, unlike everything above it: the segmentation overlay is a
    # secondary product (the cross-sectional button already uploads one for
    # this study, and the trajectories are what this pipeline is for), and it
    # is the one step whose inputs come from a part of the output tree nothing
    # else reads. Losing a multi-hour run's tables over it would be the wrong
    # trade -- so it is reported instead, both here and in the job's message.
    segmentation_error = None
    try:
        native_seg_path = _map_segmentation_to_native(output_dir, current['tpid'], gpu_index)
        labels, values = _segmentation_labels_from_aseg_stats(current['aseg_stats_path'])
        current['segmentation'] = _resample_segmentation_to_t1_grid(
            native_seg_path, os.path.join(input_dir, f'{current["tpid"]}.nii.gz')
        )
        current['segmentation_labels'] = labels
        current['segmentation_values'] = values
    except Exception as error:  # noqa: BLE001 - reported, see comment above
        segmentation_error = str(error)
        log(f"Longitudinal FastSurfer: no segmentation overlay for the current study: {error}")

    return {
        'volume_trajectory': volume_trajectory,
        'thickness_trajectory': thickness_trajectory,
        'renders': renders,
        'current': current,
        'segmentation_error': segmentation_error,
    }


def fastsurfer_long_batch_stats(result):
    """Flat {metric_name: value} row for the batch pipeline's
    results_overview.csv (see routes.py's _run_fastsurfer_long_tool),
    mirroring fastsurfer_functions.fastsurfer_batch_stats: reuses the
    trajectory tables fastsurfer_long_core() already computed rather than
    recomputing anything.

    Only the annualised-change columns are kept, not each timepoint's own
    volume/thickness -- those columns' *count* varies with how many
    timepoints a case had (2 vs. 4), so they cannot fold into one uniform
    row the way every other batch tool's per-case stats do; the
    per-timepoint numbers are still in volume_trajectory.csv/
    thickness_trajectory.csv (see save_fastsurfer_long_core_outputs)."""
    stats = {}
    for _, row in result['volume_trajectory'].iterrows():
        region = row['Region']
        stats[f'{region}_Annual_Volume_%_Left'] = float(row['Annual_%_Left'])
        stats[f'{region}_Annual_Volume_%_Right'] = float(row['Annual_%_Right'])
    for _, row in result['thickness_trajectory'].iterrows():
        region = row['Region']
        stats[f'{region}_Annual_Thickness_%_Left'] = float(row['Annual_%_Left'])
        stats[f'{region}_Annual_Thickness_%_Right'] = float(row['Annual_%_Right'])
    return stats


### ---------------------------------------------------------------------- ###
### batch pipeline: write core outputs to a plain output tree (no dcm4chee) ###
### ---------------------------------------------------------------------- ###

def save_fastsurfer_long_core_outputs(result, plan, input_dir, out_dir):
    """Write fastsurfer_long_core()'s in-memory outputs to `out_dir` as plain
    files, for the batch pipeline's ZIP-in/ZIP-out route (POST /batch/run,
    when 'fastsurfer_long' is one of the selected tools). No DICOM SC/SEG
    here -- a batch case has no dcm4chee study to attach one to.

    Writes the same provenance/trajectory tables the interactive route
    uploads (_timepoints_table, volume/thickness trajectories), the change
    and trajectory plots already computed by fastsurfer_long_core (cheap
    next to the run itself, so always saved rather than recomputed), and
    the current timepoint's segmentation on its own T1 grid when the
    template->native mapping succeeded (see fastsurfer_long_core's
    'segmentation_error')."""
    os.makedirs(out_dir, exist_ok=True)
    current = result['current']

    _timepoints_table(plan['timepoints'], plan['role']).to_csv(
        os.path.join(out_dir, 'timepoints.csv'), index=False
    )
    result['volume_trajectory'].to_csv(os.path.join(out_dir, 'volume_trajectory.csv'), index=False)
    result['thickness_trajectory'].to_csv(os.path.join(out_dir, 'thickness_trajectory.csv'), index=False)

    if result['renders']:
        Image.fromarray(result['renders'][0]).save(os.path.join(out_dir, 'annual_change.png'))
        Image.fromarray(result['renders'][1]).save(os.path.join(out_dir, 'trajectory.png'))

    if 'segmentation' in current:
        t1_affine = nib.load(os.path.join(input_dir, f"{current['tpid']}.nii.gz")).affine
        seg_img = nib.Nifti1Image(current['segmentation'].astype(np.int16), t1_affine)
        nib.save(seg_img, os.path.join(out_dir, 'native_space_aseg.nii.gz'))
    elif result['segmentation_error']:
        with open(os.path.join(out_dir, 'segmentation_error.txt'), 'w') as f:
            f.write(result['segmentation_error'])


### ---------------------------------------------------------------------- ###
### wrapper: stage-in from dcm4chee -> core -> stage-out to dcm4chee        ###
### ---------------------------------------------------------------------- ###

def fastsurfer_long_wrapper(studyInstanceUID):
    """End-to-end longitudinal FastSurfer run for a patient, driven from one
    of their studies: pick the timepoints, download each one's T1, run the
    longitudinal stream, and upload the trajectories (plus the current
    timepoint's segmentation) into the clicked study.

    Results always land in `studyInstanceUID` -- the study the button was
    pressed on -- even though the run reaches back into the patient's earlier
    studies for its other timepoints, matching how siena_wrapper reports a
    two-timepoint comparison. This is what the
    /fastsurfer_long/<studyInstanceUID> route calls.
    """
    plan = plan_longitudinal_fastsurfer(studyInstanceUID)
    timepoints = plan['timepoints']

    study_dir = os.path.join(STORAGE_ROOT, studyInstanceUID)
    input_dir = os.path.join(study_dir, 'input')
    output_dir = os.path.join(study_dir, 'output')

    # stage-in: one T1 per timepoint, named by the subject id the pipeline
    # will use for it.
    for timepoint in timepoints:
        store_series_as_nifti(
            timepoint['uid'], timepoint['series_uid'],
            filename=os.path.join(input_dir, f'{timepoint["tpid"]}.nii.gz'),
        )

    # core
    result = fastsurfer_long_core(input_dir, output_dir, timepoints)
    current = result['current']

    # stage-out
    reference_instance = client.retrieve_series(
        study_instance_uid=studyInstanceUID, series_instance_uid=current['series_uid']
    )[0]

    upload_dataframe_as_sc(
        _timepoints_table(timepoints, plan['role']),
        title='FastSurfer Longitudinal: Timepoints Compared',
        study_uid=studyInstanceUID, reference_instance=reference_instance,
        series_description='FastSurfer Longitudinal Timepoints',
        series_number=_TIMEPOINTS_SERIES_NUMBER,
        highlight_rows=_short_interval_highlight(timepoints),
        highlight_note=(
            f'Highlighted rows: less than {SHORT_INTERVAL_DAYS} days after the previous '
            'timepoint, so the annualised change columns extrapolate a short interval.'
        ),
    )
    upload_dataframe_as_sc(
        result['volume_trajectory'],
        title='FastSurfer Longitudinal: Volume per Timepoint (mm3) + Annualised Change',
        study_uid=studyInstanceUID, reference_instance=reference_instance,
        series_description='FastSurfer Longitudinal Volume Trajectory',
        series_number=_VOLUME_SERIES_NUMBER,
        highlight_rows=_annual_change_highlight(
            result['volume_trajectory'], VOLUME_ANNUAL_HIGHLIGHT_PERCENT
        ),
        highlight_note=_highlight_note(VOLUME_ANNUAL_HIGHLIGHT_PERCENT, 'volume'),
    )
    upload_dataframe_as_sc(
        result['thickness_trajectory'],
        title='FastSurfer Longitudinal: Cortical Thickness per Timepoint (mm) + Annualised Change',
        study_uid=studyInstanceUID, reference_instance=reference_instance,
        series_description='FastSurfer Longitudinal Thickness Trajectory',
        series_number=_THICKNESS_SERIES_NUMBER,
        highlight_rows=_annual_change_highlight(
            result['thickness_trajectory'], THICKNESS_ANNUAL_HIGHLIGHT_PERCENT
        ),
        highlight_note=_highlight_note(THICKNESS_ANNUAL_HIGHLIGHT_PERCENT, 'thickness'),
    )

    if result['renders']:
        upload_images_as_sc(
            result['renders'], study_uid=studyInstanceUID,
            reference_instance=reference_instance,
            series_description='FastSurfer Longitudinal Change Plots',
            series_number=_RENDER_SERIES_NUMBER,
        )

    if 'segmentation' in current:
        upload_segmentation(
            current['segmentation'],
            labels=current['segmentation_labels'], values=current['segmentation_values'],
            study_uid=studyInstanceUID,
            series_uid=current['series_uid'],
            # Named for the stream it came out of, because it is not the same
            # map the cross-sectional button uploads for this study: it was
            # estimated from the person-specific template, so it is the
            # unbiased one to read the trajectory numbers against.
            seg_description='FastSurfer longitudinal whole-brain segmentation',
            seg_series_number=_SEG_SERIES_NUMBER,
        )

    interval = timepoints[-1]['years']
    summary = (
        f"Longitudinal FastSurfer completed for study {studyInstanceUID}: "
        f"{len(timepoints)} timepoints ({timepoints[0]['label']} -> {timepoints[-1]['label']}, "
        f"{interval:.2f} years) on '{timepoints[0]['series_description']}'. "
        "Timepoint, volume and thickness trajectory tables and change plots uploaded to PACS."
    )
    if result['segmentation_error']:
        summary += (
            " The segmentation overlay could not be mapped back onto this study's geometry "
            f"and was not uploaded: {result['segmentation_error']}"
        )
    return summary
