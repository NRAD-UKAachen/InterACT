import logging
import subprocess
import os
import sys
import tempfile
import zipfile
from io import BytesIO
import highdicom
import numpy as np
import nibabel as nib
import pandas as pd
import matplotlib
matplotlib.use('Agg')  # headless server; must be set before pyplot is imported anywhere
import matplotlib.pyplot as plt
from PIL import Image
from dicomweb_client.api import DICOMwebClient
from highdicom.seg import Segmentation
from highdicom.seg import SegmentDescription
from highdicom.seg.enum import SegmentationFractionalTypeValues
from highdicom.sr.coding import Code
from highdicom.sc import SCImage
from highdicom.enum import CoordinateSystemNames
from pydicom.dataset import Dataset

from .job_logging import run_logged


# set up the address to the database
#
# `arc` is the dcm4chee service name on the shared interact-dcm4chee network.
# This used to name the container as `nginx-dcm4chee-arc-1`, i.e. the name
# Compose derives from the project directory -- which broke the moment the
# stacks were merged into one project. The service name is stable regardless.
client = DICOMwebClient(
    url=os.environ.get(
        "DCM4CHEE_RS",
        "http://arc:8080/dcm4chee-arc/aets/DCM4CHEE/rs",
    )
)


def log(message, level=logging.INFO):
    """Log a message under the calling module's name (interact.<module>).

    It reaches stdout, API_storage/logs/api.log and -- when called from a job
    thread -- that job's own log file; see job_logging.py.
    """
    module = sys._getframe(1).f_globals.get('__name__', 'app').rsplit('.', 1)[-1]
    logging.getLogger(f'interact.{module}').log(level, message)


