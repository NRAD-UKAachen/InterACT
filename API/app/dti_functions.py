"""
FSL diffusion-tensor and tractography pipeline for a study's DTI series.

Ports the validated protocol in ../DTI/PIPELINE_Datensatz2.md into this API as
two buttons, sharing one working directory per study so the second reuses what
the first produced:

  dti/tensor        DICOM -> NIfTI (dcm2niix) -> gradient scheme -> brain mask
                    (BET on the averaged b0) -> eddy -> dtifit.
                    Uploads FA, MD and direction-encoded colour FA as new MR
                    series, plus a QC series and a metrics table, and tracks
                    the whole-brain fibre field (deterministic, on the tensor
                    fit alone -- see dti_streamlines.py). ~10-15 min.

  dti/tractography  additionally: bedpostx (2 fibre orientations per voxel,
                    3 above _BEDPOSTX_THREE_FIBRE_MIN_DIRECTIONS measured
                    directions) -> registration to standard space -> XTRACT
                    (42 tracts).
                    Uploads the reconstructed tracts as colour overlays on FA
                    and extends the metrics table. ~45-60 min on top.

Three properties of this project's data shape the whole pipeline:

  * The PACS strips the Siemens private tags, so dcm2niix produces an all-zero
    .bval/.bvec. The scheme is reconstructed from the data instead (see
    dti_gradients.py) and is *not* trusted until the resulting FA has been
    correlated against the scanner's own FA map -- a run whose reconstruction
    fails that check aborts rather than producing a plausible-looking wrong
    tensor. See validate_against_scanner_fa().

  * The exports carry no T1, so the usual diffusion -> T1 -> MNI registration
    chain is unavailable and FA is registered straight to FMRIB58_FA_1mm, the
    way TBSS does. That is good enough for atlas-based tractography and not
    good enough for precise localisation of an individual lesion; the metrics
    table reports the registration quality so a reader can judge it. See
    _register_to_standard().

  * Some versions of the protocol keep the b=0 measurement out of the raw
    series and export it as a separate "..._TENSOR_B0" series, leaving the raw
    series diffusion-weighted throughout. BET, eddy and dtifit all need a b0,
    so it is merged back in as volume 0. See _merge_external_b0().

Susceptibility distortion is not corrected: topup needs a b0 acquired with the
opposite phase-encoding direction, and these acquisitions have only one. A few
millimetres of frontobasal and temporopolar distortion therefore remain, which
matters for where a tract is said to run, not for whether it was found.

Nothing here writes streamlines a surgeon should plan against. XTRACT's tract
densities are model output that stops where FA drops -- in oedema, in tumour
infiltration, at crossings -- so a missing tract is evidence that the model did
not get through, not that the fibre bundle is interrupted.
"""
import glob
import json
import os
import shutil
import subprocess

import nibabel as nib
import numpy as np
import pandas as pd
import pydicom

from . import dti_gradients
from . import dti_streamlines
from .backend_helpers import (
    SERIES_CRITERIA, client, emptiest_gpu_index, find_series, log, score_series_for_criteria,
    upload_dataframe_as_sc, upload_images_as_sc,
)
from .job_logging import run_logged
from .nifti_to_dicom import build_and_store_dicom_series_from_reoriented_volume
from .storage_paths import API_STORAGE_DIR

FSLDIR = os.environ.get("FSLDIR", "/usr/local/fsl")
FSL_BIN = os.path.join(FSLDIR, "bin")
STANDARD_FA = os.path.join(FSLDIR, "data/standard/FMRIB58_FA_1mm")
STANDARD_T1_BRAIN = os.path.join(FSLDIR, "data/standard/MNI152_T1_1mm_brain")

# XTRACT's protocol library for the species this pipeline runs (-species
# HUMAN): one directory per tract, alongside a structureList file. Read to
# tell a finished XTRACT run from an interrupted one -- see _run_xtract().
XTRACT_PROTOCOLS = os.path.join(FSLDIR, "data/xtract_data/HUMAN")

STORAGE_ROOT = f"{API_STORAGE_DIR}/dti"

# DICOM stores integers, so both scalar maps are scaled to the units the
# scanner itself uses for the same quantities: FA x1000 (the scanner's own FA
# series is read back as /1000 in dti_gradients.py) and MD in 1e-6 mm^2/s, the
# units of its ADC map. A reader comparing the pipeline's map against the
# scanner's is then comparing like with like.
FA_SCALE = 1000.0
MD_SCALE = 1e6

_FA_SERIES_NUMBER = 5100
_MD_SERIES_NUMBER = 5101
_COLFA_SERIES_NUMBER = 5102
_QC_SERIES_NUMBER = 5103
_METRICS_SERIES_NUMBER = 5104
_TRACTS_SERIES_NUMBER = 5110
_TRACT_HIGHLIGHT_SERIES_BASE = 5120
_STREAMLINES_SERIES_NUMBER = 5130

# Tracts uploaded as their own overlay series, on top of the combined one --
# the bundles most often asked about in neurosurgical and stroke reading.
# Anything empty after XTRACT is skipped rather than uploaded blank.
TRACT_HIGHLIGHTS = (
    ('cst_l', 'Corticospinal tract, left'),
    ('cst_r', 'Corticospinal tract, right'),
    ('af_l', 'Arcuate fasciculus, left'),
    ('af_r', 'Arcuate fasciculus, right'),
    ('or_l', 'Optic radiation, left'),
    ('or_r', 'Optic radiation, right'),
)

# A tract's extent is counted above 1% of its own maximum density, the same
# threshold the reference protocol reported its per-tract voxel counts at.
_TRACT_THRESHOLD_FRACTION = 0.01

# Distinct hues for the combined tract overlay, in the order tracts are drawn
# (strongest first). Deliberately saturated and far apart so neighbouring
# bundles stay separable on a greyscale FA background.
_TRACT_COLOURS = (
    (255, 60, 60), (60, 200, 255), (120, 255, 90), (255, 200, 40),
    (220, 110, 255), (60, 120, 255), (255, 140, 60), (80, 255, 210),
)

# Deterministic FACT tractography for the whole-brain fibre viewer -- see
# dti_streamlines.py. Not the same reconstruction as XTRACT (probabilistic,
# per-protocol) and not validated the way XTRACT's atlas protocols are; this
# is for a picture, seeded broadly wherever the tensor fit is confident.
_STREAMLINE_SEED_FA_THRESHOLD = 0.25
_STREAMLINE_SEED_COUNT = 6000

# Loopback front door for files a browser fetches directly rather than
# through this JSON API -- same env var and default host hippocampus_
# functions.py's FILES_BASE_URL uses, for the same reason (see routes.py's
# hippocampus_files_route()).
FILES_BASE_URL = os.environ.get("API_PUBLIC_BASE_URL", "http://ohif_api:9200")

# Everything below this is a step of the reference protocol; the timeouts are
# roughly 4x the runtimes it recorded on this host (an A100 shared with other
# work), i.e. generous enough not to kill a slow-but-healthy run.
_TIMEOUT_CONVERT = 3600
_TIMEOUT_SHORT = 600
_TIMEOUT_EDDY = 7200
_TIMEOUT_BEDPOSTX = 14400
_TIMEOUT_XTRACT = 14400

# eddy_cpu is single-threaded unless told otherwise (--nthr defaults to 1),
# which is what the no-GPU fallback gets. Capped rather than all cores: the
# host is shared, and eddy's speed-up flattens well before 256 threads.
_EDDY_CPU_THREADS = int(os.environ.get(
    "DTI_EDDY_THREADS", min(16, os.cpu_count() or 1)))


def _fsl_env(gpu_index=None):
    """Environment for an FSL call.

    bedpostx and xtract are shell scripts that invoke further FSL binaries by
    bare name, so $FSLDIR/bin has to be on PATH, not just referenced by the
    command we build. The CUDA builds of eddy/bedpostx/probtrackx pick their
    device from CUDA_VISIBLE_DEVICES, which is how the run is pinned -- to
    `gpu_index`, or else whichever card has the most free memory at the time
    of this call. Each FSL call is its own process, so choosing per call
    (eddy and bedpostx can land on different cards) is safe.
    """
    env = {
        **os.environ,
        "FSLDIR": FSLDIR,
        "FSLOUTPUTTYPE": "NIFTI_GZ",
        "PATH": f"{FSL_BIN}:{os.environ.get('PATH', '')}",
    }
    if gpu_index is None:
        gpu_index = emptiest_gpu_index()
    if gpu_index is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(gpu_index)
    return env


def _run_fsl(command, timeout, cwd=None, gpu_index=None, check=True):
    """Run one FSL command, raising with both streams attached on failure.

    FSL tools routinely report the actual cause on stdout and exit non-zero
    with an empty stderr, so both are folded into the exception -- that string
    is what reaches the user as the job's error toast.
    """
    if not os.path.isabs(command[0]):
        command = [os.path.join(FSL_BIN, command[0])] + list(command[1:])
    result = run_logged(
        [str(c) for c in command], timeout=timeout, cwd=cwd, env=_fsl_env(gpu_index),
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"{os.path.basename(command[0])} failed (exit {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}".strip()
        )
    return result


def _gpu_available():
    """Whether a CUDA device is actually visible to this container.

    The CUDA builds of eddy/bedpostx exit with a CUDA runtime error rather
    than falling back when no device is present, so this decides up front
    which build to call instead of failing several minutes in.
    """
    if not shutil.which("nvidia-smi"):
        return False
    try:
        return subprocess.run(
            ["nvidia-smi", "-L"], capture_output=True, timeout=30
        ).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


### ---------------------------------------------------------------------- ###
### stage-in: find the study's diffusion series and convert them            ###
### ---------------------------------------------------------------------- ###

def find_dti_series(studyInstanceUID):
    """Resolve the study's diffusion series into {role: SeriesInstanceUID}.

    Returns the raw series under 'dwi', plus whichever of the scanner's
    derived maps ('dwi_fa', 'dwi_adc', 'dwi_colfa') and of the separately
    exported b0 ('dwi_b0') the export carried. Written here rather than
    reusing find_required_seriesUIDs() because that one is all-or-nothing:
    the companion series are optional (the derived maps are only needed when
    the gradient scheme has to be reconstructed, the b0 series only when the
    raw series has no b0 volume of its own), and a study that has them must
    not be rejected for missing one of them.

    Among equally well-matching candidates for the raw series the one with the
    most instances wins. A Siemens DTI export puts thousands of images in the
    raw series and ~one per slice in each derived map, so this is what
    separates them when a description does not follow the expected suffixes.

    The companion series are then resolved *against the chosen raw series*,
    not independently: a study frequently holds more than one diffusion
    acquisition (a clinical trace-DWI alongside the DTI, say), and the
    criteria alone cannot tell those two acquisitions' ADC maps apart. Siemens
    names a derived map after its own raw series plus a suffix, so a candidate
    whose description starts with the raw one's is preferred over any other.
    Getting this wrong would fit the DTI's gradient directions against a
    different scan's tensor -- which the shape check in
    dti_gradients.estimate_directions() would usually, but not always, catch.
    """
    series, series_uids, modalities, descriptions = find_series(studyInstanceUID)

    def instance_count(index):
        return int(series[index].get('00201209', {}).get('Value', [0])[0] or 0)

    def candidates_for(role):
        return [
            i for i in range(len(series_uids))
            if score_series_for_criteria(modalities[i], descriptions[i],
                                         SERIES_CRITERIA[role]) is not None
        ]

    raw_candidates = candidates_for('dwi')
    if not raw_candidates:
        raise ValueError(
            f"Study {studyInstanceUID} has no diffusion-weighted MR series "
            "(nothing matching 'diff'/'DTI'/'DWI' that is not a derived ADC/"
            "TRACEW/FA/ColFA map)."
        )
    raw = max(raw_candidates, key=lambda i: (
        score_series_for_criteria(modalities[i], descriptions[i], SERIES_CRITERIA['dwi']),
        instance_count(i),
    ))
    resolved = {'dwi': series_uids[raw]}
    log(f"DTI role 'dwi': {descriptions[raw]!r} ({instance_count(raw)} instances)")

    raw_description = (descriptions[raw] or '').lower()
    for role in ('dwi_b0', 'dwi_fa', 'dwi_adc', 'dwi_colfa'):
        candidates = candidates_for(role)
        if not candidates:
            continue
        same_acquisition = [i for i in candidates
                            if (descriptions[i] or '').lower().startswith(raw_description)]
        if not same_acquisition:
            log(f"DTI role '{role}': no candidate named after {descriptions[raw]!r}; "
                f"falling back to {descriptions[candidates[0]]!r}, which may belong "
                "to a different acquisition.")
        pick = (same_acquisition or candidates)[0]
        resolved[role] = series_uids[pick]
        log(f"DTI role '{role}': {descriptions[pick]!r} "
            f"({instance_count(pick)} instances)")

    return resolved


