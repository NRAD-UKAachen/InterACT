"""
FSL SIENA / SIENAX brain atrophy measurement.

One button, two analyses: if the patient has a second MR study carrying the
same kind of structural series, this runs the longitudinal two-timepoint
SIENA (percentage brain volume change between the two scans); otherwise it
falls back to the single-timepoint SIENAX (normalised tissue volumes). Which
one applies is decided per patient by plan_siena_analysis(), not by the
caller -- see its docstring.

Both are FSL shell scripts (${FSLDIR}/bin/siena, ${FSLDIR}/bin/sienax) run
in-container, the same way hippocampus_functions.py runs run_first_all.
"""
import os
import re
import shutil
import subprocess
from datetime import datetime

import nibabel as nib
import numpy as np
import pandas as pd
from PIL import Image, ImageSequence
from nibabel.processing import resample_from_to

from .job_logging import run_logged
from .backend_helpers import (
    SERIES_CRITERIA, client, find_series, log, score_series_for_criteria,
    store_series_as_nifti, upload_dataframe_as_sc, upload_images_as_sc,
    upload_segmentation,
)
from .batch_io import classify_studies_for_patient, series_label
from .storage_paths import API_STORAGE_DIR

FSLDIR = os.environ.get("FSLDIR", "/usr/local/fsl")
FSL_BIN = os.path.join(FSLDIR, "bin")

STORAGE_ROOT = f'{API_STORAGE_DIR}/siena'

# Structural series SIENA/SIENAX can run on, in preference order. T1 MPRAGE
# first (what both tools are tuned for), a high-resolution T2 as fallback --
# in which case the '-t2' flag has to be passed, see _is_t2_role.
SIENA_ROLES = ('t1n_acquired', 'flair')

# Options handed to BET inside siena/sienax (their own '-B "betopts"' flag).
#
# '-B' here is *BET's* -B, "bias field & neck cleanup", which BET's usage text
# itself marks as "can be useful in SIENA" -- not to be confused with the
# siena/sienax -B that carries it. It is the default because plain BET
# over-includes badly on this project's MPRAGE protocol, which images well
# down into the neck: on the first real longitudinal pair run through here it
# kept 1387 mL for the follow-up and 1291 mL for the baseline, both inflated
# with skull base and neck, and -- worse for SIENA -- inflated by *different*
# amounts at the two timepoints, which is exactly the asymmetry that turns
# into a spurious PBVC. With '-B' the same follow-up comes out at 1113 mL and
# the mask stops cleanly at the skull base.
#
# '-f 0.3' lowers BET's intensity threshold from its 0.5 default. '-B' alone
# is not enough on this protocol: these MPRAGEs carry a strong receive-coil
# gradient, with the anterior quarter of the brain sitting at 0.72x (baseline)
# and 0.69x (follow-up) the intensity of the posterior quarter. BET grows its
# surface against one global threshold, t2 + f*(t98-t2), so a frontal lobe
# that far below the rest of the brain falls under it and the surface never
# reaches the frontal cortex. '-B' is meant to cover exactly this -- it runs
# `fast -b` and divides the bias field out -- but on this data it only
# recovers part of the gradient (anterior/posterior 0.72 -> 0.82 and
# 0.69 -> 0.80), leaving enough residual to still lose the frontal pole. The
# large FOV compounds it: bright neck and scalp fat raise t98, which raises
# the absolute threshold again.
#
# Measured on study 1.2.840.113654.2.70.1.702969623196229657728778399343702-
# 67341, brain mask volume at the two timepoints:
#
#   -B          1188 / 1269 mL   frontal cortex clipped, 81 mL apart
#   -B -f 0.4   1283 / 1291 mL   still trims frontal cortex
#   -B -f 0.3   1365 / 1372 mL   frontal cortex covered, 7 mL apart
#   -B -f 0.2   1426 / 1457 mL   dura creeping back in, 30 mL apart
#
# The gap between the timepoints is the number that matters here, not the
# absolute volume: SIENA intersects the two brain masks, so a frontal lobe
# lost by different amounts at the two timepoints drops out of the valid mask
# and biases the PBVC. At '-B' that run reported directional PBVCs of +0.816
# and -1.065 (asymmetry 0.249, over the 0.2 CHECK RUN threshold in
# _siena_table) with siena_cal correction factors 27% apart.
#
# Other options worth reaching for via the route's ?bet= override: '-f <frac>'
# (lower = larger brain estimate), '-S' (eye & optic nerve cleanup, also
# flagged useful for SIENA), '-Z' (small FOV in z).
DEFAULT_BET_OPTIONS = '-B -f 0.3'

_TABLE_SERIES_NUMBER = 5100
_RENDER_SERIES_NUMBER = 5101
_REGISTRATION_QC_SERIES_NUMBER = 5102
_SEG_SERIES_NUMBER = 5103

# FAST's tissue classes as SIENAX consumes them -- sienax itself reads
# pve_0 as CSF and pve_1 as grey (see its regional '-r' masking step), so
# the discrete _seg map is 1=CSF, 2=GM, 3=WM in that same order.
SIENAX_TISSUE_LABELS = {'CSF': 1, 'Grey Matter': 2, 'White Matter': 3}