### GPU selection ####################################################################
def free_gpu_memory_gb(gpu_index):
    """Free memory on one GPU in GB, or None if it cannot be determined.

    Reads nvidia-smi rather than torch: torch would have to initialize a CUDA
    context on the very device whose scarce memory we are trying to measure.
    """
    try:
        result = subprocess.run(
            ['nvidia-smi', f'--id={gpu_index}', '--query-gpu=memory.free',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip().splitlines()[0]) / 1024
    except (IndexError, ValueError):
        return None


def emptiest_gpu_index():
    """The GPU with the most free memory right now, or None if that can't be
    read."""
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=index,memory.free',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    best_index, best_free = None, -1.0
    for line in result.stdout.strip().splitlines():
        try:
            index, free = (part.strip() for part in line.split(','))
            free_mb = float(free)
        except ValueError:
            continue
        if free_mb > best_free:
            best_index, best_free = int(index), free_mb
    return best_index


def choose_gpu_index(gpu_index=None):
    """The GPU a run should be pinned to: `gpu_index` when the caller already
    decided -- batch_jobs.py's worker pool gives each slot its own card, and
    that always wins -- else the one with the most free memory right now.

    The interactive per-study routes pass nothing and land here. Left to
    their own devices they ended up on cuda:0 (or a hard-coded card), which
    is one of the cards the batch pool shares out, no matter how busy it was.
    Picking by free memory naturally favours the card(s) the pool leaves out
    (see BATCH_JOB_NUM_GPUS) without hard-coding which one that is.

    Returns None when nvidia-smi can't be read; callers then fall back to
    their unpinned behavior.
    """
    if gpu_index is not None:
        return gpu_index
    chosen = emptiest_gpu_index()
    if chosen is not None:
        log(f"Picked GPU {chosen} (most free memory)")
    return chosen

### for browser download #############################################################
def retrieve_series_sorted(studyInstanceUID, seriesInstanceUID):
    """Retrieve all instances of a series, sorted by InstanceNumber."""
    instances = client.retrieve_series(studyInstanceUID, seriesInstanceUID)
    return sorted(instances, key=lambda ds: int(ds.InstanceNumber))

def series_to_zip(instances, studyInstanceUID, seriesInstanceUID):
    """Pack DICOM datasets into an in-memory ZIP buffer (rewound, ready to send)."""
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, ds in enumerate(instances, start=1):
            name = f"{studyInstanceUID}_{seriesInstanceUID}_{idx}.dcm"
            temp_path = os.path.join(tempfile.gettempdir(), name)
            ds.save_as(temp_path)
            zf.write(temp_path, name)
            os.remove(temp_path)
    zip_buffer.seek(0)
    return zip_buffer

def series_to_nifti(instances, studyInstanceUID, seriesInstanceUID):
    """Convert DICOM datasets to NIfTI with dcm2niix; return the .nii.gz path."""
    work_dir = os.path.join(tempfile.gettempdir(), f"nifti_{studyInstanceUID}_{seriesInstanceUID}")
    dcm_dir = os.path.join(work_dir, "dcm")
    nii_dir = os.path.join(work_dir, "nii")
    os.makedirs(dcm_dir, exist_ok=True)
    os.makedirs(nii_dir, exist_ok=True)

    for idx, ds in enumerate(instances, start=1):
        ds.save_as(os.path.join(dcm_dir, f"{idx:04d}.dcm"))
    log(f"Saved {len(instances)} DICOM files, running dcm2niix")

    result = run_logged(
        ["dcm2niix", "-z", "y", "-f", f"{studyInstanceUID}_{seriesInstanceUID}", "-o", nii_dir, dcm_dir],
    )
    if result.returncode != 0:
        raise RuntimeError(f"dcm2niix error: {result.stderr}")

    nii_files = [f for f in os.listdir(nii_dir) if f.endswith('.nii.gz')]
    if not nii_files:
        raise RuntimeError("dcm2niix produced no NIfTI output")
    return os.path.join(nii_dir, nii_files[0])

### series identification and download to API ########################################
def find_series(studyInstanceUID, seriesNumber=None, seriesDescription=None):
    """Query the DICOM server for series in a study, optionally filtered by
    SeriesNumber/SeriesDescription. Returns four parallel lists: the raw
    series metadata, SeriesInstanceUIDs, modalities, and descriptions."""
    query_params = {'StudyInstanceUID': studyInstanceUID}
    if seriesNumber:
        query_params['SeriesNumber'] = seriesNumber
    if seriesDescription:
        query_params['SeriesDescription'] = seriesDescription
    series = client.search_for_series(search_filters=query_params)
    
    series_uids = []
    modalities = []
    series_descriptions = []

    for serie in series:
        series_uids.append(serie.get('0020000E', {}).get('Value', [''])[0])
        modalities.append(serie.get('00080060', {}).get('Value', [''])[0])
        series_descriptions.append(serie.get('0008103E', {}).get('Value', [''])[0])

    return series, series_uids, modalities, series_descriptions

SERIES_CRITERIA = {
    't1n': {
        'modality': 'MR',
        'must_contain': ['t1', 'mpr'],
        'should_contain': ['tra', 'nativ'],
        'mustnt_contain': ['KM'],
    },
    't1c': {
        'modality': 'MR',
        'must_contain': ['t1', 'mpr', 'KM', 'tra'],
        'mustnt_contain': ['nativ'],
    },
    'flair': {
        'modality': 'MR',
        'must_contain': ['t2'],
        'or_contain': ['tirm', 'flair'],
        'should_contain': ['tra'],
        'mustnt_contain': [],
    },
    't2': {
        'modality': 'MR',
        'must_contain': ['t2', 'tse'],
        'should_contain': ['tra'],
        'mustnt_contain': [],
    },
    # The T1 SIENA/SIENAX run on: the original 3D acquisition, as opposed to
    # the secondary MPR reformats a Siemens console exports alongside it
    # ("..._MPR_MPR tra 1mm", "..._MPR_MPR cor 1mm", "..._MPR_Easyguide").
    # Same exclude-the-derived-suffix idiom the DTI roles below use.
    #
    # Note the exclusions are the doubled 'mpr_mpr' / 'mpr_easyguide' markers,
    # not a bare 'mpr_': this archive also holds genuine acquisitions named
    # 't1_mpr_nativ_tra' and 't1_mpr_KM_tra', which a bare '_mpr_' filter
    # would throw away along with the reformats.
    #
    # This matters more for SIENA than for a segmentation tool. A reformat is
    # interpolated data whose reconstruction box and angulation are chosen per
    # scan, so two timepoints' reformats differ in ways the original
    # acquisitions do not -- and SIENA measures a sub-voxel edge shift.
    # Measured on this archive's t1_mprage: BET keeps 1113 mL from the
    # reformat but 1193 mL from the original acquisition of the very same
    # scan, the difference being frontal cortex the reformat causes it to
    # clip. That clipping is what surfaced this.
    #
    # 't1n' above is deliberately left alone, so this stays scoped to SIENA --
    # but note it actively *prefers* the reformat, since its should_contain
    # 'tra' matches "..._MPR_MPR tra 1mm" and scores it above the sagittal
    # original. FastSurfer, FSL FIRST and coreg all resolve T1 through it and
    # would most likely also do better on the original acquisition.
    't1n_acquired': {
        'modality': 'MR',
        'must_contain': ['t1', 'mpr'],
        'should_contain': ['nativ', 'iso'],
        'mustnt_contain': ['KM', 'mpr_mpr', 'mpr_easyguide'],
    },
    # SIENA/SIENAX's fallback when a study has no T1 MPRAGE (see
    # siena_functions.SIENA_ROLES). Unlike 't2' this does not require a TSE
    # readout, because what matters to FSL here is a whole-head, high-
    # resolution, near-isotropic T2 -- hence 3D acquisitions (Siemens SPACE/
    # spc, GE CUBE) scoring highest. FLAIR/TIRM are excluded rather than
    # merely down-scored: attenuating the CSF moves the brain/non-brain edge
    # SIENA measures its sub-voxel shift against, and biases BET and FAST
    # with it. T2* and derived maps are excluded for the same reason.
    't2_highres': {
        'modality': 'MR',
        'must_contain': ['t2'],
        'should_contain': ['tse'],
        'mustnt_contain': ['flair', 'tirm', 'dark-fluid', 'star', 't2*', 'map', 'fsl',
                           'mpr_mpr', 'mpr_easyguide'],
    },
    # SynthSeg's own T1/T2 roles (see synthseg_functions.SYNTHSEG_CONTRASTS).
    #
    # Deliberately looser than 't1n'/'t2' above, which require a 3D MPRAGE
    # ('t1' AND 'mpr') and a TSE readout ('t2' AND 'tse') respectively -- and
    # so miss the plain 2D axial series a lot of routine clinical studies
    # consist of ("T1 ax", "T2 ax"). Those are exactly the acquisitions
    # SynthSeg exists to handle, so requiring the research-grade naming would
    # have it decline the studies it is best at.
    #
    # Kept as separate roles rather than by relaxing 't1n'/'t2' in place:
    # those two are shared by FastSurfer, BraTS, FSL FIRST and coreg, none of
    # which is contrast- or resolution-agnostic, and widening the pool they
    # draw from could silently change which series those tools pick for a
    # study they already handle. Same reasoning as 't1n_acquired' and
    # 't2_highres' above, which SIENA introduced for its own needs.
    #
    # 'must_contain' is AND and 'or_contain' is OR (see
    # score_series_for_criteria), so these read as: a T1 that is either an
    # MPRAGE or an axial series, and a T2 that is either a TSE/SPACE or an
    # axial one. 'should_contain' only scores, so when a study has both a 3D
    # MPRAGE and a 2D axial T1 the MPRAGE still wins -- looser matching adds
    # candidates, it does not demote the good ones.
    'synthseg_t1': {
        'modality': 'MR',
        'must_contain': ['t1'],
        'or_contain': ['mpr', 'ax'],
        'should_contain': ['mpr', 'nativ', 'iso', 'tra'],
        # 'KM' is this archive's contrast-agent marker (as in 't1n').
        #
        # The rest are the price of matching on 'ax': score_series_for_criteria
        # does plain substring matching with no word boundaries, so 'ax' also
        # hits 'relaxometry', 'thorax' and 'axilla' -- and a body series
        # segmented as though it were a brain would produce a full set of
        # confident, meaningless volumes rather than an obvious failure. These
        # are the words that actually occur; a name containing 'ax' inside some
        # other word entirely would still slip through, which is why the tool
        # reports which series it used.
        'mustnt_contain': ['KM', 'relax', 'thorax', 'axilla',
                           'map', 'MIP', 'seg', 'mask'],
    },
    'synthseg_t2': {
        'modality': 'MR',
        'must_contain': ['t2'],
        'or_contain': ['tse', 'spc', 'ax'],
        'should_contain': ['tse', 'spc', 'tra'],
        # FLAIR/TIRM must be excluded explicitly here: their descriptions
        # contain 't2' and routinely 'ax' too, so unlike the stricter 't2'
        # role (which they fail on the missing 'tse') they would otherwise
        # match. They are a different tissue contrast, not a T2 -- segmenting
        # one as though it were a T2 is not wrong for SynthSeg per se, but it
        # would mean the "T2" table silently describes a FLAIR. T2*/GRE and
        # derived maps are excluded for the same reason.
        # 'relax'/'thorax'/'axilla': see the note on synthseg_t1's exclusions --
        # 'ax' is matched as a bare substring.
        'mustnt_contain': ['flair', 'tirm', 'dark-fluid', 'star', 't2*',
                           'relax', 'thorax', 'axilla',
                           'map', 'MIP', 'seg', 'mask'],
    },
    'tof': {
        'modality': 'MR',
        'must_contain': ['tof', '3d'],
        'should_contain': ['tra'],
        'mustnt_contain': ['MIP'],
    },
    'ct_tissue': {
        'modality': 'CT',
        'or_contain': ['H30s', 'Hr36', 'WF', 'Br44', 'WT'],
        'should_contain': ['ax'],
        'mustnt_contain': ['MPR'],
    },
    # Any CT series at all -- no keyword filter, so this scores 0 for every
    # CT and never rules one out. Used by tools that work on whichever CT
    # series a study happens to hold rather than one specific reconstruction
    # (LDCT simulation), including tools that run over *all* of them; see
    # TOOL_SERIES_SELECTION in batch_io.py.
    'ct': {
        'modality': 'CT',
    },
    # The specimen/patient CT a screw segmentation runs on -- any CT series
    # except the derived ones a previous run of the screw pipeline itself
    # left in the study. Mirrors screw_functions._find_ct_series_uid(), which
    # makes the same exclusion when picking a series straight off dcm4chee,
    # so the batch tool and the per-study button choose the same kind of
    # series; the batch equivalent scores folder/series *names*, so it has to
    # be expressed as criteria rather than as that function's loop.
    'ct_screw': {
        'modality': 'CT',
        'mustnt_contain': ['mask', 'seg'],
    },
    # --- diffusion (DTI) roles, see dti_functions.py -------------------------
    # A Siemens DTI export arrives as one ORIGINAL series plus a family of
    # DERIVED maps sharing its description with a suffix ("..._ADC",
    # "..._TRACEW", "..._FA", "..._ColFA", "..._TENSOR_B0"). All of them match
    # on the sequence keyword, so the raw series is identified by *excluding*
    # every derived suffix rather than by a keyword of its own.
    #
    # The FA/ADC/ColFA maps below are not inputs to the tensor fit -- they are
    # what the gradient-direction reconstruction fits against when the PACS
    # stripped the Siemens private tags (see dti_gradients.py), so they are
    # optional and resolved separately from the raw series.
    #
    # 'dwi_b0' is different: it is raw data, not a derived map. Some protocol
    # versions leave the b=0 measurement out of the raw series entirely and
    # export it as its own "..._TENSOR_B0" series, in which case
    # dti_functions.py has to merge it back in -- eddy, BET and dtifit all
    # need a b0 and there is none in the raw series to find.
    # 'fsl' is excluded everywhere: dti_functions.py uploads its own FA/MD/
    # colour-FA/tract series back into the study as MR series whose
    # descriptions would otherwise match these roles, so a second run would
    # feed on the first run's output. Same self-exclusion 'ct_screw' makes.
    'dwi': {
        'modality': 'MR',
        'or_contain': ['diff', 'dti', 'dwi'],
        'should_contain': ['dti', 'mddw'],
        'mustnt_contain': ['adc', 'tracew', 'colfa', '_fa', 'tensor', 'mip',
                           'exp', 'fsl'],
    },
    'dwi_b0': {
        'modality': 'MR',
        'must_contain': ['b0'],
        'or_contain': ['diff', 'dti', 'dwi'],
        'mustnt_contain': ['fsl'],
    },
    'dwi_fa': {
        'modality': 'MR',
        'must_contain': ['_fa'],
        'or_contain': ['diff', 'dti', 'dwi'],
        'mustnt_contain': ['colfa', 'fsl'],
    },
    'dwi_adc': {
        'modality': 'MR',
        'must_contain': ['adc'],
        'or_contain': ['diff', 'dti', 'dwi'],
        'mustnt_contain': ['fsl'],
    },
    'dwi_colfa': {
        'modality': 'MR',
        'must_contain': ['colfa'],
        'or_contain': ['diff', 'dti', 'dwi'],
        'mustnt_contain': ['fsl'],
    },
}

def score_series_for_criteria(modality, series_description, criteria):
    """Score how well one series matches one required-series' criteria.
    Returns None if a hard filter (modality/must_contain/mustnt_contain/or_contain)
    fails, otherwise a non-negative score based on should_contain keyword matches."""
    if criteria['modality'] != modality:
        return None

    description = (series_description or '').lower()

    def contains(keyword):
        return keyword.lower() in description

    if not all(contains(kw) for kw in criteria.get('must_contain', [])):
        return None

    if any(contains(kw) for kw in criteria.get('mustnt_contain', [])):
        return None

    or_contain = criteria.get('or_contain', [])
    if or_contain and not any(contains(kw) for kw in or_contain):
        return None

    return sum(1 for kw in criteria.get('should_contain', []) if contains(kw))

def find_required_seriesUIDs(studyInstanceUID, required_series):
    """Find the best-matching series for each entry in `required_series` (e.g.
    ['t1n', 't1c', 't2', 'flair']). Matches are assigned greedily in that
    order and removed from the pool so no series is reused for two roles."""
    _, list_seriesUIDs, list_of_modalities, list_of_series = find_series(studyInstanceUID)

    remaining_indices = set(range(len(list_of_series)))
    UID_dic = {}

    for s in required_series:
        criteria = SERIES_CRITERIA[s]
        best_index = None
        best_score = -1

        for i in remaining_indices:
            score = score_series_for_criteria(list_of_modalities[i], list_of_series[i], criteria)
            if score is not None and score > best_score:
                best_score = score
                best_index = i

        if best_index is None:
            log(f'Could not find a matching series for required series "{s}".')
            return None

        UID_dic[s] = list_seriesUIDs[best_index]
        remaining_indices.discard(best_index)
        log(f'Series for "{s}": "{list_of_series[best_index]}" ({list_of_modalities[best_index]}, '
            f'score {best_score}) {list_seriesUIDs[best_index]}')

    return UID_dic

def _drop_off_plane_instances(instances):
    """Drop instances whose ImageOrientationPatient doesn't match the series'
    majority orientation. Some series (e.g. CT "AX" reconstructions) bundle
    in a few derived secondary images (scout/MIP/summary reformats) under the
    same SeriesInstanceUID as the real slice stack but with a different
    orientation; if one of those is used as the reference slice it poisons
    the sort order and affine for the whole series."""
    from collections import Counter

    orientations = {}
    for ds in instances:
        try:
            orientations[id(ds)] = tuple(round(float(x), 3) for x in ds.ImageOrientationPatient)
        except Exception:
            continue

    if not orientations:
        return instances

    majority_iop = Counter(orientations.values()).most_common(1)[0][0]
    filtered = [ds for ds in instances if orientations.get(id(ds)) == majority_iop]

    dropped = len(instances) - len(filtered)
    if dropped:
        log(f"Dropping {dropped} instance(s) with off-plane orientation "
              f"(likely scout/MIP/reformat) from series.")

    if not filtered:
        return instances

    # Real slices in the same series still carry tiny per-instance
    # floating-point jitter in ImageOrientationPatient (differ in the 6th-7th
    # decimal from scanner reconstruction rounding). That's within our
    # 3-decimal "same plane" tolerance above, but highdicom's Segmentation
    # constructor requires every source image's ImageOrientationPatient to
    # be *exactly* equal (raises "Source images must have a shared
    # orientation." otherwise). Snap them all to one instance's exact value
    # since they represent the same physical plane.
    canonical_iop = filtered[0].ImageOrientationPatient
    for ds in filtered:
        ds.ImageOrientationPatient = canonical_iop

    return filtered

def sort_instances_by_slice_normal(instances):
    """Sort instances along the true slice-to-slice acquisition direction
    (derived from ImageOrientationPatient), not just ImagePositionPatient[2]
    -- the latter is meaningless for sagittal/coronal/oblique series. Used
    both for building inference NIfTIs and for re-ordering instances before
    a DICOM SEG upload, so mask frames land on the right source slice."""
    instances = _drop_off_plane_instances(instances)

    first = instances[0]
    try:
        orient = np.array([float(x) for x in first.ImageOrientationPatient])
    except:
        orient = np.array([0,1,0,0,0,1])  # makes sense

    row_cosine = orient[0:3]
    col_cosine = orient[3:6]#
    normal = np.cross(row_cosine, col_cosine)

    try:
        instances.sort(key=lambda s: np.dot(np.array(s.ImagePositionPatient, dtype=float), normal))
    except:
        log("Could not sort instances based on ImagePositionPatient. Using original order.")

    return instances

def _pixel_measures(ds):
    """Return (row_spacing, col_spacing, slice_thickness) for a DICOM
    instance. Falls back to SharedFunctionalGroupsSequence's Pixel Measures
    macro when the top-level PixelSpacing/SliceThickness are absent, as seen
    on some PET reconstructions."""
    pixel_spacing = getattr(ds, 'PixelSpacing', None)
    slice_thickness = getattr(ds, 'SliceThickness', None)

    if pixel_spacing is None or slice_thickness is None:
        shared_groups = getattr(ds, 'SharedFunctionalGroupsSequence', None)
        if shared_groups:
            measures_seq = getattr(shared_groups[0], 'PixelMeasuresSequence', None)
            if measures_seq:
                measures = measures_seq[0]
                pixel_spacing = pixel_spacing if pixel_spacing is not None else getattr(measures, 'PixelSpacing', None)
                slice_thickness = slice_thickness if slice_thickness is not None else getattr(measures, 'SliceThickness', None)

    if pixel_spacing is None:
        raise ValueError(
            f"Instance {getattr(ds, 'SOPInstanceUID', '?')} has no PixelSpacing "
            "(checked top-level and SharedFunctionalGroupsSequence) -- cannot "
            "determine voxel size."
        )

    row_spacing, col_spacing = float(pixel_spacing[0]), float(pixel_spacing[1])
    return row_spacing, col_spacing, float(slice_thickness) if slice_thickness is not None else 1.0


def _rescale_params(instance):
    """Return (slope, intercept) for a DICOM instance, falling back from the
    top-level RescaleSlope/RescaleIntercept to the per-frame Pixel Value
    Transformation macro used by multi-frame instances. Uses the first
    frame's value throughout, which is fine for coregistration but not for
    absolute SUV quantification on decay-corrected PET."""
    slope = getattr(instance, 'RescaleSlope', None)
    intercept = getattr(instance, 'RescaleIntercept', None)

    if slope is None or intercept is None:
        transform_seq = None
        shared = getattr(instance, 'SharedFunctionalGroupsSequence', None)
        if shared:
            transform_seq = getattr(shared[0], 'PixelValueTransformationSequence', None)
        if not transform_seq:
            per_frame = getattr(instance, 'PerFrameFunctionalGroupsSequence', None)
            if per_frame:
                transform_seq = getattr(per_frame[0], 'PixelValueTransformationSequence', None)
        if transform_seq:
            slope = slope if slope is not None else getattr(transform_seq[0], 'RescaleSlope', None)
            intercept = intercept if intercept is not None else getattr(transform_seq[0], 'RescaleIntercept', None)

    return (
        float(slope) if slope is not None else 1.0,
        float(intercept) if intercept is not None else 0.0,
    )


def _multiframe_orientation(instance):
    """ImageOrientationPatient for a multi-frame instance: shared first, then
    the first per-frame group, matching how DICOM allows either to carry it."""
    shared = getattr(instance, 'SharedFunctionalGroupsSequence', None)
    if shared:
        plane_orientation = getattr(shared[0], 'PlaneOrientationSequence', None)
        if plane_orientation:
            return np.array([float(x) for x in plane_orientation[0].ImageOrientationPatient])

    per_frame = getattr(instance, 'PerFrameFunctionalGroupsSequence', None)
    if per_frame:
        plane_orientation = getattr(per_frame[0], 'PlaneOrientationSequence', None)
        if plane_orientation:
            return np.array([float(x) for x in plane_orientation[0].ImageOrientationPatient])

    return np.array([0, 1, 0, 0, 0, 1])


def _multiframe_order_and_origin(instance, num_frames, normal):
    """Return (frame_order, origin): frame_order is the 0-based frame indices
    sorted along `normal` (mirroring sort_instances_by_slice_normal for
    classic series); origin is the ImagePositionPatient of whichever frame
    ends up first. Falls back to on-disk order and a zero origin if per-frame
    position info isn't present."""
    per_frame = getattr(instance, 'PerFrameFunctionalGroupsSequence', None)
    positions = []
    if per_frame:
        for group in per_frame[:num_frames]:
            plane_position = getattr(group, 'PlanePositionSequence', None)
            if not plane_position:
                positions = []
                break
            positions.append(np.array([float(x) for x in plane_position[0].ImagePositionPatient]))

    if len(positions) == num_frames:
        frame_order = sorted(range(num_frames), key=lambda i: np.dot(positions[i], normal))
        origin = positions[frame_order[0]]
    else:
        frame_order = list(range(num_frames))
        origin = np.array([0.0, 0.0, 0.0])

    return frame_order, origin


def _read_multiframe_instance(instance):
    """Read a single Enhanced-Multiframe-style instance (all slices packed
    into one SOP instance -- some PET reconstructions come back this way)
    into the same (volume, row_cosine, col_cosine, normal, row_spacing,
    col_spacing, thickness, origin) shape the classic one-instance-per-slice
    path in store_series_as_nifti produces, so both feed the same
    affine-building step."""
    num_frames = int(instance.NumberOfFrames)

    orient = _multiframe_orientation(instance)
    row_cosine = orient[0:3]
    col_cosine = orient[3:6]
    normal = np.cross(row_cosine, col_cosine)

    frame_order, origin = _multiframe_order_and_origin(instance, num_frames, normal)
    row_spacing, col_spacing, thickness = _pixel_measures(instance)
    slope, intercept = _rescale_params(instance)

    pixel_array = instance.pixel_array
    if pixel_array.ndim != 3 or pixel_array.shape[0] != num_frames:
        raise ValueError(
            f"Unexpected pixel array shape {pixel_array.shape} for a {num_frames}-frame instance."
        )

    ordered = pixel_array[frame_order]  # sort along the slice normal
    volume = np.transpose(ordered, (1, 2, 0)).astype(np.float32) * slope + intercept  # -> (rows, cols, frames)

    return volume, row_cosine, col_cosine, normal, row_spacing, col_spacing, thickness, origin


def store_series_as_nifti(studyInstanceUID, seriesInstanceUID, filename = "volume.nii.gz"):
    """Download a series and save it as a NIfTI file with a correct RAS
    affine (spacing, orientation, and origin all derived from the DICOM
    headers), ready to feed into any of the inference pipelines. Handles
    both classic one-instance-per-slice series and single multi-frame
    instances (e.g. some PET reconstructions)."""
    instances = client.retrieve_series(study_instance_uid=studyInstanceUID, series_instance_uid=seriesInstanceUID)
    log(f"Retrieved {len(instances)} instance(s) of series '{getattr(instances[0], 'SeriesDescription', seriesInstanceUID)}'")

    is_multiframe = len(instances) == 1 and int(getattr(instances[0], 'NumberOfFrames', 1) or 1) > 1

    if is_multiframe:
        # A single multi-frame instance (all slices packed into one SOP
        # instance, e.g. Enhanced PET) doesn't fit the classic
        # one-instance-per-slice stacking below, so it's read separately.
        volume, row_cosine, col_cosine, normal, row_spacing, col_spacing, thickness, origin = (
            _read_multiframe_instance(instances[0])
        )

        # Build affine in LPS coordinate system
        affine_lps = np.eye(4)
        affine_lps[0:3, 0] = col_cosine * col_spacing   # voxel dim 0 = image columns
        affine_lps[0:3, 1] = row_cosine * row_spacing   # voxel dim 1 = image rows
        affine_lps[0:3, 2] = normal * thickness          # voxel dim 2 = slices
        affine_lps[0:3, 3] = origin
    else:
        # Filters out off-plane instances (scout/MIP/reformats) and sorts along
        # the slice normal, so `first` below is guaranteed to be a real slice of
        # the actual stack, not whatever happened to be retrieved first.
        instances = sort_instances_by_slice_normal(instances)
        first = instances[0]

        orient = np.array([float(x) for x in first.ImageOrientationPatient]) if hasattr(first, 'ImageOrientationPatient') else np.array([0,1,0,0,0,1])
        row_cosine = orient[0:3]
        col_cosine = orient[3:6]
        normal = np.cross(row_cosine, col_cosine)

        # Must apply RescaleSlope/RescaleIntercept to get true Hounsfield Units.
        # Without this, models (e.g. TotalSegmentator) trained on calibrated HU
        # input receive raw stored pixel values instead (e.g. shifted by +1024
        # for typical CT with RescaleIntercept=-1024), so intensity-dependent
        # segmentation ends up essentially arbitrary. upload_segmentation() does
        # this correctly already; mirror it here.
        slope = float(getattr(first, 'RescaleSlope', 1))
        intercept = float(getattr(first, 'RescaleIntercept', 0))
        volume = np.stack([s.pixel_array for s in instances], axis=-1).astype(np.float32) * slope + intercept

        row_spacing, col_spacing, thickness = _pixel_measures(first)
        try:
            origin = np.array(first.ImagePositionPatient, dtype=float)

            # Build affine in LPS coordinate system
            affine_lps = np.eye(4)
            affine_lps[0:3, 0] = col_cosine * col_spacing   # voxel dim 0 = image columns
            affine_lps[0:3, 1] = row_cosine * row_spacing   # voxel dim 1 = image rows
            affine_lps[0:3, 2] = normal * thickness          # voxel dim 2 = slices
            affine_lps[0:3, 3] = origin
        except:
            origin = np.array([0.0, 0.0, 0.0])
            row_spacing, col_spacing, thickness = _pixel_measures(first)

            # Build affine in LPS coordinate system
            affine_lps = np.eye(4)
            affine_lps[0:3, 0] = col_cosine * thickness   # voxel dim 0 = image columns
            affine_lps[0:3, 1] = row_cosine * row_spacing   # voxel dim 1 = image rows
            affine_lps[0:3, 2] = normal * col_spacing          # voxel dim 2 = slices
            affine_lps[0:3, 3] = origin

    # Convert LPS to RAS and save
    lps_to_ras = np.diag([-1, -1, 1, 1])
    affine_ras = lps_to_ras @ affine_lps

    os.makedirs(os.path.dirname(filename), exist_ok=True)
    nifti_img = nib.Nifti1Image(volume, affine_ras)
    nib.save(nifti_img, filename)

    log(f"Saved volume to {filename}")
    return volume

def store_required_series_as_nifti(studyInstanceUID, required_series, storage_path):
    """Find and download each of `required_series` (e.g. ['t1n', 't1c', 't2',
    'flair']) as NIfTI files named '<role>.nii.gz' under `storage_path`.
    Returns the {role: SeriesInstanceUID} dict, or None if a required series
    couldn't be found."""
    UID_dic = find_required_seriesUIDs(studyInstanceUID, required_series)
    if UID_dic:
        for s in required_series:
            store_series_as_nifti(studyInstanceUID, UID_dic[s], filename=os.path.join(storage_path, f'{s}.nii.gz'))
    else:
        log('Missing essential series.')

    return UID_dic


def _resolve_segmentation(segmentation, affine=None):
    """Normalize the flexible `segmentation` argument get_segment_volume() and
    segment_volume_table() accept into (label array, voxel volume in mm3).

    `segmentation` is either a path to a NIfTI file, an already-loaded nibabel
    image, or a raw array paired with `affine`. Voxel volume comes from the
    affine's linear part (its determinant), not a plain voxel count, so
    anisotropic spacing is accounted for correctly.
    """
    if isinstance(segmentation, str):
        img = nib.load(segmentation)
        data, affine = img.get_fdata(), img.affine
    elif isinstance(segmentation, nib.Nifti1Image):
        data, affine = segmentation.get_fdata(), segmentation.affine
    else:
        if affine is None:
            raise ValueError("affine is required when segmentation is a raw array.")
        data = segmentation

    return data, abs(np.linalg.det(affine[:3, :3]))


def get_segment_volume(segmentation, value, affine=None):
    """Physical volume (mm3) of the voxels equal to `value` in a label
    segmentation mask -- generic on purpose, so any segmentation-based tool
    (BraTS zones, TotalSegmentator segments, ...) can reuse it instead of
    reimplementing voxel-count * spacing itself. See _resolve_segmentation()
    for the accepted `segmentation`/`affine` forms.
    """
    data, voxel_volume_mm3 = _resolve_segmentation(segmentation, affine)
    return float(np.count_nonzero(data == value) * voxel_volume_mm3)


def voxel_volume_from_instance(ds):
    """Physical volume (mm3) of one voxel of the series `ds` belongs to, taken
    from its Pixel Measures. For masks that only ever exist as a raw array
    aligned to a DICOM series, with no NIfTI affine to take a determinant of
    (see screw_functions.py, whose mask arrives as an NRRD carrying no usable
    geometry). Equals the voxel volume implied by the affine
    store_series_as_nifti() builds for the same series."""
    row_spacing, col_spacing, thickness = _pixel_measures(ds)
    return row_spacing * col_spacing * thickness


SEGMENT_VOLUME_TABLE_COLUMNS = ['Segment', 'Voxels', 'Volume_mm3', 'Volume_mL']


def segment_volume_table(segmentation, labels, values, affine=None, voxel_volume_mm3=None):
    """Per-segment volumetry as a DataFrame: one row per (label, value) pair
    with the segment's voxel count and its physical volume in mm3 and mL.

    This is the volumetry table every segmentation-producing tool reports --
    to PACS as a rendered Secondary Capture (upload_segment_volume_table) and
    to the batch pipeline's results_overview.csv (segment_volume_stats), so
    both paths always show the same numbers. Tools whose segmentation isn't a
    flat set of independent labels report a purpose-built table instead
    (FastSurfer's left/right comparison, FSL FIRST's hippocampus report).

    `segmentation`/`affine` are the forms _resolve_segmentation() accepts.
    `voxel_volume_mm3` is the escape hatch for a raw array with no affine at
    all: pass it (from voxel_volume_from_instance(), say) and `segmentation`
    is used as the array as-is.
    """
    if voxel_volume_mm3 is None:
        data, voxel_volume_mm3 = _resolve_segmentation(segmentation, affine)
    else:
        data = segmentation

    rows = []
    for label, value in zip(labels, values):
        voxels = int(np.count_nonzero(data == int(value)))
        volume_mm3 = voxels * voxel_volume_mm3
        rows.append({
            'Segment': label,
            'Voxels': voxels,
            'Volume_mm3': round(volume_mm3, 2),
            'Volume_mL': round(volume_mm3 / 1000.0, 3),
        })

    return pd.DataFrame(rows, columns=SEGMENT_VOLUME_TABLE_COLUMNS)


def segment_volume_stats(volume_table):
    """Flatten a segment_volume_table() into a flat {metric: value} dict for
    the batch pipeline's results_overview.csv -- reuses the table the tool's
    *_core() already computed rather than recomputing volumes from the mask,
    the same way fastsurfer_batch_stats() reuses its own tables."""
    return {
        f"{row['Segment']}_Volume_mm3": float(row['Volume_mm3'])
        for _, row in volume_table.iterrows()
    }


### upload to dcm4chee ###############################################################
# Some Siemens MR consoles write a single-valued SpecificCharacterSet using
# the "code extension" encoding name (e.g. 'ISO 2022 IR 100') instead of the
# plain form ('ISO_IR 100'). Per DICOM PS3.5, code-extension encodings are
# only valid in a multi-valued declaration; a lone value must use the plain
# form. highdicom >=0.28 enforces this and raises on the malformed value, so
# map it back to the equivalent plain encoding (decodes identically).
_CODE_EXTENSION_TO_BASE_CHARACTER_SET = {
    f'ISO 2022 IR {n}': f'ISO_IR {n}'
    for n in (100, 101, 109, 110, 144, 127, 126, 138, 148, 203, 13, 166)
}

def sanitize_dicom_dataset(ds: Dataset) -> Dataset:
    """Fill in the optional fields highdicom's Segmentation constructor
    requires but a source instance may not carry."""
    if not hasattr(ds, 'PatientBirthDate'):
        ds.PatientBirthDate = ''
    if not hasattr(ds, 'PatientSex'):
        ds.PatientSex = ''
    if not hasattr(ds, 'StudyID'):
        ds.StudyID = ''
    if not hasattr(ds, 'FrameOfReferenceUID') or not ds.FrameOfReferenceUID:
        ds.FrameOfReferenceUID = highdicom.UID()

    charset = getattr(ds, 'SpecificCharacterSet', None)
    if isinstance(charset, str) and charset in _CODE_EXTENSION_TO_BASE_CHARACTER_SET:
        ds.SpecificCharacterSet = _CODE_EXTENSION_TO_BASE_CHARACTER_SET[charset]

    return ds

# Lossless, so the mask round-trips bit-exact. Used for the FRACTIONAL
# encoding below, where it also cuts the object well under what the BINARY
# form costs (sparse masks compress ~50x).
_JPEG_LS_LOSSLESS_UID = '1.2.840.10008.1.2.4.80'

_SEG_ENCODINGS = ('auto', 'binary', 'fractional', 'fractional-uncompressed')


def _segmentation_encoding_kwargs(rows, columns, encoding):
    """Pick the SEG pixel encoding and return (name, Segmentation kwargs).

    'auto' keeps BINARY -- 1 bit/pixel, the most compact form -- whenever one
    frame is a whole number of bytes, and switches to FRACTIONAL when it isn't.

    The reason is a consumer-side bug, not anything wrong with BINARY. DICOM
    packs BINARY segmentation frames as one continuous bit stream with no
    padding between frames (PS3.3 C.8.20.2), so when rows*columns isn't a
    multiple of 8 every frame after the first starts mid-byte. Our brain MR is
    257x257 = 66049 bits = 8256.125 bytes per frame, and both consumers in the
    OHIF 3.14 path get that wrong:

      - dcm4chee's WADO-RS /frames/N endpoint serves naive 8256-byte chunks,
        so frame N arrives shifted by N-1 bits; and
      - cornerstone's unpackBinaryFrame() indexes its byte array at
        `frameIndex * pixelsPerFrame * 0.125`, a fractional offset, which
        yields undefined and blanks every frame whose index isn't a multiple
        of 8.

    (Before OHIF 3.14 neither mattered: the viewer fetched the whole instance
    and unpacked it as one contiguous stream, which is correct.)

    FRACTIONAL stores 8 bits/pixel, so frames are byte-aligned by construction
    and neither code path can miscompute an offset. max_fractional_value=1
    keeps samples strictly 0/1 -- cornerstone's SEG adapter only accepts a
    FRACTIONAL segmentation when every sample is 0 or MaximumFractionalValue
    ("pseudo-binary") and otherwise throws "Fractional segmentations are not
    yet supported". JPEG-LS keeps the 8x sample-size increase from inflating
    the stored object; 'fractional-uncompressed' is the escape hatch if a
    consumer ever turns out not to decode JPEG-LS.
    """
    if encoding not in _SEG_ENCODINGS:
        raise ValueError(
            f"Unknown segmentation encoding {encoding!r}; expected one of {_SEG_ENCODINGS}."
        )

    if encoding == 'auto':
        encoding = 'binary' if (rows * columns) % 8 == 0 else 'fractional'

    if encoding == 'binary':
        return encoding, {'segmentation_type': 'BINARY'}

    kwargs = {
        'segmentation_type': 'FRACTIONAL',
        # OCCUPANCY, not the PROBABILITY default: these are hard masks, and the
        # value means "this voxel is in the segment", not a confidence.
        'fractional_type': SegmentationFractionalTypeValues.OCCUPANCY,
        'max_fractional_value': 1,
    }
    if encoding == 'fractional':
        kwargs['transfer_syntax_uid'] = _JPEG_LS_LOSSLESS_UID

    return encoding, kwargs


def upload_segmentation(segmentation, labels, values, series_uid, study_uid, seg_description='Automated Segmentation',
                       seg_series_number=5000, colors=None, encoding='auto', instances=None,
                       model_name='ThresholdSegmenter'):
    """Build a DICOM SEG from a label volume and store it in the same study
    as `series_uid`/`study_uid`. `segmentation` is a (rows, cols, slices)
    array; `labels`/`values` pair each segment's display name with the
    integer it has in `segmentation` (e.g. labels=['Edema'], values=[2]).
    `colors`, if given, is a list of (L, a, b) CIELab tuples parallel to
    `labels`/`values`; segments without an entry (or when `colors` is None)
    get a random display color, as before.

    `encoding` selects how the mask pixels are stored -- see
    _segmentation_encoding_kwargs(); the 'auto' default picks the compact
    BINARY form when the source grid allows it and a byte-aligned FRACTIONAL
    one when it doesn't.

    `instances` lets a caller that has already retrieved and sorted the
    source series hand it in rather than have it fetched a second time (see
    upload_mask_file_as_segmentation(), which needs the sorted instances to
    check the mask against the series grid before getting here). It must be
    the output of sort_instances_by_slice_normal() for that series, since
    the mask's slice axis is matched to it positionally.

    `model_name` names the software that produced the mask in the SEG's
    ManufacturerModelName - worth setting when it wasn't the threshold
    segmenter this defaults to."""
    log(f"Uploading segmentation '{seg_description}'")
    if instances is None:
        instances = client.retrieve_series(study_instance_uid=study_uid, series_instance_uid=series_uid)
        # Must use the same orientation-aware ordering as store_series_as_nifti
        # (not a raw ImagePositionPatient[2] sort), otherwise mask frames from a
        # sagittal/coronal/oblique series (e.g. TOF) end up assigned to the wrong
        # source slice and the uploaded SEG appears misregistered.
        instances = sort_instances_by_slice_normal(instances)

    rows, columns = int(instances[0].Rows), int(instances[0].Columns)
    encoding, encoding_kwargs = _segmentation_encoding_kwargs(rows, columns, encoding)
    log(f"  {rows}x{columns} source grid ({rows * columns} bits/frame) -> {encoding} encoding")

    # Build one binary mask per segment, plus its DICOM segment description.
    masks = []
    segment_descriptions = []
    segmentation = segmentation.transpose(2, 0, 1)  # Transpose to match DICOM orientation

    for i, (label, value) in enumerate(zip(labels, values), start=1):
        mask = np.zeros_like(segmentation, dtype=np.uint8)
        mask[segmentation == int(value)] = 1
        masks.append(mask)

        color = colors[i - 1] if colors and i - 1 < len(colors) else (
            np.random.randint(0, 100), np.random.randint(-128, 127), np.random.randint(-128, 127)
        )

        segment_description = SegmentDescription(
            segment_number=i,
            segment_label=label,
            algorithm_type="MANUAL",
            segmented_property_category=Code("T-D0050", "SRT", "Anatomical Structure"),
            segmented_property_type=Code("T-12770", "SRT", "Bone"),
            display_color=highdicom.color.CIELabColor(*color)
        )
        segment_descriptions.append(segment_description)

    combined_mask = np.stack(masks, axis=3)

    sanitized_instances = [sanitize_dicom_dataset(ds) for ds in instances]
    segmentation_obj = Segmentation(
        source_images=sanitized_instances,
        pixel_array=combined_mask,
        segment_descriptions=segment_descriptions,
        series_instance_uid=highdicom.UID(),
        series_number=seg_series_number,
        series_description=seg_description,
        sop_instance_uid=highdicom.UID(),
        instance_number=1,
        manufacturer="TestOrg",
        manufacturer_model_name=model_name,
        software_versions="1.0.0",
        device_serial_number="SN-TEST",
        **encoding_kwargs
    )

    log(f"Uploading SEG '{seg_description}' ({len(labels)} segment(s): {', '.join(map(str, labels))}) "
        f"to study {study_uid}")
    client.store_instances([segmentation_obj])
    return segmentation_obj

def mask_array_for_series(mask, instances, affine=None):
    """Bring a label mask onto the (rows, columns, slices) voxel grid and the
    slice order upload_segmentation() expects for `instances`.

    `mask` is a path to a NIfTI file, a loaded nibabel image, or a raw array
    paired with `affine`. `instances` must already be sorted by
    sort_instances_by_slice_normal().

    Two things can differ between a mask written by an external tool and the
    grid the SEG needs:

      - Axis order. A mask stacked in the (columns, rows, slices) layout -
        which is what a tool that built its volume straight from
        ImageOrientationPatient produces, since the first direction cosine is
        the one along increasing *column* index - has to be transposed to
        (rows, columns, slices). On a non-square image the shape says which of
        the two it is; on the usual square one (512x512) it doesn't, so the
        affine decides: its first column points along one of the two in-plane
        direction cosines, and which one tells us what voxel axis 0 indexes.

      - Slice direction. The mask is expected to run along the slice normal in
        the same ascending direction sort_instances_by_slice_normal() uses, but
        a tool that stacked the series the other way round is easy to spot: its
        affine's origin then sits on the *last* instance rather than the first,
        and the slice axis is reversed here.

    Anything that fits neither in-plane layout raises rather than being
    uploaded as a SEG whose frames land on the wrong pixels.
    """
    if isinstance(mask, str):
        img = nib.load(mask)
        data, affine = np.asanyarray(img.dataobj), img.affine
    elif isinstance(mask, nib.Nifti1Image):
        data, affine = np.asanyarray(mask.dataobj), mask.affine
    else:
        data = np.asarray(mask)

    if data.ndim != 3:
        raise ValueError(f"Segmentation mask must be a 3D volume (got {data.ndim} dimensions).")

    rows, columns, slices = int(instances[0].Rows), int(instances[0].Columns), len(instances)
    in_plane = tuple(data.shape[:2])

    if data.shape[2] != slices or sorted(in_plane) != sorted((rows, columns)):
        raise ValueError(
            f"Segmentation mask of shape {data.shape} does not fit this series: it has "
            f"{slices} slice(s) of {rows}x{columns} pixels, so the mask must be "
            f"{(rows, columns, slices)} or {(columns, rows, slices)}."
        )

    if rows != columns:
        column_major = in_plane == (columns, rows)
    else:
        column_major = _mask_axis0_is_column_index(affine, instances[0])

    if column_major:
        data = data.transpose(1, 0, 2)

    data = _match_slice_direction(data, affine, instances)
    return np.ascontiguousarray(data)


def _mask_axis0_is_column_index(affine, instance):
    """Whether voxel axis 0 of a mask with this affine indexes image columns.

    Only reached for a square image, where the mask's shape can't say. The
    affine's first column is the world direction voxel axis 0 runs along, so
    comparing it against the two in-plane direction cosines - the first of
    ImageOrientationPatient runs along increasing column index, the second
    along increasing row index - identifies the axis. Compared by magnitude,
    so an affine written in RAS rather than DICOM's LPS still lands on the
    right one for the axis-aligned orientations this matters for.
    """
    try:
        orient = np.array([float(x) for x in instance.ImageOrientationPatient])
        axis0 = np.array(affine, dtype=float)[:3, 0]
        norm = float(np.linalg.norm(axis0))
        if norm < 1e-9 or not np.all(np.isfinite(axis0)):
            raise ValueError("degenerate affine")
        axis0 = axis0 / norm
    except Exception:
        # Square image, no usable geometry: read it the way DICOM itself
        # stores a frame, which is the layout the SEG needs unchanged.
        log("  warning: mask has no usable affine and the image is square - "
            "assuming its first axis indexes rows, like a DICOM frame")
        return False

    along_columns = abs(float(np.dot(axis0, orient[0:3])))
    along_rows = abs(float(np.dot(axis0, orient[3:6])))
    return along_columns >= along_rows


def _match_slice_direction(data, affine, instances):
    """Reverse the mask's slice axis if its affine says it was stacked against
    the direction sort_instances_by_slice_normal() puts the instances in."""
    if affine is None or len(instances) < 2:
        return data

    try:
        origin = np.asarray(affine, dtype=float)[:3, 3]
        first = np.asarray(instances[0].ImagePositionPatient, dtype=float)
        last = np.asarray(instances[-1].ImagePositionPatient, dtype=float)
    except Exception:
        return data

    # A hit has to actually coincide with one end of the stack. Without that
    # tolerance an affine given in some other convention (a NIfTI in RAS, say,
    # whose origin is neither instance's position) would still be "closer" to
    # one end and flip the mask on nothing.
    tolerance = max(0.01 * float(np.linalg.norm(last - first)), 1.0)
    d_first, d_last = float(np.linalg.norm(origin - first)), float(np.linalg.norm(origin - last))

    if d_last < d_first and d_last <= tolerance:
        log("  mask origin sits on the last slice of the series -> reversing its slice axis")
        return data[:, :, ::-1]
    if d_first > tolerance:
        log(f"  warning: mask origin matches neither end of the series (off by {d_first:.1f} mm "
            f"and {d_last:.1f} mm); assuming it runs along the slice normal like the series does")
    return data


def upload_mask_file_as_segmentation(mask, study_uid, series_uid, segment_label='Segmentation',
                                     seg_description='Automated Segmentation', seg_series_number=5000,
                                     model_name='ThresholdSegmenter', colors=None, encoding='auto'):
    """Store a label mask that was produced outside this API - typically a
    NIfTI file posted by an interactive tool - as a DICOM SEG in the study of
    `series_uid`/`study_uid`.

    The source series is retrieved and sorted once here, used to check and
    reorient the mask (mask_array_for_series), and handed to
    upload_segmentation(), so nothing is fetched twice. Every distinct
    non-zero value in the mask becomes one segment; a single-valued mask (the
    usual binary case) is labelled `segment_label` alone, and a multi-valued
    one gets its value appended per segment.

    Returns (segmentation object, labels, values).
    """
    instances = client.retrieve_series(study_instance_uid=study_uid, series_instance_uid=series_uid)
    if not instances:
        raise ValueError(f"Series {series_uid} of study {study_uid} holds no instances.")
    instances = sort_instances_by_slice_normal(instances)

    data = mask_array_for_series(mask, instances)
    if not np.issubdtype(data.dtype, np.integer):
        # A label volume stored as float (nibabel hands back float64 for a
        # scaled NIfTI): round to labels, or 0.9999 would become segment 0.
        data = np.rint(data).astype(np.int16)

    values = [int(v) for v in np.unique(data) if int(v) != 0]
    if not values:
        raise ValueError("The segmentation mask is empty - there is nothing to store as a SEG.")
    labels = [segment_label] if len(values) == 1 else [f"{segment_label} {v}" for v in values]

    segmentation_obj = upload_segmentation(
        data, labels, values, series_uid, study_uid,
        seg_description=seg_description,
        seg_series_number=seg_series_number,
        colors=colors,
        encoding=encoding,
        instances=instances,
        model_name=model_name,
    )
    return segmentation_obj, labels, values


### tables / reports as DICOM Secondary Capture #######################################
def rasterize_figure(fig, mode='L'):
    """Rasterize a matplotlib Figure to a numpy array and close it. mode='L'
    for grayscale (tables), 'RGB' for color (rendered 3D surfaces etc.)."""
    buf = BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return np.array(Image.open(buf).convert(mode))


def dataframe_to_image_array(df, title, highlight_rows=None, highlight_note=None,
                             highlight_color='#f7c6c6'):
    """Render a DataFrame as a table and rasterize it to a uint8 array, for
    upload via upload_dataframe_as_sc().

    `highlight_rows` is an optional per-row boolean sequence (aligned with
    df's rows, not its index labels); rows marked True get `highlight_color`
    as their background, so a reader spots the flagged rows without reading
    every number -- e.g. FastSurfer marking a left-right asymmetry above its
    threshold. `highlight_note` is a short caption drawn under the table
    saying what the color means, so the rendered image is self-explanatory.

    Rasterizing is grayscale ('L') normally and RGB when rows are
    highlighted, since grayscale would flatten the color into an
    indistinguishable gray band."""
    # Size the figure and each column off its actual text content instead of
    # a fixed even split, so a long column (e.g. a "Metric" name) gets
    # proportionally more room instead of clipping its text.
    col_chars = [
        max(len(str(col)), df[col].astype(str).str.len().max())
        for col in df.columns
    ]
    total_chars = sum(col_chars)
    fig_width = max(8, total_chars * 0.11)
    fig_height = max(2.5, 0.35 * (len(df) + 2))
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.axis('off')
    ax.set_title(title, fontsize=14, pad=16)

    col_widths = [c / total_chars for c in col_chars]
    table = ax.table(cellText=df.values, colLabels=df.columns, loc='center', cellLoc='center',
                      colWidths=col_widths)
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    table.scale(1, 1.3)

    if highlight_rows is not None:
        highlight_rows = list(highlight_rows)
        for row, flagged in enumerate(highlight_rows):
            if not flagged:
                continue
            for col in range(len(df.columns)):
                # +1: row 0 of the rendered table holds the column labels.
                table[row + 1, col].set_facecolor(highlight_color)
        if highlight_note:
            ax.text(0.5, 0.0, highlight_note, transform=ax.transAxes,
                    ha='center', va='top', fontsize=8)

    return rasterize_figure(fig, mode='L' if highlight_rows is None else 'RGB')


def _pad_to_even_sample_count(pixel_array):
    """Repeat the last pixel column when an image has an odd total number of
    samples, so the DICOM PixelData element does not need a pad byte.

    A DICOM data element must have an even length, so pydicom appends a pad
    byte to odd-length PixelData. For MONOCHROME2 that is harmless, but for
    RGB it leaves a pixel-data length that is no longer a multiple of 3, and
    OHIF's renderer rejects the image outright -- vtk.js throws "model.size is
    not a multiple of model.numberOfComponents", Cornerstone's StackViewport
    swallows that exception and leaves its imageData undefined, and the viewer
    shows a black pane with a TypeError on setOrigin. Since a colour image has
    3 samples per pixel, an odd sample count means rows*cols is odd, so adding
    one column is enough; copying the edge keeps it invisible on both a white
    table and a dark render."""
    rows, cols = pixel_array.shape[:2]
    samples = pixel_array.shape[2] if pixel_array.ndim == 3 else 1
    if (rows * cols * samples) % 2 == 0:
        return pixel_array

    padding = [(0, 0)] * pixel_array.ndim
    padding[1] = (0, 1)  # one extra column
    return np.pad(pixel_array, padding, mode='edge')


def _copy_patient_id_issuer(ds, reference_instance):
    """Copy IssuerOfPatientID (and its qualifiers) from `reference_instance`
    onto a derived instance we build from scratch.

    A patient is identified by PatientID *plus* its issuing authority, not by
    the ID alone. dcm4chee is configured with a SupplementIssuerOfPatientID
    attribute coercion, which stamps its own local issuer
    ("DCM4CHEE.<PatientName hash>.<PatientBirthDate hash>") on any instance
    that arrives without one. For a study whose images carry a real issuer --
    e.g. 'UK_AACHEN' on anything imported from the clinical archive -- that
    supplemented issuer does not match, and the store is rejected with

        Patient ID <id>^^^DCM4CHEE.<hash>.<hash> differs from Patient ID
        <id>^^^UK_AACHEN in previous received object of Study[uid=...]

    which STOW-RS reports as a bare 409 Conflict. It bites only the objects
    built from scratch (SCImage here): a DICOM SEG, or a series copied slice
    by slice off its reference instances, inherits the issuer with the rest
    of the patient attributes -- which is why a tool's segmentation would
    upload and its rendered table alongside it would not.
    """
    for keyword in ('IssuerOfPatientID', 'IssuerOfPatientIDQualifiersSequence'):
        value = getattr(reference_instance, keyword, None)
        if value is not None:
            setattr(ds, keyword, value)


def _sc_image_from_array(pixel_array, photometric_interpretation, study_uid, series_uid,
                          series_number, instance_number, reference_instance):
    """Build one DICOM Secondary Capture instance from a rendered image array
    ('MONOCHROME2' for grayscale, 'RGB' for color). Patient/study identifiers
    are copied from `reference_instance` (any already-uploaded instance from
    the same study) so dcm4chee links the new instance to the right patient/study.
    """
    pixel_array = _pad_to_even_sample_count(pixel_array)
    sc_image = SCImage(
        pixel_array=pixel_array,
        photometric_interpretation=photometric_interpretation,
        bits_allocated=8,
        coordinate_system=CoordinateSystemNames.PATIENT,
        # This is a rendered image, not a real anatomical acquisition, so there
        # is no true patient orientation -- highdicom requires the field anyway
        # for the PATIENT coordinate system, so pass an arbitrary placeholder.
        patient_orientation=('L', 'P'),
        study_instance_uid=study_uid,
        series_instance_uid=series_uid,
        series_number=series_number,
        sop_instance_uid=highdicom.UID(),
        instance_number=instance_number,
        manufacturer='Interact API',
        patient_id=getattr(reference_instance, 'PatientID', None),
        patient_name=getattr(reference_instance, 'PatientName', None),
        patient_birth_date=getattr(reference_instance, 'PatientBirthDate', None),
        patient_sex=getattr(reference_instance, 'PatientSex', None),
        study_id=getattr(reference_instance, 'StudyID', None),
        accession_number=getattr(reference_instance, 'AccessionNumber', None),
    )
    _copy_patient_id_issuer(sc_image, reference_instance)
    return sc_image


def upload_dataframe_as_sc(df, title, study_uid, reference_instance, series_description,
                            series_number, highlight_rows=None, highlight_note=None):
    """Render a DataFrame as a table image and store it as a DICOM Secondary
    Capture instance in the given study, so it shows up alongside the rest of
    the study in OHIF.

    `highlight_rows`/`highlight_note` are passed through to
    dataframe_to_image_array(); highlighting makes the render RGB, so the
    photometric interpretation follows the array rather than being fixed."""
    pixel_array = dataframe_to_image_array(
        df, title, highlight_rows=highlight_rows, highlight_note=highlight_note,
    )
    photometric_interpretation = 'RGB' if pixel_array.ndim == 3 else 'MONOCHROME2'
    sc_image = _sc_image_from_array(
        pixel_array, photometric_interpretation, study_uid, highdicom.UID(),
        series_number, 1, reference_instance,
    )
    sc_image.SeriesDescription = series_description

    log(f"Uploading table '{series_description}'")
    client.store_instances([sc_image])


def upload_segment_volume_table(volume_table, title, study_uid, series_uid, series_description, series_number):
    """Store a segment_volume_table() in the study as a rendered DICOM
    Secondary Capture, so a segmenting tool's volumetry is persisted in PACS
    next to its SEG and viewable in OHIF -- the same treatment FastSurfer's
    and FSL FIRST's tables already got, factored out so every other
    segmentation button gets it too.

    `series_uid` is the series the segmentation was computed on; one of its
    instances supplies the patient/study identifiers the SC needs. (This is
    an image and not a DICOM SR on purpose -- see hippocampus_functions.py's
    _upload_volume_table for why an SR doesn't work here.)"""
    reference_instance = client.retrieve_series(
        study_instance_uid=study_uid, series_instance_uid=series_uid
    )[0]
    upload_dataframe_as_sc(
        volume_table, title=title, study_uid=study_uid,
        reference_instance=reference_instance,
        series_description=series_description, series_number=series_number,
    )


def upload_images_as_sc(pixel_arrays, study_uid, reference_instance, series_description, series_number):
    """Upload a list of rendered RGB image arrays (e.g. different views of a
    3D render) as a single DICOM Secondary Capture series, one instance per
    image, so they show up as a scrollable series in OHIF alongside the rest
    of the study."""
    series_uid = highdicom.UID()
    instances = [
        _sc_image_from_array(pixel_array, 'RGB', study_uid, series_uid, series_number, i, reference_instance)
        for i, pixel_array in enumerate(pixel_arrays, start=1)
    ]
    for instance in instances:
        instance.SeriesDescription = series_description

    log(f"Uploading {len(instances)} image(s) for '{series_description}'")
    client.store_instances(instances)