def _convert_series(studyInstanceUID, seriesInstanceUID, out_dir, basename,
                    with_gradients):
    """Convert one series to NIfTI with dcm2niix under out_dir/<basename>.*.

    dcm2niix is used rather than backend_helpers.store_series_as_nifti()
    because only it understands a 4D diffusion series: that helper stacks one
    slice per instance along a third axis, which for 102 volumes x 74 slices
    produces a single 7548-slice block rather than a 4D volume. dcm2niix also
    writes the .bval/.bvec sidecars (`-b y`), which is where the gradient
    scheme would be if the PACS had not stripped it.

    A diffusion series can make dcm2niix emit several outputs (it splits off
    derived maps it recognises); the 4D one with the most volumes is the raw
    series and is what gets returned.
    """
    dicom_dir = os.path.join(out_dir, f"dicom_{basename}")
    os.makedirs(dicom_dir, exist_ok=True)

    instances = client.retrieve_series(studyInstanceUID, seriesInstanceUID)
    log(f"Retrieved {len(instances)} instance(s) for '{basename}'")
    for index, ds in enumerate(instances, start=1):
        ds.save_as(os.path.join(dicom_dir, f"{index:05d}.dcm"))

    command = ["dcm2niix", "-b", "y" if with_gradients else "n", "-z", "y",
               "-f", basename, "-o", out_dir, dicom_dir]
    result = run_logged(command, timeout=_TIMEOUT_CONVERT)
    if result.returncode != 0:
        raise RuntimeError(f"dcm2niix failed for '{basename}':\n{result.stdout}\n{result.stderr}")

    candidates = sorted(glob.glob(os.path.join(out_dir, f"{basename}*.nii.gz")))
    if not candidates:
        raise RuntimeError(f"dcm2niix produced no NIfTI for '{basename}'.")

    def volumes(path):
        shape = nib.load(path).shape
        return shape[3] if len(shape) > 3 else 1

    chosen = max(candidates, key=volumes)
    # dcm2niix appends a suffix when it splits a series; normalise back onto
    # the plain name the rest of the pipeline (and FSL's -f/--imain arguments)
    # expects, taking the sidecars along.
    target = os.path.join(out_dir, f"{basename}.nii.gz")
    if chosen != target:
        for extension in ('.nii.gz', '.bval', '.bvec', '.json'):
            source = chosen[: -len('.nii.gz')] + extension
            if os.path.exists(source):
                shutil.move(source, os.path.join(out_dir, basename + extension))

    shutil.rmtree(dicom_dir, ignore_errors=True)
    return target, instances


def stage_dti_series(studyInstanceUID, work_dir):
    """Download and convert the study's diffusion series into work_dir.

    Returns a dict with the resolved series UIDs, the converted file paths,
    and the two things that have to be taken off the DICOM headers while they
    are still in hand: one cached reference instance (the source of the
    patient/study identifiers every upload needs, and the only part of the
    series the tractography stage's reuse path still needs afterwards) and the
    acquisition's b-values. The instance list itself is not kept -- a raw DTI
    series is thousands of images and holding them all through a
    45-minute pipeline costs hundreds of MB for two header lookups.
    """
    os.makedirs(work_dir, exist_ok=True)
    roles = find_dti_series(studyInstanceUID)

    dwi_path, instances = _convert_series(
        studyInstanceUID, roles['dwi'], work_dir, 'dwi', with_gradients=True
    )
    shape = nib.load(dwi_path).shape
    if len(shape) != 4 or shape[3] < 7:
        raise ValueError(
            f"The selected diffusion series converts to shape {shape}, which is "
            "not a multi-volume diffusion acquisition. Check that the study's "
            "raw DTI series (not a derived map) was exported."
        )
    log(f"Raw diffusion series: {shape[0]}x{shape[1]}x{shape[2]}, {shape[3]} volumes")

    derived = {}
    for role, basename in (('dwi_fa', 'scanFA'), ('dwi_adc', 'scanADC'),
                           ('dwi_colfa', 'scanColFA')):
        if role in roles:
            derived[basename], _ = _convert_series(
                studyInstanceUID, roles[role], work_dir, basename, with_gradients=False
            )

    # Kept out of `derived`: that dict is the set of scanner maps the gradient
    # reconstruction fits against, and this is raw data. Only used when the
    # raw series turns out to carry no b0 of its own -- see
    # _merge_external_b0() -- so it is converted here, while the study is
    # still being read, rather than sending the pipeline back to dcm4chee.
    b0_path = None
    if 'dwi_b0' in roles:
        b0_path, _ = _convert_series(
            studyInstanceUID, roles['dwi_b0'], work_dir, 'seriesB0',
            with_gradients=False
        )

    reference_instance = instances[0]
    reference_path = os.path.join(work_dir, 'reference.dcm')
    reference_instance.save_as(reference_path)

    return {
        'roles': roles,
        'reference_instance': reference_instance,
        'nominal_b_values': dti_gradients.nominal_b_values(instances),
        'dwi_path': dwi_path,
        'derived': derived,
        'b0_path': b0_path,
        'shape': shape,
    }


### ---------------------------------------------------------------------- ###
### gradient scheme: use what the export kept, reconstruct what it dropped  ###
### ---------------------------------------------------------------------- ###

def _merge_external_b0(work_dir, staged, data):
    """Prepend the study's separately exported b0 series to the raw diffusion
    data as volume 0, and point `staged` at the merged file.

    Some versions of this protocol leave the b=0 measurement out of the raw
    series entirely and export it as its own "..._TENSOR_B0" series -- the raw
    series is then 90 diffusion-weighted volumes and nothing else, and looking
    for a b0 inside it correctly finds none. BET, eddy and dtifit all need
    one, so it is put back where the acquisition would have had it: at the
    front, with b = 0 and a zero gradient vector. That is what the reference
    protocol did by hand for exactly such a study.

    It is sound because the b0 series is not a derived map but the same
    acquisition's own measurement -- same geometry, same coil, same run -- so
    it sits on the diffusion-weighted volumes without registration and eddy
    treats it as the reference volume it is. Both of those claims are checked
    rather than assumed: the geometry against the raw series' affine, and
    "this really is a b0" against the brightness split that failed on the raw
    series alone.

    Returns (merged data, b0 mask over the merged volume axis).
    """
    if not staged.get('b0_path'):
        raise ValueError(
            "This diffusion series contains no b=0 volume, and the study holds "
            "no separate b0 series (Siemens exports it as '..._TENSOR_B0') to "
            "take one from. eddy, BET and dtifit all need a b0; re-export the "
            "study including its b0, or export from the scanner as enhanced "
            "DICOM."
        )

    dwi_image = nib.load(staged['dwi_path'])
    b0_image = nib.load(staged['b0_path'])
    b0 = np.asanyarray(b0_image.dataobj, dtype=np.float32)
    if b0.ndim == 4:
        # More than one b0 was exported in that series: average them, the same
        # way _average_b0() treats the b0 volumes of a raw series that has any.
        b0 = b0.mean(axis=3)

    if b0.shape != data.shape[:3]:
        raise ValueError(
            f"The study's b0 series has shape {b0.shape} but the raw diffusion "
            f"series has {data.shape[:3]} -- they are not from the same "
            "acquisition, so the b0 cannot be merged into it."
        )
    if not np.allclose(b0_image.affine, dwi_image.affine, atol=1e-3):
        raise ValueError(
            "The study's b0 series does not share the raw diffusion series' "
            "geometry, so it is not the same acquisition's b0. Merging it "
            "would put the brain mask and eddy's reference volume a few "
            "millimetres off every diffusion-weighted volume."
        )

    merged = np.concatenate([b0[..., None], data], axis=3)
    # Deliberately not named "dwi...": the work directory is persistent and
    # re-staged on a rerun, and _convert_series() picks the file with the most
    # volumes out of glob("dwi*.nii.gz"). A merged series left over from the
    # previous run would win that and be taken for the raw series.
    merged_path = os.path.join(work_dir, 'merged_dwi.nii.gz')
    image = nib.Nifti1Image(merged, dwi_image.affine, dwi_image.header)
    # Keep the raw series' on-disk type where the values still fit it: the b0
    # comes off the same scanner at the same scaling, so int16 stays exact and
    # the 4D file stays a quarter of what a float one would cost.
    dtype = dwi_image.get_data_dtype()
    if not np.issubdtype(dtype, np.integer) or merged.max() > np.iinfo(dtype).max:
        dtype = np.float32
    image.set_data_dtype(dtype)
    nib.save(image, merged_path)

    is_b0 = dti_gradients.classify_b0_volumes(merged, required=False)
    if is_b0 is None or not is_b0[0] or int(is_b0.sum()) != 1:
        raise ValueError(
            "The study's b0 series does not stand out as a b=0 volume against "
            "the raw series (a b0 is ~3x brighter at b=1000). It is probably "
            "not this acquisition's b0; refusing to build a brain mask and an "
            "eddy reference on it."
        )

    staged['dwi_path'] = merged_path
    staged['shape'] = merged.shape
    staged['b0_merged'] = True
    log(f"The raw series carries no b0; merged the study's b0 series in as "
        f"volume 0 ({merged.shape[3]} volumes total).")
    return merged, is_b0