# Annualised whole-brain atrophy rates from the general literature: healthy
# adults lose roughly 0.2-0.5 %/year, and rates beyond about -0.8 %/year are
# what the MS/dementia literature treats as pathological. This is a coarse,
# age-unadjusted band used only to flag a result as worth a second look --
# SIENA's own accuracy is around 0.2 %, and a poor brain extraction or a
# protocol change between the two scans can move PBVC by more than the
# effect being measured, so the flag is a QC prompt and not a clinical read.
_NORMAL_ANNUAL_PBVC_RANGE = (-0.5, 0.0)
_PATHOLOGICAL_ANNUAL_PBVC = -0.8


def _flag_annual_pbvc(annual_pbvc):
    if annual_pbvc is None:
        return ''
    if annual_pbvc <= _PATHOLOGICAL_ANNUAL_PBVC:
        return 'ACCELERATED ATROPHY'
    low, high = _NORMAL_ANNUAL_PBVC_RANGE
    if annual_pbvc < low:
        return 'above normal ageing'
    if annual_pbvc > high:
        return 'volume increase - check registration'
    return 'within normal ageing'


def _is_t2_role(role):
    return role == 'flair'


### ---------------------------------------------------------------------- ###
### deciding what to run: patient's studies -> SIENA pair or SIENAX single  ###
### ---------------------------------------------------------------------- ###

def _study_attribute(study, tag, default=''):
    return study.get(tag, {}).get('Value', [default])[0] or default


def _parse_study_date(value):
    """DICOM StudyDate (YYYYMMDD) as a date, or None if absent/unparseable.
    Studies without a usable date can still be analysed on their own, but
    cannot take part in a SIENA pair -- PBVC's sign is defined by which scan
    came first, and annualising it needs the interval."""
    try:
        return datetime.strptime(value, '%Y%m%d').date()
    except (TypeError, ValueError):
        return None


def _study_record(study):
    return {
        'uid': _study_attribute(study, '0020000D'),
        'date': _parse_study_date(_study_attribute(study, '00080020')),
        'description': _study_attribute(study, '00081030'),
    }


def _current_study(studyInstanceUID):
    """The clicked study's own record plus its PatientID, which is what the
    search for sibling studies keys off."""
    results = client.search_for_studies(search_filters={'StudyInstanceUID': studyInstanceUID})
    if not results:
        raise ValueError(f"Study {studyInstanceUID} was not found in PACS.")
    return _study_record(results[0]), _study_attribute(results[0], '00100020')


def _patient_studies(patient_id):
    """Every study of this patient that could hold an MR series, unordered.
    Studies whose ModalitiesInStudy says they have no MR at all are dropped
    here so the per-study series query is not run for them; when a server
    omits that tag the study is kept and the series-role matching (which
    requires modality MR) rules it out instead."""
    studies = []
    for study in client.search_for_studies(search_filters={'PatientID': patient_id}):
        modalities = study.get('00080061', {}).get('Value', [])
        if modalities and 'MR' not in modalities:
            continue
        studies.append(_study_record(study))
    return studies


def _best_series_for_roles(studyInstanceUID, roles):
    """Best-matching series per role for one study, scored against the shared
    SERIES_CRITERIA. Returns {role: (seriesInstanceUID, seriesDescription)},
    omitting roles this study has nothing for.

    Unlike find_required_seriesUIDs this scores every role off a single
    series query and does not treat a missing role as a failure -- SIENA has
    to probe each of the patient's studies for each candidate role before it
    knows which sequence the two timepoints can share."""
    _, series_uids, modalities, descriptions = find_series(studyInstanceUID)

    matches = {}
    for role in roles:
        criteria = SERIES_CRITERIA[role]
        best_index, best_score = None, -1
        for i in range(len(series_uids)):
            score = score_series_for_criteria(modalities[i], descriptions[i], criteria)
            if score is not None and score > best_score:
                best_index, best_score = i, score
        if best_index is not None:
            matches[role] = (series_uids[best_index], descriptions[best_index])
    return matches