def resolve_gradient_scheme(work_dir, staged):
    """Produce work_dir/data.bval and work_dir/data.bvec, whatever the export
    left behind, and describe how they were obtained.

    Preferred path: dcm2niix already recovered a real scheme, in which case it
    is copied over unchanged. Fallback: reconstruct it (see dti_gradients.py's
    module docstring), which needs the scanner's derived FA/ADC/ColFA maps and
    is refused without them -- guessing a vendor table instead would silently
    produce wrong tracts, and the reference protocol's central finding is that
    two software versions of the *same named sequence* ship direction tables
    11.7 degrees apart, so no table may be carried between datasets.

    Either way, this is also where a raw series with no b0 volume of its own
    gets the study's separate b0 series merged in (_merge_external_b0), since
    the resulting bvals have to describe the merged volume order and every
    later step reads the series through staged['dwi_path'].
    """
    bvals, bvecs = dti_gradients.load_gradient_files(
        os.path.join(work_dir, 'dwi.bval'), os.path.join(work_dir, 'dwi.bvec')
    )
    bval_path = os.path.join(work_dir, 'data.bval')
    bvec_path = os.path.join(work_dir, 'data.bvec')

    if bvals is not None and dti_gradients.gradients_are_usable(bvals, bvecs):
        log("Gradient scheme read from the DICOM export -- no reconstruction needed.")
        if not np.any(bvals == 0):
            # The scheme is trustworthy and says outright that this raw series
            # has no b0, so the merge does not need the brightness split to
            # discover it -- but it still runs the same checks on what it
            # merges in.
            data = np.asanyarray(nib.load(staged['dwi_path']).dataobj, dtype=np.float32)
            _merge_external_b0(work_dir, staged, data)
            del data
            bvals = np.concatenate([[0.0], bvals])
            bvecs = np.concatenate([np.zeros((3, 1)), bvecs], axis=1)
        np.savetxt(bval_path, bvals[None], fmt='%d')
        np.savetxt(bvec_path, bvecs, fmt='%.6f')
        return {'source': 'DICOM', 'bvals': bvals, 'bvecs': bvecs,
                'n_directions': dti_gradients.count_directions(bvals, bvecs),
                'fit_rms': None}

    log("The export carries no usable gradient scheme (all-zero bval/bvec) -- "
        "reconstructing it from the data.")

    missing = [name for name in ('scanFA', 'scanADC', 'scanColFA')
               if name not in staged['derived']]
    if missing:
        raise ValueError(
            "This study's diffusion series lost its gradient directions in the "
            f"PACS export, and the scanner maps needed to reconstruct them are "
            f"also missing ({', '.join(missing)}). Re-export the study "
            "including its derived FA, ADC and ColFA series, or export from the "
            "scanner as enhanced DICOM so the directions travel in the "
            "MRDiffusionSequence."
        )

    data = np.asanyarray(nib.load(staged['dwi_path']).dataobj, dtype=np.float32)

    b_values = staged['nominal_b_values']
    if not b_values:
        raise ValueError(
            "No b-value survived in this export (neither DiffusionBValue nor "
            "the sequence name), so the diffusion weighting is unknown and the "
            "tensor cannot be scaled. Re-export as enhanced DICOM."
        )
    if len(b_values) > 1:
        # Which volume sits on which shell lived in the same stripped tags as
        # the directions, and the brightness split below only separates b=0
        # from everything else -- so a multi-shell acquisition cannot be
        # reconstructed this way, and guessing would mis-scale every tensor.
        raise ValueError(
            f"This is a multi-shell acquisition (b = "
            f"{', '.join(str(int(b)) for b in b_values)}) whose per-volume "
            "b-values were stripped by the PACS. Which volume belongs to which "
            "shell cannot be recovered from the data. Export the study from the "
            "scanner as enhanced DICOM."
        )
    b_value = b_values[0]

    is_b0 = dti_gradients.classify_b0_volumes(data, required=False)
    if is_b0 is None:
        data, is_b0 = _merge_external_b0(work_dir, staged, data)
    bvals = np.where(is_b0, 0.0, b_value)
    np.savetxt(bval_path, bvals[None], fmt='%d')
    log(f"b-values reconstructed: {int((~is_b0).sum())} volumes at b={b_value:.0f}, "
        f"{int(is_b0.sum())} at b=0")

    del data  # the estimator reloads it; a 4D DWI is several hundred MB

    bvecs, n_directions, fit_rms = dti_gradients.estimate_directions(
        staged['dwi_path'], bvals,
        staged['derived']['scanFA'], staged['derived']['scanADC'],
        staged['derived']['scanColFA'],
    )
    np.savetxt(bvec_path, bvecs, fmt='%.6f')

    return {'source': 'reconstructed', 'bvals': bvals, 'bvecs': bvecs,
            'n_directions': n_directions, 'fit_rms': fit_rms}


def validate_against_scanner_fa(work_dir, staged, mask_path):
    """Correlate an FA map fitted with the resolved gradient scheme against the
    scanner's own FA, in white matter, and return the coefficient.

    Fitted on the *uncorrected* data on purpose: the scanner computed its map
    from the uncorrected series too, so this compares two tensor fits of the
    same voxels and isolates the one thing under test -- the gradient scheme.
    Run against eddy-corrected data the same comparison drops (0.995 -> 0.764
    in the reference protocol) simply because eddy moved the data, which says
    nothing about whether the directions are right.

    Returns None when the study carries no scanner FA to compare against; the
    caller decides whether that is acceptable (it is, if the scheme came from
    the DICOM headers; it is not, if it was reconstructed).
    """
    if 'scanFA' not in staged['derived']:
        return None

    prefix = os.path.join(work_dir, 'dti_raw')
    _run_fsl([
        "dtifit",
        f"--data={staged['dwi_path']}",
        f"--out={prefix}",
        f"--mask={mask_path}",
        f"--bvecs={os.path.join(work_dir, 'data.bvec')}",
        f"--bvals={os.path.join(work_dir, 'data.bval')}",
    ], timeout=_TIMEOUT_SHORT)

    fitted = nib.load(f"{prefix}_FA.nii.gz").get_fdata()
    scanner = nib.load(staged['derived']['scanFA']).get_fdata() / FA_SCALE
    if fitted.shape != scanner.shape:
        log(f"Scanner FA shape {scanner.shape} != fitted {fitted.shape}; skipping validation.")
        return None

    white_matter = (scanner > 0.2) & (fitted > 0)
    if white_matter.sum() < 1000:
        return None
    return float(np.corrcoef(fitted[white_matter], scanner[white_matter])[0, 1])


### ---------------------------------------------------------------------- ###
### the pipeline's FSL steps                                               ###
### ---------------------------------------------------------------------- ###

def _average_b0(dwi_path, bvals, out_path):
    """Mean of every b=0 volume -- markedly less noisy than any single one,
    and what BET and eddy both get pointed at."""
    image = nib.load(dwi_path)
    data = np.asanyarray(image.dataobj, dtype=np.float32)
    mean_b0 = data[..., bvals == 0].mean(axis=3)
    nib.save(nib.Nifti1Image(mean_b0, image.affine, image.header), out_path)
    return out_path


def _brain_mask(nodif_path, out_prefix, fractional_intensity=0.25):
    """BET on the averaged b0. The threshold is the reference protocol's; a b0
    is dimmer and lower-contrast than a T1, so BET's 0.5 default cuts into
    cortex here."""
    _run_fsl(["bet", nodif_path, out_prefix, "-m", "-f", str(fractional_intensity)],
             timeout=_TIMEOUT_SHORT)
    mask_path = f"{out_prefix}_mask.nii.gz"
    if not os.path.exists(mask_path):
        raise RuntimeError("BET produced no brain mask for the averaged b0.")
    return mask_path


def _acquisition_parameters(work_dir, n_volumes):
    """Write eddy's --acqp and --index files, from dcm2niix's sidecar where it
    knows the phase encoding and from the acquisition's usual values where it
    does not.

    With no reverse-phase-encoded b0 there is no topup and hence no
    susceptibility field to estimate, so these values only set the scale of
    eddy's own eddy-current/movement model rather than warping anything: an
    approximate readout time shifts nothing geometrically. The direction still
    matters, since it tells eddy along which axis eddy currents stretch.
    """
    phase_encoding, readout_time = 'j-', 0.05
    sidecar = os.path.join(work_dir, 'dwi.json')
    if os.path.exists(sidecar):
        with open(sidecar) as handle:
            metadata = json.load(handle)
        # PhaseEncodingDirection carries the polarity and is what we want;
        # PhaseEncodingAxis is what dcm2niix falls back to when the CSA header
        # (stripped here) was the only place the polarity lived. Assuming the
        # negative polarity for the bare axis matches the acquisitions in this
        # project (COL / anterior-posterior).
        phase_encoding = (metadata.get('PhaseEncodingDirection')
                          or metadata.get('PhaseEncodingAxis') or 'j')
        if phase_encoding in ('i', 'j', 'k'):
            phase_encoding += '-'
        readout_time = float(metadata.get('TotalReadoutTime', readout_time))

    axis = {'i': (1, 0, 0), 'j': (0, 1, 0), 'k': (0, 0, 1)}[phase_encoding[0]]
    sign = -1 if phase_encoding.endswith('-') else 1
    vector = [sign * component for component in axis]

    acqp_path = os.path.join(work_dir, 'acqp.txt')
    with open(acqp_path, 'w') as handle:
        handle.write(f"{vector[0]} {vector[1]} {vector[2]} {readout_time}\n")

    index_path = os.path.join(work_dir, 'index.txt')
    with open(index_path, 'w') as handle:
        handle.write('\n'.join(['1'] * n_volumes) + '\n')

    log(f"eddy acquisition parameters: phase encoding {phase_encoding}, "
        f"readout {readout_time}s, {n_volumes} volumes")
    return acqp_path, index_path


def _run_eddy(work_dir, dwi_path, mask_path, n_volumes):
    """Motion and eddy-current correction.

    --repol replaces slices eddy identifies as outliers (signal dropout from
    motion during the diffusion gradients) with their model prediction rather
    than leaving them to bias the tensor fit; --cnr_maps produces the SNR/CNR
    maps the metrics table reports.

    Returns the output prefix. The rotated bvecs eddy writes next to it are
    what every later step must use -- see _run_dtifit().
    """
    acqp_path, index_path = _acquisition_parameters(work_dir, n_volumes)
    prefix = os.path.join(work_dir, 'eddy_corrected')
    use_gpu = _gpu_available()
    binary = "eddy_cuda11.0" if use_gpu else "eddy_cpu"
    thread_args = [] if use_gpu else [f"--nthr={_EDDY_CPU_THREADS}"]
    log(f"Running {binary} on {n_volumes} volumes")

    _run_fsl([
        binary,
        f"--imain={dwi_path}",
        f"--mask={mask_path}",
        f"--acqp={acqp_path}",
        f"--index={index_path}",
        f"--bvecs={os.path.join(work_dir, 'data.bvec')}",
        f"--bvals={os.path.join(work_dir, 'data.bval')}",
        "--repol",
        "--cnr_maps",
        *thread_args,
        f"--out={prefix}",
    ], timeout=_TIMEOUT_EDDY)

    if not os.path.exists(f"{prefix}.nii.gz"):
        raise RuntimeError("eddy produced no corrected data.")
    return prefix


def _run_dtifit(work_dir, eddy_prefix, mask_path):
    """Fit the diffusion tensor, and return the output prefix.

    --bvecs must be eddy's *rotated* bvecs, not the original ones: where eddy
    rotated a volume to correct motion, its gradient direction rotated with it.
    Passing the originals is the pipeline's classic silent failure -- the FA
    map looks entirely normal while every direction is systematically wrong,
    so the tensor is fine and the tracts run askew.
    """
    rotated_bvecs = f"{eddy_prefix}.eddy_rotated_bvecs"
    if not os.path.exists(rotated_bvecs):
        raise RuntimeError(f"eddy wrote no rotated bvecs at {rotated_bvecs}.")

    prefix = os.path.join(work_dir, 'dti')
    _run_fsl([
        "dtifit",
        f"--data={eddy_prefix}",
        f"--out={prefix}",
        f"--mask={mask_path}",
        f"--bvecs={rotated_bvecs}",
        f"--bvals={os.path.join(work_dir, 'data.bval')}",
    ], timeout=_TIMEOUT_SHORT)
    return prefix