def plan_siena_analysis(studyInstanceUID):
    """Decide whether this study gets longitudinal SIENA or cross-sectional
    SIENAX, and on which series.

    The rules, in order:

    1. A role is only usable longitudinally if *both* timepoints have it --
       FSL's guide is explicit that the two scans must come from the same
       sequence, since SIENA measures a sub-voxel edge shift and a protocol
       change moves that edge by more than the atrophy being looked for.
       Matching by role (rather than by SeriesInstanceUID or description
       string) is what enforces "same sequence" across studies here.
    2. Longitudinal beats sequence preference: a T2 pair is chosen over a
       lone T1, because SIENA measures change directly while SIENAX's
       cross-sectional volume has to be read against population norms.
    3. Within that, T1 MPRAGE beats high-resolution T2 (SIENA_ROLES order).
    4. The clicked study is always one of the two timepoints; its partner is
       the patient's temporally furthest other study carrying the same role,
       which maximises the interval and so the atrophy-to-noise ratio. The
       pair is then ordered oldest-first, since PBVC's sign is only
       meaningful that way (negative = atrophy from baseline to follow-up).

    Returns a dict with 'mode' ('siena' or 'sienax'), 'role', 'is_t2' and
    'timepoints' -- a list of one (SIENAX) or two (SIENA, oldest first)
    {'uid', 'series_uid', 'series_description', 'date'} entries.
    """
    current, patient_id = _current_study(studyInstanceUID)
    studies = [s for s in (_patient_studies(patient_id) if patient_id else [])
               if s['uid'] != studyInstanceUID]
    studies.append(current)

    roles_by_study = {s['uid']: _best_series_for_roles(s['uid'], SIENA_ROLES) for s in studies}
    current_roles = roles_by_study[studyInstanceUID]

    def timepoint(study, role):
        series_uid, series_description = roles_by_study[study['uid']][role]
        return {
            'uid': study['uid'],
            'series_uid': series_uid,
            'series_description': series_description,
            'date': study['date'],
        }

    for role in SIENA_ROLES:
        if role not in current_roles or current['date'] is None:
            continue
        # A same-day sibling study is excluded rather than compared against:
        # its PBVC would be a scan-rescan reproducibility figure, not the
        # atrophy over time this is reporting, and annualising it divides by
        # a zero interval.
        partners = [
            s for s in studies
            if s['uid'] != studyInstanceUID and s['date'] and s['date'] != current['date']
            and role in roles_by_study[s['uid']]
        ]
        if not partners:
            continue
        partner = max(partners, key=lambda s: abs((s['date'] - current['date']).days))
        pair = sorted([timepoint(current, role), timepoint(partner, role)], key=lambda t: t['date'])
        log(
            f"SIENA: patient {patient_id} has {len(partners) + 1} studies with a '{role}' series; "
            f"comparing {pair[0]['date']} -> {pair[1]['date']}"
        )
        return {'mode': 'siena', 'role': role, 'is_t2': _is_t2_role(role), 'timepoints': pair}

    for role in SIENA_ROLES:
        if role in current_roles:
            log(f"SIENAX: no second study with a matching '{role}' series for patient {patient_id}")
            return {
                'mode': 'sienax', 'role': role, 'is_t2': _is_t2_role(role),
                'timepoints': [timepoint(current, role)],
            }

    raise ValueError(
        f"Study {studyInstanceUID} has no T1 MPRAGE and no high-resolution T2 series "
        "for SIENA/SIENAX to run on."
    )


def plan_siena_analysis_batch(root_dir, patient_id, study_name):
    """plan_siena_analysis's counterpart for the batch pipeline: decide SIENA
    vs. SIENAX from the *other study folders this patient already has in the
    uploaded batch cohort*, instead of the patient's PACS history -- the
    batch tree has no dcm4chee study behind it to query. Same rules (see
    plan_siena_analysis's docstring), applied to
    batch_io.classify_studies_for_patient's local role/date resolution. Kept
    as a separate function rather than a shared helper so the interactive,
    dcm4chee-backed route is untouched by batch-only changes.

    `study_name` is the batch tree's folder name for the case this is being
    planned for (routes.py's _run_siena_tool), playing the role
    studyInstanceUID plays above.

    Returns the same {'mode', 'role', 'is_t2', 'timepoints'} shape as
    plan_siena_analysis, except each timepoint dict carries 'study_name' +
    'candidate' (a batch_io.classify_series() dict, for
    batch_io.materialize_timepoint_inputs) + 'tpid' ('baseline'/'followup'
    for SIENA, 'current' for SIENAX -- the destination filename stem
    materialize_timepoint_inputs uses) instead of 'uid'/'series_uid'.
    """
    studies = classify_studies_for_patient(root_dir, patient_id, SIENA_ROLES, 'MR')
    current = next((s for s in studies if s['study_name'] == study_name), None)
    if current is None:
        raise ValueError(f"Study '{study_name}' not found in this batch cohort for patient '{patient_id}'.")

    def timepoint(study, role, tpid):
        candidate = study['role_map'][role]
        return {
            'study_name': study['study_name'], 'date': study['date'], 'candidate': candidate,
            'series_description': series_label(candidate), 'tpid': tpid,
        }

    for role in SIENA_ROLES:
        if role not in current['role_map'] or current['date'] is None:
            continue
        partners = [
            s for s in studies
            if s['study_name'] != study_name and s['date'] and s['date'] != current['date']
            and role in s['role_map']
        ]
        if not partners:
            continue
        partner = max(partners, key=lambda s: abs((s['date'] - current['date']).days))
        # Sort oldest-first before stamping 'tpid': whichever of the pair is
        # actually earlier becomes the baseline, regardless of whether that
        # is the clicked study or its partner -- same ordering guarantee as
        # plan_siena_analysis.
        pair = sorted(
            [timepoint(current, role, None), timepoint(partner, role, None)], key=lambda t: t['date']
        )
        pair[0]['tpid'], pair[1]['tpid'] = 'baseline', 'followup'
        log(
            f"Batch SIENA: patient '{patient_id}' has {len(partners) + 1} studies with a "
            f"'{role}' series in this cohort; comparing {pair[0]['date']} -> {pair[1]['date']}"
        )
        return {'mode': 'siena', 'role': role, 'is_t2': _is_t2_role(role), 'timepoints': pair}

    for role in SIENA_ROLES:
        if role in current['role_map']:
            log(
                f"Batch SIENAX: no second study with a matching '{role}' series for patient "
                f"'{patient_id}' in this cohort"
            )
            return {
                'mode': 'sienax', 'role': role, 'is_t2': _is_t2_role(role),
                'timepoints': [timepoint(current, role, 'current')],
            }

    # 't1n_acquired' rejects MPR reformats on purpose (see SERIES_CRITERIA),
    # so a study holding only reformats would otherwise read as having "no T1
    # MPRAGE" at all -- name them, so the skip says what to add instead.
    reformats = [
        name for name in current['series_found']
        if 't1' in name.lower() and 'mpr_mpr' in name.lower()
    ]
    reformat_note = (
        f" Its T1 MPRAGE series {reformats} are MPR reformats, which SIENA does not use "
        "(they make BET clip cortex); add the original acquisition to the cohort."
        if reformats else ""
    )
    raise ValueError(
        f"Study '{study_name}' has no T1 MPRAGE and no high-resolution T2 series in this "
        f"cohort for SIENA/SIENAX to run on.{reformat_note}"
    )