# bedpostx's crossing-fibre model needs enough independent directions to
# constrain each extra fibre population, or asking for one just buys noise.
# The reference protocol validated 2 fibres at 30 directions (see
# _bedpostx_fibre_target below); the commonly cited rule of thumb for a third
# population is roughly double that. That figure is not validated against
# this project's own data the way the 30-direction one is, so the threshold
# here is deliberately conservative -- prefer 2 fibres over a spurious 3rd
# population if in doubt.
_BEDPOSTX_THREE_FIBRE_MIN_DIRECTIONS = 60


def _bedpostx_fibre_target(n_directions):
    """How many fibre orientations to ask bedpostx for, given the number of
    distinct gradient directions the acquisition actually measured.

    2 below the threshold -- the reference protocol's validated figure, at
    its 30 directions -- 3 at or above it. See
    _BEDPOSTX_THREE_FIBRE_MIN_DIRECTIONS for where that line sits and why.
    An unknown count (no tensor_metrics.json to read, e.g. a work directory
    from before this was tracked) falls back to the validated floor.
    """
    if n_directions is not None and n_directions >= _BEDPOSTX_THREE_FIBRE_MIN_DIRECTIONS:
        return 3
    return 2


def _bedpostx_fibre_count(bedpostx_dir):
    """How many fibre orientations a bedpostX output actually holds, read off
    the merged_f{i}samples files it wrote rather than assumed -- so a report
    reflects reality even when the output was reused from a run made under a
    different fibre-count decision than the current code would make."""
    count = 0
    while os.path.exists(os.path.join(bedpostx_dir, f'merged_f{count + 1}samples.nii.gz')):
        count += 1
    return count


def _run_bedpostx(work_dir, eddy_prefix, mask_path, n_fibres):
    """Estimate up to n_fibres fibre orientations per voxel, and return the
    bedpostX output directory.

    n_fibres is decided by the caller from the acquisition's measured
    direction count (see _bedpostx_fibre_target) -- 30 directions does not
    support a third fibre population, more does. bedpostx insists on its
    input directory holding exactly these four names, the text files without
    extensions -- hence the copies rather than a directory of symlinks to the
    pipeline's own names.

    Reuse is keyed on merged_f{n_fibres}samples.nii.gz specifically, not just
    any bedpostX output: a directory left over from a run made with a
    different fibre-count decision (an older code version, or a study
    reprocessed after more directions were added) would otherwise be reused
    as if it had been run with today's target.
    """
    input_dir = os.path.join(work_dir, 'bpx')
    output_dir = f"{input_dir}.bedpostX"
    samples_path = os.path.join(output_dir, f'merged_f{n_fibres}samples.nii.gz')
    if os.path.exists(samples_path):
        log(f"Reusing existing bedpostX output ({n_fibres} fibres).")
        return output_dir

    os.makedirs(input_dir, exist_ok=True)
    shutil.copyfile(f"{eddy_prefix}.nii.gz", os.path.join(input_dir, 'data.nii.gz'))
    shutil.copyfile(mask_path, os.path.join(input_dir, 'nodif_brain_mask.nii.gz'))
    shutil.copyfile(os.path.join(work_dir, 'data.bval'), os.path.join(input_dir, 'bvals'))
    shutil.copyfile(f"{eddy_prefix}.eddy_rotated_bvecs", os.path.join(input_dir, 'bvecs'))

    _run_fsl(["bedpostx_datacheck", input_dir], timeout=_TIMEOUT_SHORT)

    binary = "bedpostx_gpu" if _gpu_available() else "bedpostx"
    _run_fsl([binary, input_dir, "-n", str(n_fibres), "-model", "2"], timeout=_TIMEOUT_BEDPOSTX)

    if not os.path.exists(samples_path):
        raise RuntimeError(
            f"bedpostx finished without producing {n_fibres} fibre orientations in "
            f"{output_dir}. See the bedpostX logs directory for the per-slice job output."
        )
    return output_dir


def _register_to_standard(work_dir, dti_prefix, bedpostx_dir, nodif_brain_path):
    """Register FA to standard space and write the six transform files XTRACT
    expects into <bedpostX>/xfms.

    With no T1 in the export the usual diffusion -> T1 -> MNI chain is
    unavailable, so FA goes straight to FMRIB58_FA_1mm -- the same route TBSS
    takes, and valid here because FMRIB58_FA_1mm shares MNI152_T1_1mm's affine,
    so the XTRACT protocols' standard-space seeds land correctly.

    XTRACT still wants all six files, so with diffusion and "structural" space
    collapsed into one, diff2str and str2diff become the identity.

    This is coarser than a route through a T1: FA is 2mm rather than 1mm, has
    far less anatomical contrast, and there is no grey/white boundary for
    epi_reg -bbr to lock onto. Good enough for atlas-based tractography; not
    good enough to localise an individual lesion against. Returns the
    correlation with the template, which the metrics table reports so the
    reader can see how well it went.
    """
    affine_path = os.path.join(work_dir, 'diff2standard_aff.mat')
    warp_path = os.path.join(work_dir, 'diff2standard_warp')
    fa_path = f"{dti_prefix}_FA"

    if os.path.exists(f"{warp_path}.nii.gz") and os.path.exists(affine_path):
        log("Reusing the existing FA-to-standard registration.")
    else:
        _run_fsl(["flirt", "-in", fa_path, "-ref", STANDARD_FA,
                  "-omat", affine_path, "-dof", "12"], timeout=_TIMEOUT_SHORT)
        _run_fsl(["fnirt", f"--in={fa_path}", f"--aff={affine_path}",
                  f"--cout={warp_path}", "--config=FA_2_FMRIB58_1mm"],
                 timeout=_TIMEOUT_BEDPOSTX)

    xfms_dir = os.path.join(bedpostx_dir, 'xfms')
    os.makedirs(xfms_dir, exist_ok=True)
    identity = os.path.join(xfms_dir, 'diff2str.mat')
    with open(identity, 'w') as handle:
        handle.write("1 0 0 0\n0 1 0 0\n0 0 1 0\n0 0 0 1\n")
    shutil.copyfile(identity, os.path.join(xfms_dir, 'str2diff.mat'))
    shutil.copyfile(affine_path, os.path.join(xfms_dir, 'str2standard.mat'))
    _run_fsl(["convert_xfm", "-omat", os.path.join(xfms_dir, 'standard2str.mat'),
              "-inverse", affine_path], timeout=_TIMEOUT_SHORT)

    _run_fsl(["convertwarp", f"--ref={STANDARD_T1_BRAIN}", f"--warp1={warp_path}",
              f"--out={os.path.join(xfms_dir, 'diff2standard')}"], timeout=_TIMEOUT_SHORT)
    _run_fsl(["invwarp", "-w", os.path.join(xfms_dir, 'diff2standard'),
              "-o", os.path.join(xfms_dir, 'standard2diff'),
              "-r", nodif_brain_path], timeout=_TIMEOUT_SHORT)

    # Registration quality, on the same intensity correlation the reference
    # protocol reported (it reached r = 0.867 without a T1).
    warped = os.path.join(work_dir, 'dti_FA_standard')
    _run_fsl(["applywarp", f"--in={fa_path}", f"--ref={STANDARD_FA}",
              f"--warp={os.path.join(xfms_dir, 'diff2standard')}",
              f"--out={warped}"], timeout=_TIMEOUT_SHORT)

    moved = nib.load(f"{warped}.nii.gz").get_fdata()
    template = nib.load(f"{STANDARD_FA}.nii.gz").get_fdata()
    overlap = (moved > 0) & (template > 0)
    correlation = (float(np.corrcoef(moved[overlap], template[overlap])[0, 1])
                   if overlap.sum() > 1000 else None)
    log(f"FA-to-FMRIB58 registration correlation: {correlation}")
    return correlation


def _xtract_is_complete(output_dir):
    """Whether an existing XTRACT output directory holds a density map for
    every protocol of the species.

    Checked against XTRACT's own protocol library rather than against a marker
    file, for the reason _run_bedpostx() checks its outputs: a run cut short
    (a restarted container, the timeout, a full disk) leaves a directory that
    looks finished but holds a subset of the tracts, and reusing that would
    upload a partial result as if it were the whole atlas. The per-tract
    directories are no evidence either -- XTRACT creates all 42 of them before
    it starts tracking -- so only densityNorm.nii.gz counts.
    """
    tracts_dir = os.path.join(output_dir, 'tracts')
    if not (os.path.isdir(tracts_dir) and os.path.isdir(XTRACT_PROTOCOLS)):
        return False

    protocols = [name for name in os.listdir(XTRACT_PROTOCOLS)
                 if os.path.isdir(os.path.join(XTRACT_PROTOCOLS, name))]
    missing = [name for name in protocols
               if not os.path.exists(os.path.join(tracts_dir, name, 'densityNorm.nii.gz'))]
    if missing:
        log(f"Existing XTRACT output is incomplete ({len(missing)} of "
            f"{len(protocols)} tracts have no density map) -- rerunning it.")
        return False
    return True


def _run_xtract(work_dir, bedpostx_dir):
    """Run XTRACT's 42 standard-space human tract protocols, returning the
    output directory. Roughly 16 minutes on a shared A100.

    Reuses a complete previous run, so re-uploading a study's tracts (or
    picking up after a failure further down) costs seconds instead of the full
    tracking -- the same reasoning as _run_bedpostx()'s reuse.
    """
    output_dir = os.path.join(work_dir, 'xtract_out')
    if _xtract_is_complete(output_dir):
        log("Reusing existing XTRACT output.")
        return output_dir

    # XTRACT normalises every *bidirectional* tract by piping the two
    # directions' waytotals through bc and dividing the summed density by the
    # result. Without bc the shell writes an empty sum_waytotal, fslmaths then
    # runs with a missing operand, and XTRACT still exits 0 -- so the run looks
    # successful while 16 of its 42 tracts (both optic radiations among them)
    # have no density map. Refusing up front turns that into a message instead
    # of an hour of tracking with a quietly halved result.
    if not shutil.which("bc"):
        raise RuntimeError(
            "XTRACT needs `bc` to normalise its bidirectional tracts, and it is "
            "not installed in this container. Without it 16 of the 42 tracts "
            "would silently come out empty. Install it (see API/Dockerfile) and "
            "run this again."
        )

    command = ["xtract", "-bpx", bedpostx_dir, "-out", output_dir, "-species", "HUMAN"]
    if _gpu_available():
        command.append("-gpu")
    _run_fsl(command, timeout=_TIMEOUT_XTRACT)

    if not os.path.isdir(os.path.join(output_dir, 'tracts')):
        raise RuntimeError(f"XTRACT produced no tracts directory under {output_dir}.")
    return output_dir


### ---------------------------------------------------------------------- ###
### products: colour maps, QC renders, metrics                             ###
### ---------------------------------------------------------------------- ###

def colour_fa(fa, v1):
    """Direction-encoded colour FA as a (X, Y, Z, 3) uint8 volume.

    The standard neuroradiological convention: hue is the absolute principal
    eigenvector (red left-right, green anterior-posterior, blue
    superior-inferior), brightness is FA. This is the map to look at to judge
    whether the gradient scheme is right at all -- with wrong or unrotated
    directions the corpus callosum stops being red and the corticospinal
    tract stops being blue, which no FA map would reveal.
    """
    rgb = np.abs(v1) * np.clip(fa, 0, 1)[..., None]
    return (np.clip(rgb, 0, 1) * 255).astype(np.uint8)