### ---------------------------------------------------------------------- ###
### pure local-file compute -- no dcm4chee access below this point          ###
### ---------------------------------------------------------------------- ###

def _require_fsl_arithmetic():
    """siena and sienax are shell scripts that do every calculation by piping
    into `dc` -- the tissue volumes, the V-scaling factor and the final PBVC
    all come out of it. Without `dc` the shell substitutes an empty string
    for each of them and both scripts still exit 0, so the run looks
    successful while report.sienax/report.siena come back with blank value
    columns. Refusing up front turns that into a message instead of an hour
    of processing for an empty report. Same failure mode, and same fix, as
    XTRACT's `bc` dependency in dti_functions._run_xtract."""
    if not shutil.which("dc"):
        raise RuntimeError(
            "FSL SIENA/SIENAX need `dc` for their arithmetic, and it is not "
            "installed in this container. Without it every volume and the PBVC "
            "estimate come out blank. Install it (see API/Dockerfile) and run "
            "this again."
        )


def _run_fsl(cmd, timeout):
    env = {**os.environ, "FSLDIR": FSLDIR, "FSLOUTPUTTYPE": "NIFTI_GZ"}
    result = run_logged(cmd, timeout=timeout, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"{os.path.basename(cmd[0])} failed:\n{result.stdout}\n{result.stderr}")
    return result


def _reorient_to_std(input_image, output_image):
    """Losslessly permute/flip onto FSL's standard axis order. Both tools
    register to MNI152 with pairreg, which -- like every other flirt-based
    step in this project -- can fail outright on a sagittally-acquired
    series whose NIfTI axes are not in standard order; see
    hippocampus_functions._reorient_to_std for the full writeup of that
    failure mode."""
    _run_fsl([os.path.join(FSL_BIN, "fslreorient2std"), input_image, output_image], timeout=120)


def _load_render_images(*paths):
    """Read the PNG/GIF QC renders siena and sienax produce themselves (via
    slicer/pngappend) as RGB arrays for upload_images_as_sc.

    These are taken as-is rather than re-rendered from the overlay NIfTIs:
    'overlay' encodes its colour maps into a single-channel image that only
    means anything under FSL's own lookup tables, so the PNGs FSL already
    wrote are the only faithful rendering of them. Missing files are skipped
    -- which renders exist depends on the options a run used."""
    images = []
    for path in paths:
        if not os.path.exists(path):
            continue
        with Image.open(path) as image:
            images.extend(
                np.array(frame.convert('RGB')) for frame in ImageSequence.Iterator(image)
            )
    return images


# One number as siena/sienax's `dc` writes it -- note it emits a leading
# bare '.' for values below 1 (e.g. 'finalPBVC -.868 %'), so the integer part
# is optional.
_NUMBER = r'(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)'


def _search_float(text, pattern, group=1):
    match = re.search(pattern, text, re.MULTILINE)
    return float(match.group(group)) if match else None


def _mm3_to_ml(volume_mm3):
    return round(volume_mm3 / 1000.0, 1) if volume_mm3 is not None else None


def parse_sienax_report(report_path):
    """Pull the volumes out of report.sienax.

    sienax writes one line per tissue as '<TISSUE> <normalised> <unnormalised>'
    in mm3, plus the 'VSCALING <factor>' line from the skull-constrained
    registration to MNI152. The normalised column is the volume after scaling
    by that factor, i.e. corrected for head size, and is the number to compare
    across subjects; the unnormalised column is this brain's actual volume.
    'pgrey' and 'vcsf' only appear when sienax ran with '-r'.
    """
    with open(report_path) as report:
        text = report.read()

    volumes = {'vscaling': _search_float(text, rf'^VSCALING\s+{_NUMBER}')}
    for key, line_label in (
        ('grey', 'GREY'), ('white', 'WHITE'), ('brain', 'BRAIN'),
        ('peripheral_grey', 'pgrey'), ('ventricular_csf', 'vcsf'),
    ):
        pattern = rf'^{line_label}\s+{_NUMBER}\s+{_NUMBER}'
        volumes[f'{key}_normalised_mm3'] = _search_float(text, pattern, group=1)
        volumes[f'{key}_unnormalised_mm3'] = _search_float(text, pattern, group=2)
    return volumes


def parse_siena_report(report_path):
    """Pull the atrophy estimate out of report.siena.

    siena runs siena_diff in both directions and writes the mean of the two
    as 'finalPBVC <value> %'. Each direction's own 'PBVC <value>' line is
    kept too: the two should be near-equal and opposite, so how far they
    disagree is a free reliability check on the run (a large asymmetry means
    the registration or brain extraction differed between the timepoints).
    """
    with open(report_path) as report:
        text = report.read()
    directional = [float(v) for v in re.findall(rf'^PBVC\s+{_NUMBER}', text, re.MULTILINE)]
    asymmetry = (
        round(abs(directional[0] + directional[1]), 3) if len(directional) >= 2 else None
    )
    return {
        'pbvc': _search_float(text, rf'^finalPBVC\s+{_NUMBER}'),
        'directional_pbvc': directional,
        'pbvc_asymmetry': asymmetry,
        # siena_cal's self-calibration factor, from the 'corr1=.. corr2=.. corr=..'
        # line. See _check_siena_calibration for why this is read back.
        'calibration': _search_float(text, rf'^corr1=\S*\s+corr2=\S*\s+corr={_NUMBER}'),
    }


def _check_siena_calibration(result, report_path):
    """Refuse a run whose self-calibration step produced nothing.

    siena runs siena_cal first to derive a correction factor, then passes it
    to both siena_diff passes as '-c <corr>'. When siena_cal fails it prints
    nothing, the shell expands '-c' with an empty argument, and siena_diff
    goes on to report a PBVC of exactly 0 -- so the run completes, exits 0,
    and the report reads as "no brain volume change at all" rather than as a
    failure. That is the worst possible way for this particular tool to
    break, so an absent correction factor is treated as an error here.
    """
    if result['calibration'] is None:
        raise RuntimeError(
            "SIENA's self-calibration (siena_cal) produced no correction factor, so "
            "its PBVC estimate is uncalibrated and reads as exactly 0% regardless of "
            f"the real change. See report.siena at {report_path}."
        )


def sienax_core(nifti_path, output_dir, is_t2=False, bet_options=DEFAULT_BET_OPTIONS):
    """Run FSL SIENAX on an already-staged structural NIfTI, without touching
    dcm4chee.

    Runs with '-r' for the regional peripheral-cortex-grey and ventricular-CSF
    volumes, and with '-d' so the FAST tissue segmentation survives -- sienax
    deletes its '<input>_stdmask*' intermediates otherwise, and that map is
    what gets uploaded as the DICOM SEG.

    Returns a dict with the parsed volumes, the volume table, the tissue
    segmentation on the source NIfTI's own grid, and sienax's QC renders.
    """
    _require_fsl_arithmetic()
    os.makedirs(output_dir, exist_ok=True)

    reoriented_path = os.path.join(output_dir, 'input_std.nii.gz')
    _reorient_to_std(nifti_path, reoriented_path)

    # sienax does `mkdir -p` and copies its input in as 'I', so a leftover
    # tree from an earlier run would be read back as this run's output.
    sienax_dir = os.path.join(output_dir, 'sienax')
    shutil.rmtree(sienax_dir, ignore_errors=True)

    cmd = [os.path.join(FSL_BIN, 'sienax'), reoriented_path, '-o', sienax_dir, '-r', '-d']
    if is_t2:
        cmd.append('-t2')
    if bet_options:
        cmd += ['-B', bet_options]
    _run_fsl(cmd, timeout=7200)

    report_path = os.path.join(sienax_dir, 'report.sienax')
    if not os.path.exists(report_path):
        raise RuntimeError(f"SIENAX produced no report at {report_path}")
    volumes = parse_sienax_report(report_path)
    if volumes['brain_normalised_mm3'] is None:
        raise RuntimeError(
            "SIENAX finished without producing a brain volume -- see report.sienax "
            f"at {report_path} (usually a failed brain extraction or a failed "
            "skull-constrained registration to MNI152)."
        )

    segmentation = _sienax_segmentation_on_native_grid(sienax_dir, nifti_path)

    return {
        'mode': 'sienax',
        'volumes': volumes,
        'volume_table': _sienax_table(volumes),
        'segmentation': segmentation,
        'output_dir': sienax_dir,
        'report_path': report_path,
        'render_images': _load_render_images(
            os.path.join(sienax_dir, 'I_render.png'),
            os.path.join(sienax_dir, 'I_bet.png'),
            os.path.join(sienax_dir, 'I2std.png'),
        ),
    }


def _sienax_segmentation_on_native_grid(sienax_dir, native_path):
    """FAST's CSF/GM/WM map, mapped back from the reoriented volume sienax
    ran on onto the source series' own grid so upload_segmentation can assign
    it slice-by-slice to that series' DICOM instances. Nearest-neighbour, to
    keep it a clean 3-value label map rather than blending classes at tissue
    boundaries."""
    seg_path = os.path.join(sienax_dir, 'I_stdmaskbrain_seg.nii.gz')
    if not os.path.exists(seg_path):
        raise RuntimeError(f"SIENAX produced no tissue segmentation at {seg_path}")

    seg_img = nib.load(seg_path)
    # sienax's own output sits on the reoriented grid, not the native one;
    # resample_from_to needs both in the same world space, which they are.
    native_img = nib.load(native_path)
    resampled = resample_from_to(seg_img, native_img, order=0)
    return np.asanyarray(resampled.dataobj).astype(np.uint8)