def _slice_positions(mask, count=12):
    """Evenly spaced slice indices spanning the mask along the acquisition's
    slice axis (the third array axis for the axial DTI acquisitions this
    pipeline sees), for a QC montage."""
    present = np.where(mask.any(axis=(0, 1)))[0]
    if not len(present):
        present = np.arange(mask.shape[2])
    return np.linspace(present[0], present[-1], min(count, len(present))).astype(int)


def _greyscale_slice(volume, index, percentiles=(1, 99)):
    """One slice of a scalar volume as an RGB uint8 image, windowed on the
    volume's own percentiles and flipped into radiological display order."""
    finite = volume[np.isfinite(volume) & (volume > 0)]
    low, high = (np.percentile(finite, percentiles) if finite.size
                 else (0.0, 1.0))
    if high <= low:
        high = low + 1e-6
    plane = np.clip((volume[:, :, index].T - low) / (high - low), 0, 1)
    return np.stack([(plane * 255).astype(np.uint8)] * 3, axis=-1)[::-1]


def _montage(images, columns=4):
    """Tile equally-shaped RGB slices into one grid, so a QC set travels as a
    single image instead of a dozen loose instances."""
    rows = int(np.ceil(len(images) / columns))
    height, width = images[0].shape[:2]
    grid = np.zeros((rows * height, columns * width, 3), dtype=np.uint8)
    for i, image in enumerate(images):
        r, c = divmod(i, columns)
        grid[r * height:(r + 1) * height, c * width:(c + 1) * width] = image
    return grid


def _overlay_slice(base_rgb, overlay, index, colour, threshold, opacity=0.75):
    """Alpha-blend one overlay volume's slice onto an already-rendered
    greyscale slice, with the blend weight following the overlay's own value
    so a tract fades out at its margins instead of ending in a hard edge."""
    plane = overlay[:, :, index].T[::-1]
    maximum = float(plane.max())
    if maximum <= 0:
        return base_rgb
    weight = np.clip(plane / maximum, 0, 1)
    weight[plane < threshold] = 0
    weight = (weight * opacity)[..., None]
    return (base_rgb * (1 - weight) + np.array(colour, dtype=np.float32) * weight
            ).astype(np.uint8)


def _tensor_qc_images(nodif, mask, fa, colour_fa_volume):
    """QC montages for the tensor stage: brain mask on the averaged b0, the FA
    map, and the direction-encoded colour FA."""
    positions = _slice_positions(mask > 0)

    mask_images, fa_images, colour_images = [], [], []
    for index in positions:
        base = _greyscale_slice(nodif, index)
        mask_images.append(_overlay_slice(base, mask.astype(np.float32), index,
                                          (80, 220, 80), threshold=0.5, opacity=0.35))
        fa_images.append(_greyscale_slice(fa, index, percentiles=(1, 99.5)))
        colour_images.append(colour_fa_volume[:, :, index].transpose(1, 0, 2)[::-1])

    return [_montage(mask_images), _montage(fa_images), _montage(colour_images)]


def _eddy_quality(eddy_prefix, mask_path, bvals):
    """Parse eddy's own quality outputs into the numbers the metrics table
    reports: how much the head moved, how many slices had to be replaced, and
    the signal-to-noise the tensor fit had to work with.

    Every value is read from a file eddy wrote; nothing here is recomputed
    from the data, so these are eddy's own estimates rather than a second
    opinion on them.
    """
    metrics = {}

    movement_path = f"{eddy_prefix}.eddy_movement_rms"
    if os.path.exists(movement_path):
        # Column 0 is total RMS displacement relative to the first volume
        # (column 1 is relative to the previous volume). This is FSL's own
        # summary of how much the head moved over the acquisition.
        movement = np.atleast_2d(np.loadtxt(movement_path))[:, 0]
        metrics['displacement_median_mm'] = float(np.median(movement))
        metrics['displacement_max_mm'] = float(np.max(movement))

    parameters_path = f"{eddy_prefix}.eddy_parameters"
    if os.path.exists(parameters_path):
        # Columns 0-2 are per-axis translations in mm, 3-5 rotations in
        # radians. Translations are summarised per axis rather than as a
        # per-volume norm because that is the figure the reference protocol
        # reports (0.21 mm median / 0.71 mm max on its own data), so a reader
        # holding that document can compare directly.
        parameters = np.atleast_2d(np.loadtxt(parameters_path))
        translations = np.abs(parameters[:, :3])
        metrics['translation_median_mm'] = float(np.median(translations))
        metrics['translation_max_mm'] = float(translations.max())
        metrics['rotation_max_deg'] = float(np.degrees(np.abs(parameters[:, 3:6]).max()))

    outlier_path = f"{eddy_prefix}.eddy_outlier_report"
    if os.path.exists(outlier_path):
        with open(outlier_path) as handle:
            replaced = sum(1 for line in handle if line.strip())
        n_slices = nib.load(f"{eddy_prefix}.nii.gz").shape[2]
        metrics['outlier_slices'] = replaced
        metrics['total_slices'] = int(n_slices * len(bvals))

    cnr_path = f"{eddy_prefix}.eddy_cnr_maps.nii.gz"
    if os.path.exists(cnr_path):
        # Volume 0 is the b0 SNR map, each following volume the CNR of one
        # diffusion-weighted shell.
        cnr = np.asanyarray(nib.load(cnr_path).dataobj, dtype=np.float32)
        mask = nib.load(mask_path).get_fdata() > 0
        # Median, not mean: the CNR maps have a long bright tail at the brain
        # edge and in CSF that pulls a mean up by ~20%. The median is also
        # what the reference protocol quotes (SNR 30.3, CNR 1.15).
        metrics['snr_b0'] = float(np.median(cnr[..., 0][mask]))
        if cnr.shape[-1] > 1:
            shells = sorted({int(b) for b in bvals if b > 0})
            for i, shell in enumerate(shells, start=1):
                if i < cnr.shape[-1]:
                    metrics[f'cnr_b{shell}'] = float(np.median(cnr[..., i][mask]))

    return metrics


def _tensor_metrics_table(staged, gradients, fa_correlation, eddy_metrics, fa, mask):
    """The tensor stage's one-page summary: how the gradient scheme was
    obtained and how well it validated, what eddy had to correct, and the FA
    the fit produced. Everything a reader needs to decide whether to trust the
    maps sitting next to it in the study."""
    white_matter = (fa > 0.2) & (mask > 0)
    rows = [
        ('Acquisition', f"{staged['shape'][0]}x{staged['shape'][1]}x{staged['shape'][2]}, "
                        f"{staged['shape'][3]} volumes", ''),
        ('b-values', ', '.join(str(int(b)) for b in sorted(set(gradients['bvals']))),
         f"{int((gradients['bvals'] == 0).sum())} b=0 volumes"
         + (", merged in from the study's separate b0 series"
            if staged.get('b0_merged') else '')),
        ('Gradient directions',
         f"read from DICOM ({gradients['n_directions']} directions)"
         if gradients['source'] == 'DICOM'
         else f"reconstructed from scanner FA/ADC/ColFA ({gradients['n_directions']} directions)",
         '' if gradients['source'] == 'DICOM'
         else f"fit RMS {gradients['fit_rms']:.4f}"),
        ('FA vs scanner FA (white matter)',
         'n/a - no scanner FA in study' if fa_correlation is None else f"r = {fa_correlation:.4f}",
         'fitted on uncorrected data, as the scanner map was'),
    ]

    if 'translation_median_mm' in eddy_metrics:
        rows.append(('Translation (median / max per axis)',
                     f"{eddy_metrics['translation_median_mm']:.2f} / "
                     f"{eddy_metrics['translation_max_mm']:.2f} mm", 'eddy estimate'))
    if 'displacement_median_mm' in eddy_metrics:
        rows.append(('Total displacement (median / max)',
                     f"{eddy_metrics['displacement_median_mm']:.2f} / "
                     f"{eddy_metrics['displacement_max_mm']:.2f} mm",
                     'RMS relative to the first volume'))
    if 'rotation_max_deg' in eddy_metrics:
        rows.append(('Rotation (max)', f"{eddy_metrics['rotation_max_deg']:.2f} deg",
                     'eddy estimate'))
    if 'outlier_slices' in eddy_metrics:
        rows.append(('Outlier slices replaced',
                     f"{eddy_metrics['outlier_slices']} of {eddy_metrics['total_slices']}",
                     'eddy --repol'))
    if 'snr_b0' in eddy_metrics:
        rows.append(('SNR (b=0)', f"{eddy_metrics['snr_b0']:.1f}", 'eddy CNR map'))
    for key, value in eddy_metrics.items():
        if key.startswith('cnr_b'):
            rows.append((f"CNR ({key[4:]})", f"{value:.2f}", 'eddy CNR map'))

    rows.append(('Mean FA in white matter (FA > 0.2)',
                 f"{float(fa[white_matter].mean()):.3f}" if white_matter.any() else 'n/a', ''))
    rows.append(('Susceptibility distortion', 'not corrected',
                 'needs a reverse phase-encoded b0 (topup)'))

    return pd.DataFrame(rows, columns=['Metric', 'Value', 'Note'])


### ---------------------------------------------------------------------- ###
### products: tracts                                                       ###
### ---------------------------------------------------------------------- ###

def _tract_table(xtract_dir, fa_standard_path, md_standard_path):
    """Per-tract extent and diffusion metrics for every XTRACT protocol.

    Computed in standard space, where the tract densities already live, by
    warping FA and MD there once rather than warping 42 tracts back -- the
    numbers are identical either way and this is two resamples instead of
    forty-two.

    An empty tract is reported as such rather than omitted: "XTRACT found
    nothing here" is a finding a reader needs to see, and it does not mean the
    bundle is interrupted -- streamlines stop wherever FA drops or fibres
    cross, and thin structures like the fornix are barely resolvable at 2mm.
    """
    fa = nib.load(fa_standard_path).get_fdata()
    md = nib.load(md_standard_path).get_fdata()

    rows = []
    tracts_dir = os.path.join(xtract_dir, 'tracts')
    for name in sorted(os.listdir(tracts_dir)):
        density_path = os.path.join(tracts_dir, name, 'densityNorm.nii.gz')
        if not os.path.exists(density_path):
            # XTRACT made the directory but never wrote a density map, so its
            # tracking or its normalisation failed. Reported rather than
            # dropped: a quietly shorter table reads as "these are all the
            # tracts there are", which is exactly how a container without bc
            # cost 16 of 42 tracts while the run reported success.
            rows.append((name, 0, '', '', 'no output - XTRACT step failed'))
            continue
        density = nib.load(density_path).get_fdata()
        maximum = float(density.max())
        if maximum <= 0:
            rows.append((name, 0, '', '', 'empty'))
            continue
        extent = density > _TRACT_THRESHOLD_FRACTION * maximum
        rows.append((
            name, int(extent.sum()),
            f"{float(fa[extent].mean()):.3f}" if fa.shape == density.shape else '',
            f"{float(md[extent].mean() * 1000):.3f}" if md.shape == density.shape else '',
            '',
        ))

    table = pd.DataFrame(rows, columns=['Tract', 'Voxels', 'Mean FA',
                                        'Mean MD (1e-3 mm2/s)', 'Note'])
    return table.sort_values('Voxels', ascending=False, ignore_index=True)


### ---------------------------------------------------------------------- ###
### products: whole-brain fibre viewer (not part of XTRACT's atlas result) ###
### ---------------------------------------------------------------------- ###