def _sienax_table(volumes):
    """Normalised (head-size corrected) and actual volumes per tissue class,
    in mL, plus the V-scaling factor those normalised volumes were derived
    with.

    Normalised is the column to compare against other subjects or a
    population norm -- it is the measured volume scaled to a standard skull
    size, so it does not depend on how big this patient's head is.
    Unnormalised is this brain as actually measured. V-scaling is carried
    alongside because it is the QC handle on the pair: a factor far from ~1
    means the skull-constrained registration to MNI152 went wrong, and every
    normalised volume above it is wrong by the same factor.
    """
    rows = [
        ('Whole brain', 'brain'),
        ('Grey matter', 'grey'),
        ('White matter', 'white'),
        ('Peripheral cortical grey', 'peripheral_grey'),
        ('Ventricular CSF', 'ventricular_csf'),
    ]
    vscaling = volumes['vscaling']
    return pd.DataFrame({
        'Measure': [label for label, _ in rows] + ['V-scaling factor (skull-based)'],
        'Normalised': [
            f'{_mm3_to_ml(volumes[f"{key}_normalised_mm3"])} mL' for _, key in rows
        ] + [f'{vscaling:.4f} x' if vscaling else 'n/a'],
        'As measured': [
            f'{_mm3_to_ml(volumes[f"{key}_unnormalised_mm3"])} mL' for _, key in rows
        ] + [''],
    })


def siena_core(baseline_nifti, followup_nifti, output_dir, is_t2=False,
               bet_options=DEFAULT_BET_OPTIONS):
    """Run FSL SIENA between two already-staged structural NIfTIs, without
    touching dcm4chee. `baseline_nifti` must be the earlier scan: SIENA
    reports the change from the first input to the second, so swapping them
    flips the sign of the atrophy estimate.

    Returns a dict with the parsed PBVC, the report table and SIENA's own
    edge-motion and registration QC renders.
    """
    _require_fsl_arithmetic()
    os.makedirs(output_dir, exist_ok=True)

    baseline_std = os.path.join(output_dir, 'baseline_std.nii.gz')
    followup_std = os.path.join(output_dir, 'followup_std.nii.gz')
    _reorient_to_std(baseline_nifti, baseline_std)
    _reorient_to_std(followup_nifti, followup_std)

    siena_dir = os.path.join(output_dir, 'siena')
    shutil.rmtree(siena_dir, ignore_errors=True)

    cmd = [os.path.join(FSL_BIN, 'siena'), baseline_std, followup_std, '-o', siena_dir]
    if is_t2:
        cmd.append('-t2')
    if bet_options:
        cmd += ['-B', bet_options]
    _run_fsl(cmd, timeout=14400)

    report_path = os.path.join(siena_dir, 'report.siena')
    if not os.path.exists(report_path):
        raise RuntimeError(f"SIENA produced no report at {report_path}")
    result = parse_siena_report(report_path)
    if result['pbvc'] is None:
        raise RuntimeError(
            "SIENA finished without producing a PBVC estimate -- see report.siena "
            f"at {report_path} (usually a failed brain extraction or registration)."
        )
    _check_siena_calibration(result, report_path)

    return {
        'mode': 'siena',
        **result,
        'output_dir': siena_dir,
        'report_path': report_path,
        # siena copies its inputs in as 'A' and 'B' regardless of their
        # filenames, so its renders always carry these fixed names.
        'render_images': _load_render_images(os.path.join(siena_dir, 'A_halfwayto_B_render.png')),
        'registration_images': _load_render_images(
            os.path.join(siena_dir, 'A_and_B.gif'),
            os.path.join(siena_dir, 'A_halfwayto_B_brain_seg.png'),
            os.path.join(siena_dir, 'B_halfwayto_A_brain_seg.png'),
        ),
    }


def _siena_table(result, plan):
    """PBVC with the interval it was measured over, annualised, plus the
    forward/backward disagreement as a reliability check."""
    baseline, followup = plan['timepoints']
    years = (followup['date'] - baseline['date']).days / 365.25
    annual_pbvc = round(result['pbvc'] / years, 3) if years else None

    low, high = _NORMAL_ANNUAL_PBVC_RANGE
    return pd.DataFrame({
        'Metric': [
            'Percentage brain volume change (PBVC, %)',
            'Annualised PBVC (%/year)',
            'Interval between scans (years)',
            'Forward/backward PBVC disagreement (%)',
            'Baseline scan',
            'Follow-up scan',
            'Sequence compared',
        ],
        'Value': [
            round(result['pbvc'], 3),
            annual_pbvc,
            round(years, 2),
            result['pbvc_asymmetry'],
            str(baseline['date']),
            str(followup['date']),
            baseline['series_description'],
        ],
        'Reference': [
            'negative = atrophy',
            f'{low} to {high} in normal ageing',
            '',
            'below 0.2 expected',
            '', '', '',
        ],
        'Flag': [
            '',
            _flag_annual_pbvc(annual_pbvc),
            '',
            'CHECK RUN' if (result['pbvc_asymmetry'] or 0) > 0.2 else '',
            '', '', '',
        ],
    })


def siena_batch_stats(result, plan):
    """Flat {metric: value} row for the batch pipeline's results_overview.csv
    (see routes.py's _run_siena_tool, longitudinal case), mirroring
    fastsurfer_functions.fastsurfer_batch_stats -- reuses the numbers
    _siena_table() already computed rather than recomputing anything."""
    baseline, followup = plan['timepoints']
    years = (followup['date'] - baseline['date']).days / 365.25
    return {
        'PBVC_%': result['pbvc'],
        'Annual_PBVC_%_per_year': round(result['pbvc'] / years, 3) if years else None,
        'PBVC_Asymmetry': result['pbvc_asymmetry'],
        'Interval_Years': round(years, 2),
        'Baseline_Date': str(baseline['date']),
        'Followup_Date': str(followup['date']),
    }


def sienax_batch_stats(result):
    """Flat {metric: value} row for the batch pipeline's results_overview.csv
    (see routes.py's _run_siena_tool, cross-sectional fallback), mirroring
    siena_batch_stats -- reuses sienax_core()'s parsed volumes as-is."""
    volumes = result['volumes']
    return {
        'Brain_Volume_mL_Normalised': _mm3_to_ml(volumes['brain_normalised_mm3']),
        'Grey_Matter_mL_Normalised': _mm3_to_ml(volumes['grey_normalised_mm3']),
        'White_Matter_mL_Normalised': _mm3_to_ml(volumes['white_normalised_mm3']),
        'Peripheral_Grey_mL_Normalised': _mm3_to_ml(volumes['peripheral_grey_normalised_mm3']),
        'Ventricular_CSF_mL_Normalised': _mm3_to_ml(volumes['ventricular_csf_normalised_mm3']),
        'V_Scaling_Factor': volumes['vscaling'],
    }


### ---------------------------------------------------------------------- ###
### batch pipeline: write core outputs to a plain output tree (no dcm4chee) ###
### ---------------------------------------------------------------------- ###

def save_siena_core_outputs(result, plan, out_dir):
    """Write siena_core()'s outputs to `out_dir` as plain files, for the
    batch pipeline's ZIP-in/ZIP-out route (POST /batch/run, when 'siena' is
    selected and the patient has a longitudinal partner in the cohort). No
    DICOM SC here -- a batch case has no dcm4chee study to attach one to."""
    os.makedirs(out_dir, exist_ok=True)
    _siena_table(result, plan).to_csv(os.path.join(out_dir, 'siena_report.csv'), index=False)
    shutil.copyfile(result['report_path'], os.path.join(out_dir, 'report.siena'))
    for name, images in (
        ('brain_edge_motion', result['render_images']),
        ('registration_qc', result['registration_images']),
    ):
        for i, image in enumerate(images):
            Image.fromarray(image).save(os.path.join(out_dir, f'{name}_{i}.png'))


def save_sienax_core_outputs(result, nifti_path, out_dir):
    """Write sienax_core()'s outputs to `out_dir` as plain files, for the
    batch pipeline (POST /batch/run, when 'siena' is selected and the
    patient has no longitudinal partner in the cohort -- the cross-sectional
    fallback). No DICOM SEG/SC here -- a batch case has no dcm4chee study to
    attach one to. `nifti_path` is the source series sienax_core() ran on,
    needed to recover the affine its own segmentation array (already
    resampled onto that grid, see _sienax_segmentation_on_native_grid) has
    lost by the time it gets here."""
    os.makedirs(out_dir, exist_ok=True)
    affine = nib.load(nifti_path).affine
    seg_img = nib.Nifti1Image(result['segmentation'].astype(np.uint8), affine)
    nib.save(seg_img, os.path.join(out_dir, 'tissue_segmentation.nii.gz'))

    result['volume_table'].to_csv(os.path.join(out_dir, 'volume_table.csv'), index=False)
    shutil.copyfile(result['report_path'], os.path.join(out_dir, 'report.sienax'))
    for i, image in enumerate(result['render_images']):
        Image.fromarray(image).save(os.path.join(out_dir, f'qc_{i}.png'))


### ---------------------------------------------------------------------- ###
### wrapper: stage-in from dcm4chee -> core -> stage-out to dcm4chee        ###
### ---------------------------------------------------------------------- ###

_ALLOWED_BET_FLAGS = {'-B', '-S', '-R', '-Z', '-n'}


def validate_bet_options(bet_options):
    """Whitelist the BET options a request may override DEFAULT_BET_OPTIONS
    with, so the route can expose brain-extraction tuning (the one knob FSL's
    SIENA guide says actually matters) without letting a URL put arbitrary
    arguments in front of a subprocess. Accepts the cleanup/robustness flags
    plus '-f <fraction>' and '-g <gradient>' with numeric arguments.

    Returns the normalised option string, or raises ValueError.
    """
    if not bet_options:
        return ''

    tokens = bet_options.split()
    validated = []
    while tokens:
        token = tokens.pop(0)
        if token in _ALLOWED_BET_FLAGS:
            validated.append(token)
        elif token in ('-f', '-g'):
            if not tokens:
                raise ValueError(f"BET option {token} needs a numeric argument.")
            value = tokens.pop(0)
            try:
                float(value)
            except ValueError:
                raise ValueError(f"BET option {token} needs a numeric argument, got '{value}'.")
            validated += [token, value]
        else:
            raise ValueError(
                f"Unsupported BET option '{token}'. Allowed: "
                f"{', '.join(sorted(_ALLOWED_BET_FLAGS))}, -f <fraction>, -g <gradient>."
            )
    return ' '.join(validated)