def _streamline_cache_path(work_dir):
    return os.path.join(work_dir, 'streamlines.npz')


def _streamline_viewer_path(work_dir):
    return os.path.join(work_dir, 'streamlines_3d.html')


def _streamline_vtk_path(work_dir):
    return os.path.join(work_dir, 'streamlines.vtk')


def _clear_streamline_cache(work_dir):
    """Drop a previous run's streamlines -- for when the tensor they were
    tracked on has just been refit, which the reuse check in
    _generate_streamline_cache cannot tell from file existence alone."""
    for path in (_streamline_cache_path(work_dir), _streamline_viewer_path(work_dir),
                 _streamline_vtk_path(work_dir)):
        if os.path.exists(path):
            os.remove(path)


def _generate_streamline_cache(work_dir, dti_prefix, mask_path, n_directions):
    """Deterministic whole-brain streamlines from this run's own tensor fit
    (see dti_streamlines.py), cached to work_dir so a later reupload/rebuild
    doesn't re-track a picture-only result that took no FSL/GPU time to
    begin with but is still seconds, not free.

    Writes the interactive viewer (streamlines_3d.html, served by
    dti_files_route), a legacy-VTK export (streamlines.vtk, for 3D Slicer or
    any other tool that reads polydata -- see dti_streamlines.write_
    streamline_vtk) and the raw points (streamlines.npz, read back by
    _streamline_views for the glass-brain DICOM upload), so any of the three
    can be rebuilt from the cache without re-tracking.

    Returns whether it tracked anew, so a caller reusing an earlier tensor
    run knows whether the fibre-field series still has to be uploaded or is
    already in the study.
    """
    cache_path = _streamline_cache_path(work_dir)
    if (os.path.exists(cache_path) and os.path.exists(_streamline_viewer_path(work_dir))
            and os.path.exists(_streamline_vtk_path(work_dir))):
        log("Reusing existing whole-brain streamlines.")
        return False

    fa_image = nib.load(f"{dti_prefix}_FA.nii.gz")
    fa = fa_image.get_fdata()
    v1 = nib.load(f"{dti_prefix}_V1.nii.gz").get_fdata()
    mask = nib.load(mask_path).get_fdata()

    streamlines = dti_streamlines.generate_whole_brain_streamlines(
        fa, v1, mask, fa_image.affine,
        seed_count=_STREAMLINE_SEED_COUNT,
        seed_fa_threshold=_STREAMLINE_SEED_FA_THRESHOLD,
    )
    if not streamlines:
        log("No whole-brain streamlines reconstructed (FA never cleared "
            f"{_STREAMLINE_SEED_FA_THRESHOLD} inside the brain mask) -- skipping "
            "the fibre-field viewer and image.")
        return False

    log(f"Tracked {len(streamlines)} whole-brain streamlines for the fibre-field viewer.")
    np.savez(cache_path, streamlines=np.array(streamlines, dtype=object))
    dti_streamlines.write_streamline_viewer_html(
        streamlines, _streamline_viewer_path(work_dir),
        title='Fibre Field', subtitle='whole-brain DTI tractography',
        n_directions=n_directions, seed_fa_threshold=_STREAMLINE_SEED_FA_THRESHOLD,
    )
    dti_streamlines.write_streamline_vtk(streamlines, _streamline_vtk_path(work_dir))
    return True


# A full turntable rather than a few fixed angles: uploaded as an ordinary
# DICOM image series, scrolling through these in OHIF plays as a rotation
# around the z (superior-inferior) axis -- the closest thing to the
# interactive viewer OHIF's own stack scroll can give, since it has no
# polydata/streamline viewport (see the "similar format for OHIF" thread).
_STREAMLINE_TURNTABLE_ELEVATION = 20.0
_STREAMLINE_TURNTABLE_STEPS = 36


def _streamline_views(work_dir):
    """(images, streamline count) for the cached streamlines' turntable
    rotation, for the DICOM upload -- or (None, 0) if this run has no cached
    streamlines (an FA field too low to seed from, or a study whose DTI run
    predates this feature). Re-rendered from the cache each
    call rather than cached themselves: a few seconds for the full
    turntable, cheap next to everything else in this stage."""
    cache_path = _streamline_cache_path(work_dir)
    if not os.path.exists(cache_path):
        return None, 0
    with np.load(cache_path, allow_pickle=True) as data:
        streamlines = list(data['streamlines'])
    if not streamlines:
        return None, 0
    step = 360.0 / _STREAMLINE_TURNTABLE_STEPS
    views = tuple((_STREAMLINE_TURNTABLE_ELEVATION, i * step)
                  for i in range(_STREAMLINE_TURNTABLE_STEPS))
    return dti_streamlines.render_glass_brain_views(streamlines, views=views), len(streamlines)


def _warp_tract_to_diffusion(work_dir, xfms_dir, xtract_dir, name, reference):
    """Bring one tract's standard-space density back into the diffusion space
    the FA series was uploaded in, so an overlay lines up with it slice for
    slice in the viewer."""
    output = os.path.join(work_dir, 'tracts_diff', name)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    _run_fsl([
        "applywarp",
        f"--in={os.path.join(xtract_dir, 'tracts', name, 'densityNorm.nii.gz')}",
        f"--ref={reference}",
        f"--warp={os.path.join(xfms_dir, 'standard2diff')}",
        f"--out={output}",
    ], timeout=_TIMEOUT_SHORT)
    return nib.load(f"{output}.nii.gz").get_fdata()


def _tract_overlay_volume(fa, tracts):
    """An RGB volume of FA in greyscale with each (density, colour) tract
    blended in, ready to upload as a colour DICOM series.

    Tracts are blended in the order given and later ones win where they
    overlap, so callers pass the strongest last.
    """
    positive = fa[fa > 0]
    ceiling = float(np.percentile(positive, 99.5)) if positive.size else 1.0
    base = np.clip(fa / max(ceiling, 1e-6), 0, 1)
    rgb = np.repeat((base * 255).astype(np.float32)[..., None], 3, axis=-1)

    for density, colour in tracts:
        maximum = float(density.max())
        if maximum <= 0:
            continue
        weight = np.clip(density / maximum, 0, 1)
        weight[density < _TRACT_THRESHOLD_FRACTION * maximum] = 0
        weight = weight[..., None]
        rgb = rgb * (1 - weight) + np.array(colour, dtype=np.float32) * weight

    return np.clip(rgb, 0, 255).astype(np.uint8)


### ---------------------------------------------------------------------- ###
### pure local-file compute -- no dcm4chee access below this point         ###
### ---------------------------------------------------------------------- ###

def dti_tensor_core(work_dir, staged):
    """Everything from the staged NIfTI to the fitted tensor, on local files
    only: gradient scheme, brain mask, its validation, eddy, dtifit.

    Aborts before eddy if a *reconstructed* gradient scheme fails to reproduce
    the scanner's own FA map. That check is the only thing standing between a
    stripped export and a confidently wrong tensor, so it is a hard gate
    rather than a warning -- an FA map that looks entirely normal is exactly
    what wrong directions produce.

    Returns the maps, the QC renders, the metrics table and the file paths the
    tractography stage picks up from.
    """
    gradients = resolve_gradient_scheme(work_dir, staged)
    bvals = gradients['bvals']

    nodif_path = _average_b0(staged['dwi_path'], bvals,
                             os.path.join(work_dir, 'nodif.nii.gz'))
    nodif_brain_path = os.path.join(work_dir, 'nodif_brain')
    mask_path = _brain_mask(nodif_path, nodif_brain_path)

    fa_correlation = validate_against_scanner_fa(work_dir, staged, mask_path)
    if gradients['source'] == 'reconstructed':
        if fa_correlation is None:
            raise ValueError(
                "The gradient directions had to be reconstructed but could not "
                "be validated -- the study has no scanner FA map to check them "
                "against. Refusing to produce a tensor that cannot be verified."
            )
        if fa_correlation < dti_gradients.MIN_FA_CORRELATION:
            raise ValueError(
                f"The reconstructed gradient directions reproduce the scanner's "
                f"own FA map at only r = {fa_correlation:.3f} (need "
                f"{dti_gradients.MIN_FA_CORRELATION}). The reconstruction is "
                "not trustworthy for this acquisition; export the study from "
                "the scanner as enhanced DICOM so the real directions travel "
                "with it."
            )
        log(f"Reconstructed gradient scheme validated: r = {fa_correlation:.4f}")

    eddy_prefix = _run_eddy(work_dir, staged['dwi_path'], mask_path, len(bvals))
    dti_prefix = _run_dtifit(work_dir, eddy_prefix, mask_path)

    fa_image = nib.load(f"{dti_prefix}_FA.nii.gz")
    fa = fa_image.get_fdata()
    md = nib.load(f"{dti_prefix}_MD.nii.gz").get_fdata()
    v1 = nib.load(f"{dti_prefix}_V1.nii.gz").get_fdata()
    mask = nib.load(mask_path).get_fdata()
    nodif = nib.load(nodif_path).get_fdata()

    colour = colour_fa(fa, v1)
    eddy_metrics = _eddy_quality(eddy_prefix, mask_path, bvals)

    # The tensor was just refit, so any streamlines on disk were tracked on
    # a previous fit and must not be reused.
    _clear_streamline_cache(work_dir)
    _generate_streamline_cache(work_dir, dti_prefix, mask_path, gradients['n_directions'])
    streamline_views, n_streamlines = _streamline_views(work_dir)

    result = {
        'affine': fa_image.affine,
        'fa': fa,
        'md': md,
        'colour_fa': colour,
        'qc_images': _tensor_qc_images(nodif, mask, fa, colour),
        'metrics_table': _tensor_metrics_table(staged, gradients, fa_correlation,
                                               eddy_metrics, fa, mask),
        'gradient_source': gradients['source'],
        'fa_correlation': fa_correlation,
        'eddy_metrics': eddy_metrics,
        'dti_prefix': dti_prefix,
        'eddy_prefix': eddy_prefix,
        'mask_path': mask_path,
        'nodif_brain_path': nodif_brain_path,
        'streamline_views': streamline_views,
        'n_streamlines': n_streamlines,
    }

    # Persist how the tensor was obtained, so the tractography stage can put
    # it in its own report even when it reuses this run's outputs rather than
    # producing them (see _tractography_quality_table). Anyone reading a tract
    # overlay needs to know whether the gradient directions behind it were
    # read from the headers or reconstructed.
    with open(os.path.join(work_dir, 'tensor_metrics.json'), 'w') as handle:
        json.dump({
            'gradient_source': gradients['source'],
            'fa_correlation': fa_correlation,
            'eddy_metrics': eddy_metrics,
            'n_volumes': int(len(bvals)),
            'n_directions': gradients['n_directions'],
        }, handle, indent=2)

    return result


def dti_tractography_core(work_dir):
    """bedpostx, registration to standard space and XTRACT, on the tensor
    stage's outputs in work_dir.

    Kept separate from dti_tensor_core() because it costs another 45-60
    minutes and answers a different question: the tensor stage says how the
    tissue diffuses, this one says which bundles a probabilistic model can
    follow through it. Each of its three expensive steps reuses a complete
    previous result (bedpostX, the registration, XTRACT), so a rerun after a
    failure -- or purely to rebuild the uploads -- costs only what actually
    has to be redone.
    """
    dti_prefix = os.path.join(work_dir, 'dti')
    eddy_prefix = os.path.join(work_dir, 'eddy_corrected')
    mask_path = os.path.join(work_dir, 'nodif_brain_mask.nii.gz')
    nodif_brain_path = os.path.join(work_dir, 'nodif_brain.nii.gz')

    n_directions = None
    tensor_metrics_path = os.path.join(work_dir, 'tensor_metrics.json')
    if os.path.exists(tensor_metrics_path):
        with open(tensor_metrics_path) as handle:
            n_directions = json.load(handle).get('n_directions')
    n_fibres = _bedpostx_fibre_target(n_directions)

    bedpostx_dir = _run_bedpostx(work_dir, eddy_prefix, mask_path, n_fibres)
    registration_r = _register_to_standard(work_dir, dti_prefix, bedpostx_dir,
                                           nodif_brain_path)
    xtract_dir = _run_xtract(work_dir, bedpostx_dir)

    # The fibre field belongs to the tensor stage now (see dti_tensor_core);
    # this only catches up a study whose tensor outputs predate that, and is
    # a cache hit otherwise. Reported back so the wrapper uploads the series
    # only when it is new, not a second copy of the tensor stage's.
    fibre_field_is_new = _generate_streamline_cache(work_dir, dti_prefix, mask_path,
                                                    n_directions)

    result = _tract_products(work_dir, bedpostx_dir, xtract_dir, registration_r)
    result['fibre_field_is_new'] = fibre_field_is_new
    return result


def _tract_products(work_dir, bedpostx_dir, xtract_dir, registration_r=None):
    """Turn a finished XTRACT run into the overlays and tables that get
    uploaded, reading everything else back off work_dir.

    Separate from dti_tractography_core() so the products can be rebuilt
    without repeating the tracking -- what dti_reupload_wrapper() does when a
    study's series have to be written again. Warping the tracts back into
    diffusion space is seconds each; bedpostx and XTRACT are an hour.
    """
    dti_prefix = os.path.join(work_dir, 'dti')

    # FA is already in standard space from the registration check; MD follows
    # it there so the per-tract table can be computed without warping tracts.
    md_standard = os.path.join(work_dir, 'dti_MD_standard')
    if not os.path.exists(f"{md_standard}.nii.gz"):
        _run_fsl(["applywarp", f"--in={dti_prefix}_MD", f"--ref={STANDARD_FA}",
                  f"--warp={os.path.join(bedpostx_dir, 'xfms', 'diff2standard')}",
                  f"--out={md_standard}"], timeout=_TIMEOUT_SHORT)

    table = _tract_table(xtract_dir, os.path.join(work_dir, 'dti_FA_standard.nii.gz'),
                         f"{md_standard}.nii.gz")

    tensor_metrics_path = os.path.join(work_dir, 'tensor_metrics.json')
    tensor_metrics = {}
    if os.path.exists(tensor_metrics_path):
        with open(tensor_metrics_path) as handle:
            tensor_metrics = json.load(handle)
    reconstructed = table[table['Voxels'] > 0]
    log(f"XTRACT reconstructed {len(reconstructed)} of {len(table)} tracts")

    fa_image = nib.load(f"{dti_prefix}_FA.nii.gz")
    fa = fa_image.get_fdata()
    xfms_dir = os.path.join(bedpostx_dir, 'xfms')

    # Combined overlay: the strongest tracts, each in its own colour. Drawn
    # weakest first so the strongest bundle stays visible where they overlap.
    combined_names = list(reconstructed['Tract'].head(len(_TRACT_COLOURS)))
    layers = []
    for rank, name in enumerate(combined_names):
        colour = _TRACT_COLOURS[rank]
        density = _warp_tract_to_diffusion(work_dir, xfms_dir, xtract_dir, name,
                                           f"{dti_prefix}_FA")
        layers.append((density, colour))
    combined_overlay = (_tract_overlay_volume(fa, list(reversed(layers)))
                        if layers else None)

    # The overlay series is one image with several tracts in it, so the only
    # place its colour coding can be read off is the table uploaded next to it.
    colours = {name: 'RGB {}, {}, {}'.format(*_TRACT_COLOURS[rank])
               for rank, name in enumerate(combined_names)}
    table.insert(1, 'Overlay colour', table['Tract'].map(colours).fillna(''))

    highlights = []
    for name, description in TRACT_HIGHLIGHTS:
        row = table[table['Tract'] == name]
        if row.empty or int(row.iloc[0]['Voxels']) == 0:
            log(f"Skipping highlight tract '{name}': empty or not produced.")
            continue
        density = _warp_tract_to_diffusion(work_dir, xfms_dir, xtract_dir, name,
                                           f"{dti_prefix}_FA")
        highlights.append({
            'name': name,
            'description': description,
            'volume': _tract_overlay_volume(fa, [(density, (255, 60, 60))]),
        })

    return {
        'affine': fa_image.affine,
        'tensor_metrics': tensor_metrics,
        'tract_table': table,
        'n_reconstructed': int(len(reconstructed)),
        'n_protocols': int(len(table)),
        'registration_r': registration_r,
        'combined_overlay': combined_overlay,
        'highlights': highlights,
        'xtract_dir': xtract_dir,
        'n_fibres': _bedpostx_fibre_count(bedpostx_dir),
    }


### ---------------------------------------------------------------------- ###
### wrappers: stage-in from dcm4chee -> core -> stage-out to dcm4chee       ###
### ---------------------------------------------------------------------- ###

def _work_dir(studyInstanceUID):
    """One directory per study, shared by both stages.

    The tractography stage is 45-60 minutes of work on top of the tensor
    stage's outputs, so it reads them from here rather than recomputing them,
    and a rerun after a failed XTRACT reuses the bedpostX directory too. The
    intermediates are large (a corrected 4D series is >100 MB) and live under
    API_storage, which is the persistent mount.
    """
    return os.path.join(STORAGE_ROOT, studyInstanceUID)


def _tensor_outputs_present(work_dir):
    """Whether a previous tensor run left everything the tractography stage
    needs. Deliberately checks the actual files rather than a marker, so a run
    interrupted halfway is redone instead of being resumed from a gap."""
    return all(os.path.exists(os.path.join(work_dir, name)) for name in (
        'data.bval', 'dti_FA.nii.gz', 'dti_MD.nii.gz',
        'eddy_corrected.nii.gz', 'eddy_corrected.eddy_rotated_bvecs',
        'nodif_brain.nii.gz', 'nodif_brain_mask.nii.gz',
    ))


def _reference_instance(work_dir):
    """One instance of the study's diffusion series, as the source of the
    patient/study identifiers every upload needs.

    Cached to disk during staging so the tractography stage's reuse path does
    not have to pull a 7000-instance series back out of dcm4chee just to read
    a few header fields off its first image.
    """
    path = os.path.join(work_dir, 'reference.dcm')
    if not os.path.exists(path):
        raise ValueError(
            "No cached reference instance for this study -- run the DTI tensor "
            "pipeline for it first."
        )
    return pydicom.dcmread(path)


def _match_source_layout(volume, affine, reference_instance):
    """Re-index a result volume onto the source series' array layout.

    dcm2niix does not preserve the DICOM row/column layout: for these
    acquisitions it returns an array whose first axis runs patient-left and
    whose second runs anterior (axis codes LAS), while the source series
    stores rows running posterior and columns running left. Writing such an
    array back out slice by slice produces a series that is *geometrically*
    correct -- its ImageOrientationPatient describes the layout truthfully, so
    anything that resamples into patient space (MPR, 3D Slicer) overlays it on
    the source correctly -- but whose pixel matrix is the source's transposed.
    A 2D stack viewport draws the matrix as stored, so the map appears rotated
    by 90 degrees next to the series it was computed from.

    Permuting and flipping the array onto the source's layout fixes the
    display without moving anything in patient space: the same voxels keep the
    same coordinates, and the resulting ImageOrientationPatient comes out
    identical to the source series'.

    Only the two in-plane axes are matched to the source; the slice axis is
    left as the FSL outputs have it, since the slice *order* cannot be read off
    the single cached reference instance and the pipeline's own volumes are
    already consistent with each other.

    Handles (X, Y, Z) scalar and (X, Y, Z, 3) RGB volumes alike -- nibabel's
    apply_orientation() passes trailing axes through untouched.
    """
    orientation = np.asarray(reference_instance.ImageOrientationPatient, dtype=float)
    lps_to_ras = np.diag([-1.0, -1.0, 1.0])
    # IOP's first triplet is the direction of increasing column index, its
    # second that of increasing row index -- i.e. axes 1 and 0 of the array
    # layout we want to end up with.
    column_direction = lps_to_ras @ orientation[3:]
    row_direction = lps_to_ras @ orientation[:3]
    slice_norm = np.linalg.norm(affine[:3, 2]) or 1.0
    slice_direction = affine[:3, 2] / slice_norm

    target = np.eye(4)
    target[:3, :3] = np.stack([column_direction, row_direction, slice_direction], axis=1)

    transform = nib.orientations.ornt_transform(
        nib.orientations.io_orientation(affine),
        nib.orientations.io_orientation(target),
    )
    reoriented = nib.orientations.apply_orientation(volume, transform)
    return reoriented, affine @ nib.orientations.inv_ornt_aff(transform, volume.shape[:3])


def _store_volume(volume, affine, reference_instance, **kwargs):
    """Upload one result volume in the source series' array layout, so it sits
    on top of the acquisition it was computed from in a plain stack viewport
    instead of 90 degrees off it. See _match_source_layout()."""
    volume, affine = _match_source_layout(volume, affine, reference_instance)
    return build_and_store_dicom_series_from_reoriented_volume(
        volume, affine, reference_instance, **kwargs
    )


def _upload_tensor_maps(reference_instance, affine, fa, md, colour_fa_volume):
    """Upload the tensor stage's three image series.

    Split out of _upload_tensor_products() because these are the only part of
    that upload that carries geometry, and therefore the only part worth
    writing again when a layout has to be corrected -- see
    dti_reupload_wrapper().
    """
    _store_volume(
        fa * FA_SCALE, affine, reference_instance,
        series_description='FSL DTI FA (x1000)', series_number=_FA_SERIES_NUMBER,
        window_center=350, window_width=700,
    )
    _store_volume(
        md * MD_SCALE, affine, reference_instance,
        series_description='FSL DTI MD (1e-6 mm2/s)', series_number=_MD_SERIES_NUMBER,
        window_center=800, window_width=1600,
    )
    _store_volume(
        colour_fa_volume, affine, reference_instance,
        series_description='FSL DTI Colour FA (R=L-R, G=A-P, B=S-I)',
        series_number=_COLFA_SERIES_NUMBER,
    )


def _upload_tensor_products(studyInstanceUID, reference_instance, result):
    """Upload the tensor stage's maps, QC renders and metrics to the study.

    FA and MD go up as ordinary MR series so they behave like any other image
    in the viewer, scaled to the integer units the scanner uses for the same
    quantities (see FA_SCALE / MD_SCALE). Colour FA goes up as a true-colour
    series, since its information is the hue.

    Every description starts with "FSL" so a second run on the same study
    cannot mistake the first run's own FA map for the raw acquisition -- these
    land in the study as MR series whose descriptions otherwise match the DTI
    role criteria (see SERIES_CRITERIA's 'dwi', which excludes them by that
    prefix). Same exclusion the screw pipeline makes for its own derived
    series.
    """
    _upload_tensor_maps(reference_instance, result['affine'], result['fa'],
                        result['md'], result['colour_fa'])

    upload_images_as_sc(
        result['qc_images'], study_uid=studyInstanceUID,
        reference_instance=reference_instance,
        series_description='DTI QC (brain mask, FA, colour FA)',
        series_number=_QC_SERIES_NUMBER,
    )
    upload_dataframe_as_sc(
        result['metrics_table'], title='DTI: Acquisition and Correction Quality',
        study_uid=studyInstanceUID, reference_instance=reference_instance,
        series_description='DTI Tensor Metrics', series_number=_METRICS_SERIES_NUMBER,
    )
    _upload_fibre_field(studyInstanceUID, reference_instance,
                        result['streamline_views'], result['n_streamlines'])