def siena_wrapper(studyInstanceUID, bet_options=DEFAULT_BET_OPTIONS):
    """Measure brain atrophy for this study and upload the results into it.

    Picks longitudinal SIENA or cross-sectional SIENAX automatically, per
    plan_siena_analysis(). Results always land in `studyInstanceUID` -- the
    study the button was pressed on -- even when the SIENA pair reaches back
    into an earlier study for its baseline.
    """
    plan = plan_siena_analysis(studyInstanceUID)
    storage_dir = os.path.join(STORAGE_ROOT, studyInstanceUID)
    os.makedirs(storage_dir, exist_ok=True)

    current = next(t for t in plan['timepoints'] if t['uid'] == studyInstanceUID)
    reference_instance = client.retrieve_series(studyInstanceUID, current['series_uid'])[0]

    if plan['mode'] == 'siena':
        return _run_and_upload_siena(
            studyInstanceUID, plan, storage_dir, reference_instance, bet_options
        )
    return _run_and_upload_sienax(
        studyInstanceUID, plan, storage_dir, reference_instance, current, bet_options
    )


def _run_and_upload_siena(studyInstanceUID, plan, storage_dir, reference_instance, bet_options):
    baseline, followup = plan['timepoints']

    baseline_nifti = os.path.join(storage_dir, 'baseline.nii.gz')
    followup_nifti = os.path.join(storage_dir, 'followup.nii.gz')
    store_series_as_nifti(baseline['uid'], baseline['series_uid'], filename=baseline_nifti)
    store_series_as_nifti(followup['uid'], followup['series_uid'], filename=followup_nifti)

    result = siena_core(
        baseline_nifti, followup_nifti, storage_dir,
        is_t2=plan['is_t2'], bet_options=bet_options,
    )

    upload_dataframe_as_sc(
        _siena_table(result, plan), title='FSL SIENA: Longitudinal Brain Atrophy',
        study_uid=studyInstanceUID, reference_instance=reference_instance,
        series_description='FSL SIENA Brain Atrophy (PBVC)',
        series_number=_TABLE_SERIES_NUMBER,
    )
    if result['render_images']:
        upload_images_as_sc(
            result['render_images'], study_uid=studyInstanceUID,
            reference_instance=reference_instance,
            series_description='FSL SIENA Brain Edge Motion (blue=atrophy, red/yellow=growth)',
            series_number=_RENDER_SERIES_NUMBER,
        )
    if result['registration_images']:
        upload_images_as_sc(
            result['registration_images'], study_uid=studyInstanceUID,
            reference_instance=reference_instance,
            series_description='FSL SIENA Registration & Segmentation QC',
            series_number=_REGISTRATION_QC_SERIES_NUMBER,
        )

    return (
        f"SIENA completed for study {studyInstanceUID}: PBVC {result['pbvc']:+.3f}% "
        f"between {baseline['date']} and {followup['date']} "
        f"on '{baseline['series_description']}'. Report and QC renders uploaded to PACS."
    )


def _run_and_upload_sienax(studyInstanceUID, plan, storage_dir, reference_instance, current,
                           bet_options):
    nifti_path = os.path.join(storage_dir, 'input.nii.gz')
    store_series_as_nifti(studyInstanceUID, current['series_uid'], filename=nifti_path)

    result = sienax_core(
        nifti_path, storage_dir, is_t2=plan['is_t2'], bet_options=bet_options,
    )

    upload_segmentation(
        result['segmentation'],
        labels=list(SIENAX_TISSUE_LABELS.keys()),
        values=list(SIENAX_TISSUE_LABELS.values()),
        series_uid=current['series_uid'],
        study_uid=studyInstanceUID,
        # Named for the standard-space mask on purpose: SIENAX segments the
        # BET brain *after* intersecting it with the MNI152-derived mask, so
        # this map is legitimately clipped at the brain edge compared to a
        # plain tissue segmentation, and a reader should not read that as a
        # segmentation failure.
        seg_description='FSL SIENAX Tissue Segmentation (standard-space masked)',
        seg_series_number=_SEG_SERIES_NUMBER,
        colors=[(60, 40, -60), (60, -30, 20), (85, 0, 0)],
    )
    upload_dataframe_as_sc(
        result['volume_table'], title='FSL SIENAX: Normalised Brain Volumes',
        study_uid=studyInstanceUID, reference_instance=reference_instance,
        series_description='FSL SIENAX Brain Volumes',
        series_number=_TABLE_SERIES_NUMBER,
    )
    if result['render_images']:
        upload_images_as_sc(
            result['render_images'], study_uid=studyInstanceUID,
            reference_instance=reference_instance,
            series_description='FSL SIENAX Segmentation & Registration QC',
            series_number=_RENDER_SERIES_NUMBER,
        )

    brain_ml = _mm3_to_ml(result['volumes']['brain_normalised_mm3'])
    return (
        f"SIENAX completed for study {studyInstanceUID} (no second MR study with a matching "
        f"series for a longitudinal comparison): normalised brain volume {brain_ml} mL "
        f"on '{current['series_description']}'. SEG, volume table and QC renders uploaded to PACS."
    )