def _upload_fibre_field(studyInstanceUID, reference_instance, views, n_streamlines):
    """Upload the fibre field's turntable render, if there is one (see
    _streamline_views for when there is not).

    A free 3D render, not a slice of the acquisition -- uploaded as plain
    pictures via upload_images_as_sc, not _store_volume, which exists
    specifically to align a volume with the source series' geometry (see
    _match_source_layout).
    """
    if views is None:
        return
    # Kept to 64 chars: SeriesDescription is DICOM VR LO, which caps there --
    # pydicom only warns and dcm4chee still stored the first, 74-char version
    # of this string, but a stricter reader is not guaranteed to.
    description = (f"FSL DTI Fibre Field ({n_streamlines} streamlines, "
                   f"{len(views)}-frame spin)")
    upload_images_as_sc(
        views, study_uid=studyInstanceUID,
        reference_instance=reference_instance,
        series_description=description[:64],
        series_number=_STREAMLINES_SERIES_NUMBER,
    )


def _fibre_field_links(studyInstanceUID, work_dir):
    """The job-message tail pointing at the interactive viewer and VTK
    export, for whichever of them this study's work directory has."""
    links = ''
    if os.path.exists(_streamline_viewer_path(work_dir)):
        links += (f" Interactive 3D fibre viewer: "
                  f"{FILES_BASE_URL}/dti_files/{studyInstanceUID}/streamlines_3d.html")
    if os.path.exists(_streamline_vtk_path(work_dir)):
        links += (f" 3D Slicer-readable file (Models, or a Fiber Bundle with the "
                  f"SlicerDMRI extension): {FILES_BASE_URL}/dti_files/{studyInstanceUID}/streamlines.vtk")
    return links


def _upload_tract_products(studyInstanceUID, reference_instance, result,
                           include_tables=True):
    """Upload the tractography stage's overlays and tables to the study.

    `include_tables` exists for the re-upload path: the two tables are
    secondary captures with no geometry, so a corrected image layout leaves
    them valid, and writing them again would only leave duplicates in the
    study.

    Each overlay is FA in greyscale with tract density blended in, in the
    diffusion space the FA series was uploaded in, so it lines up with it
    slice for slice instead of needing the viewer to reslice anything.
    """
    if result['combined_overlay'] is not None:
        _store_volume(
            result['combined_overlay'], result['affine'], reference_instance,
            series_description='FSL XTRACT Tracts on FA (see tract table for colours)',
            series_number=_TRACTS_SERIES_NUMBER,
        )

    for offset, highlight in enumerate(result['highlights']):
        _store_volume(
            highlight['volume'], result['affine'], reference_instance,
            series_description=f"FSL XTRACT {highlight['description']} on FA",
            series_number=_TRACT_HIGHLIGHT_SERIES_BASE + offset,
        )

    if not include_tables:
        return

    upload_dataframe_as_sc(
        _tractography_quality_table(result),
        title='XTRACT: Tractography Quality',
        study_uid=studyInstanceUID, reference_instance=reference_instance,
        series_description='DTI Tractography Quality',
        series_number=_METRICS_SERIES_NUMBER + 1,
    )
    upload_dataframe_as_sc(
        result['tract_table'], title='XTRACT: Reconstructed Tracts',
        study_uid=studyInstanceUID, reference_instance=reference_instance,
        series_description='DTI Tract Summary',
        series_number=_METRICS_SERIES_NUMBER + 2,
    )


def _tractography_quality_table(result):
    """The caveats a reader has to have in front of them before reading a
    tract overlay, alongside the numbers that bound how far to trust it.

    Carries the tensor stage's gradient provenance forward (from
    tensor_metrics.json) because tractography can run on a study whose tensor
    stage happened in an earlier job -- and a tract built on reconstructed
    directions has to say so wherever it is read, not only in the tensor
    stage's own table.
    """
    registration = ('n/a' if result['registration_r'] is None
                    else f"r = {result['registration_r']:.3f}")
    tensor = result.get('tensor_metrics') or {}
    provenance = []
    if tensor.get('gradient_source'):
        correlation = tensor.get('fa_correlation')
        provenance.append((
            'Gradient directions', tensor['gradient_source'],
            'n/a' if correlation is None
            else f"validated against the scanner's FA at r = {correlation:.3f}",
        ))
    return pd.DataFrame(provenance + [
        ('Tracts reconstructed',
         f"{result['n_reconstructed']} of {result['n_protocols']}",
         'an empty tract means the model did not get through, not that the '
         'bundle is interrupted'),
        ('Registration to FMRIB58_FA_1mm', registration,
         'FA registered directly to the template; no T1 in this study, so this '
         'is coarser than a route through one'),
        ('Fibre orientations per voxel',
         f"{result['n_fibres']} (bedpostx -n {result['n_fibres']} -model 2)",
         (f"{tensor['n_directions']} measured directions "
          f"(a third is requested at {_BEDPOSTX_THREE_FIBRE_MIN_DIRECTIONS}+)"
          if tensor.get('n_directions') is not None
          else 'direction count not recorded for this run; defaulted to 2')),
        ('Susceptibility distortion', 'not corrected',
         'a few mm remain frontobasally and temporopolarly'),
        ('Intended use', 'atlas-based orientation',
         'streamlines are model output, not axons; not for operative planning'),
    ], columns=['Metric', 'Value', 'Note'])


def dti_tensor_wrapper(studyInstanceUID):
    """Fit the diffusion tensor for a study and upload FA, MD, colour FA, a QC
    series, a metrics table and the whole-brain fibre field to it."""
    work_dir = _work_dir(studyInstanceUID)
    staged = stage_dti_series(studyInstanceUID, work_dir)
    result = dti_tensor_core(work_dir, staged)
    _upload_tensor_products(studyInstanceUID, staged['reference_instance'], result)

    validation = ('' if result['fa_correlation'] is None
                  else f" (validated against the scanner's FA at r = "
                       f"{result['fa_correlation']:.3f})")
    fibre_field = ('' if result['streamline_views'] is None
                   else f", a {result['n_streamlines']}-streamline fibre-field image")
    return (
        f"DTI tensor completed for study {studyInstanceUID}: FA, MD and colour FA "
        f"series, QC renders{fibre_field} and a metrics table uploaded. Gradient "
        f"directions {result['gradient_source']}{validation}."
        f"{_fibre_field_links(studyInstanceUID, work_dir)}"
    )


def dti_reupload_wrapper(studyInstanceUID, include_tables=False):
    """Write the study's DTI image series again from what is already on disk,
    without recomputing any of the pipeline.

    For correcting an upload after the fact -- a fixed DICOM layout, a wrong
    window, a series that never made it into the archive -- where the FSL
    outputs in the work directory are still good and only the series built
    from them are not. Everything expensive (eddy, dtifit, bedpostx, the
    registration, XTRACT) is read back rather than repeated; the tracts are
    re-warped into diffusion space, which is seconds each.

    Only the geometry-carrying series are rewritten. The QC montage and the
    three tables are secondary captures without spatial information, so a
    layout correction cannot affect them and rewriting them would only leave
    duplicates behind. `include_tables` overrides that for the case where the
    tract results themselves changed -- a rerun that recovered tracts an
    earlier one lost, say -- and the two tractography tables would otherwise
    keep describing the old, shorter result.

    Tracts are included when a complete XTRACT run is there and skipped
    otherwise, so this works on a study that only ever had the tensor stage.

    Note that nothing is removed: DICOM has no way to replace a series in
    place, so the superseded ones stay in the study until they are deleted
    from the archive.
    """
    work_dir = _work_dir(studyInstanceUID)
    if not _tensor_outputs_present(work_dir):
        raise ValueError(
            f"No DTI outputs on disk for study {studyInstanceUID}, so there is "
            "nothing to re-upload. Run the DTI tensor pipeline for it first."
        )

    reference_instance = _reference_instance(work_dir)
    dti_prefix = os.path.join(work_dir, 'dti')

    fa_image = nib.load(f"{dti_prefix}_FA.nii.gz")
    fa = fa_image.get_fdata()
    md = nib.load(f"{dti_prefix}_MD.nii.gz").get_fdata()
    v1 = nib.load(f"{dti_prefix}_V1.nii.gz").get_fdata()

    _upload_tensor_maps(reference_instance, fa_image.affine, fa, md, colour_fa(fa, v1))
    uploaded = ['FA', 'MD', 'colour FA']

    streamline_views, n_streamlines = _streamline_views(work_dir)
    if streamline_views is not None:
        _upload_fibre_field(studyInstanceUID, reference_instance,
                            streamline_views, n_streamlines)
        uploaded.append(f"the {n_streamlines}-streamline fibre-field image")

    bedpostx_dir = os.path.join(work_dir, 'bpx.bedpostX')
    xtract_dir = os.path.join(work_dir, 'xtract_out')
    if _xtract_is_complete(xtract_dir):
        result = _tract_products(work_dir, bedpostx_dir, xtract_dir)
        _upload_tract_products(studyInstanceUID, reference_instance, result,
                               include_tables=include_tables)
        uploaded.append(f"{result['n_reconstructed']} of {result['n_protocols']} "
                        "tracts as overlays")
        if include_tables:
            uploaded.append('the tractography tables')
    else:
        log("No complete XTRACT output for this study -- re-uploading the "
            "tensor maps only.")

    return (
        f"DTI series re-uploaded for study {studyInstanceUID} from the existing "
        f"work directory: {', '.join(uploaded)}. The superseded series are still "
        "in the study and have to be deleted from the archive."
    )


def dti_tractography_wrapper(studyInstanceUID):
    """Run probabilistic tractography (bedpostx + XTRACT) for a study and
    upload the reconstructed tracts as colour overlays on FA.

    Runs the tensor stage first if this study has not had it, so the button
    works on a study nobody pressed the tensor button on -- and skips it, with
    its uploads, if the outputs are already there.
    """
    work_dir = _work_dir(studyInstanceUID)

    if _tensor_outputs_present(work_dir):
        log(f"Reusing the existing tensor outputs in {work_dir}.")
        reference_instance = _reference_instance(work_dir)
    else:
        log("No tensor outputs for this study yet -- running the tensor stage first.")
        staged = stage_dti_series(studyInstanceUID, work_dir)
        tensor = dti_tensor_core(work_dir, staged)
        reference_instance = staged['reference_instance']
        _upload_tensor_products(studyInstanceUID, reference_instance, tensor)

    result = dti_tractography_core(work_dir)
    _upload_tract_products(studyInstanceUID, reference_instance, result)
    if result['fibre_field_is_new']:
        _upload_fibre_field(studyInstanceUID, reference_instance, *_streamline_views(work_dir))

    return (
        f"DTI tractography completed for study {studyInstanceUID}: "
        f"{result['n_reconstructed']} of {result['n_protocols']} XTRACT tracts "
        f"reconstructed; tract overlays on FA and the tract summary uploaded."
        f"{_fibre_field_links(studyInstanceUID, work_dir)}"
    )
