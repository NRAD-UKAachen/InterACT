"""
Runs SynthSeg (FreeSurfer 8.2's mri_synthseg) on the study's T1 series and
uploads a whole-brain segmentation plus volumetry tables.

Why this exists next to fastsurfer_functions.py rather than as another of its
modes: the two answer different questions. FastSurfer's networks are trained
on research-grade, near-isotropic 1 mm T1s, and its preprocessing assumes
that -- conform.py snaps anything near 1 mm onto a 256^3 grid (see
_conformed_grid_side over there). SynthSeg is trained on synthetic data
spanning a deliberately wide range of resolutions and contrasts, so it is the
tool for the thick-slice, anisotropic, variable-contrast T1s that clinical
PACS is actually full of -- exactly the scans FastSurfer is weakest on.

What it does NOT do is cortical thickness or surfaces: SynthSeg is a
segmentation network, with no surface reconstruction stage. For thickness,
FastSurfer's mode='full' remains the tool. This one gives volumetry (with a
cortical parcellation when asked) and does it in a couple of minutes rather
than half an hour.
"""
import glob
import os
import shutil

import nibabel as nib
import numpy as np
import pandas as pd

from .job_logging import run_logged
from .backend_helpers import (
    choose_gpu_index, client, find_required_seriesUIDs, free_gpu_memory_gb, log,
    store_series_as_nifti, upload_dataframe_as_sc, upload_segmentation,
)
# The left-right asymmetry convention (and the highlight threshold and its
# caption) is shared with FastSurfer's report on purpose: both tools produce a
# per-region L/R volume table for the same clinical question, and a reader
# comparing a SynthSeg run against a FastSurfer run of the same patient must
# not have to work out whether the two mean the same thing by "Asymmetry_%".
# Same-package private import as fastsurfer_long_functions does of this very
# module's helpers.
from .fastsurfer_functions import (
    _asymmetry_highlight, _asymmetry_percent, _highlight_note,
    _resample_segmentation_to_t1_grid,
)
from .storage_paths import API_STORAGE_DIR

SYNTHSEG_STORAGE_ROOT = f'{API_STORAGE_DIR}/synthseg'

# FreeSurfer 8.2.0 is installed on the host and bind-mounted read-only into
# this container (see API/docker-compose.yml), the same arrangement FSL
# already has for hippocampus_functions.py's run_first_all. Nothing is
# downloaded at run time: the SynthSeg weights ship inside that tree
# ($FREESURFER_HOME/models/synthseg_*.h5), which matters on this host --
# it has no direct outbound internet.
FREESURFER_HOME = os.environ.get('FREESURFER_HOME', '/usr/local/freesurfer')

# CUDA 11.8 + cuDNN 8.6 runtime libraries, supplied purely so SynthSeg can use
# the GPU.
#
# FreeSurfer 8.2 bundles its own Python with TensorFlow 2.13.1, which is built
# against CUDA 11.8/cuDNN 8. This image ships CUDA 12.1/cuDNN 9 instead (the
# nvidia-*-cu12 wheels that come with torch 2.5.1+cu121), so that TF finds the
# driver but cannot dlopen a single one of the runtime libraries it wants and
# reports "Cannot dlopen some GPU libraries ... Skipping registering GPU
# devices", silently falling back to the CPU. The two CUDA generations coexist
# fine as long as TF's are reached through LD_LIBRARY_PATH for this process
# only (see _synthseg_env) rather than installed over the image's own.
#
# Kept on the persistent API_storage mount rather than baked into the image
# for the same reason TOTALSEG_HOME_DIR and the brainles atlases are: an
# image rebuild would otherwise wipe ~2 GB that has to come back through the
# institutional proxy. Populate with:
#   pip install --target=$SYNTHSEG_CUDA11_ROOT \
#     nvidia-cuda-runtime-cu11==11.8.89 nvidia-cublas-cu11==11.11.3.6 \
#     nvidia-cufft-cu11==10.9.0.58 nvidia-curand-cu11==10.3.0.86 \
#     nvidia-cusolver-cu11==11.4.1.48 nvidia-cusparse-cu11==11.7.5.86 \
#     nvidia-cudnn-cu11==8.6.0.163 nvidia-cuda-nvcc-cu11==11.8.89
# If the directory isn't there, the run still works -- just on the CPU (see
# _cuda11_library_path), which costs roughly a minute per scan rather than
# failing.
SYNTHSEG_CUDA11_ROOT = os.environ.get(
    'SYNTHSEG_CUDA11_ROOT',
    f'{API_STORAGE_DIR}/synthseg_cuda11',
)

# Threads for the parts of mri_synthseg that stay on the CPU (preprocessing,
# resampling, and the whole prediction when there is no usable GPU). Modest on
# purpose: the batch pipeline runs BATCH_JOB_CONCURRENCY cases at once (9 on
# this host), so a per-case thread count that looks generous in isolation
# oversubscribes badly in a cohort run.
SYNTHSEG_THREADS = int(os.environ.get('SYNTHSEG_THREADS', '8'))

# --- GPU memory preflight -------------------------------------------------
# Same class of problem fastsurfer_functions._view_aggregation_device exists
# for, and it bites harder here: TensorFlow sizes nothing against what is
# actually free, it simply allocates until it fails. On this shared box (four
# A100s, other people's jobs on them) a run that lands on a busy card dies
# with "OOM when allocating tensor with shape[1,288,288,160,24]" -- observed,
# on a card with 1.3 GB free -- after having already spent a minute loading.
#
# SynthSeg pads each dimension up to a multiple of 32 for its 5-level U-Net,
# so the memory that matters scales with the *padded* voxel count. The
# level-0 feature map is padded_voxels x 24 channels x 4 bytes (1.27 GB for
# the 288x288x160 case above); peak sits several of those higher, since the
# down-arm's activations are held for the skip connections while the up-arm
# runs. The multiplier is deliberately generous -- being wrong toward the CPU
# costs about a minute (see SYNTHSEG_CPU_SECONDS in the log line), being
# wrong toward the GPU costs the whole run -- and _run_synthseg retries on the
# CPU anyway if the estimate turns out optimistic.
UNET_PAD_MULTIPLE = 32
UNET_LEVEL0_CHANNELS = 24
UNET_PEAK_MEMORY_FACTOR = 6

# TensorFlow otherwise grabs the entire card up front (measured: 38 GB of an
# 80 GB A100 for one scan), which on this host starves the batch pipeline's
# other GPU tools -- FastSurfer's segmentation stage runs on these same cards.
# Growth-based allocation also turned out slightly *faster* here (53 s vs 80 s
# for the same volume), so there is nothing traded away for it.
TF_ALLOW_GROWTH = 'true'

# The two variants exposed as this tool's batch `mode` / route segment.
#
# 'robust' is the one to reach for on genuinely poor or very low-resolution
# clinical scans -- it is a separate, sturdier model (synthseg_robust_2.0.h5).
# mri_synthseg forces --fast alongside it (it skips the topology-preserving
# postprocessing), so it is not simply "standard plus something": it trades
# some boundary refinement for not falling apart on a bad scan.
#
# Both ask for --parc, the cortical parcellation. It is the slower option but
# it is what makes the regional table comparable to FastSurfer's DKT one, and
# without it "segmentation" here means only ~32 whole structures.
SYNTHSEG_MODES = {
    'standard': {
        'flags': ['--parc'],
        'label': 'SynthSeg 2.0 + cortical parcellation',
    },
    'robust': {
        'flags': ['--robust', '--parc'],
        'label': 'SynthSeg-robust + cortical parcellation (for poor/low-resolution scans)',
    },
}

# The contrasts this tool will segment, in the order they are run and
# reported, keyed by the series role each comes from.
#
# Running T2 at all is only sensible because SynthSeg is contrast-agnostic by
# construction -- it is trained on synthetic data of randomised contrast, so a
# T2 is not an off-label input the way it would be for FastSurfer (whose
# networks are T1-only). What it is NOT is a multi-contrast model: there is no
# joint T1+T2 inference to be had here. The two contrasts are segmented
# independently and reported separately, which is exactly why this yields two
# segmentations and two sets of tables rather than one fused result.
#
# Neither contrast is individually required: the two are alternatives, and a
# run needs *at least one* of them. A study with only a T2 is segmented on the
# T2 alone and gets a complete result -- which is only reasonable because
# SynthSeg treats contrast as a nuisance parameter rather than as information,
# so a T2-only run is not a degraded T1 run, just a run on a different image.
# (Both present is still the richer outcome, since the two segmentations are
# then independent measurements of the same anatomy.) See
# _resolve_study_series() for the per-study end and BATCH_TOOL_ROLES in
# routes.py for the cohort end.
#
# 'series_offset' spaces each contrast's four products out inside this tool's
# series-number block below.
# 'role' is the SERIES_CRITERIA key each contrast is matched with -- SynthSeg's
# own looser T1/T2 roles rather than the shared 't1n'/'t2', so that plain
# clinical "T1 ax"/"T2 ax" series are picked up too; see backend_helpers.py.
SYNTHSEG_CONTRASTS = {
    't1': {'label': 'T1', 'role': 'synthseg_t1', 'series_offset': 0},
    't2': {'label': 'T2', 'role': 'synthseg_t2', 'series_offset': 4},
}

# The SERIES_CRITERIA roles above, in the same order -- what the batch
# pipeline declares (BATCH_TOOL_ROLES in routes.py) and stages per case.
SYNTHSEG_ROLES = [spec['role'] for spec in SYNTHSEG_CONTRASTS.values()]

# Reverse lookup, for turning a staged case's role_paths back into the
# {contrast: path} dict synthseg_core takes.
CONTRAST_FOR_ROLE = {spec['role']: contrast for contrast, spec in SYNTHSEG_CONTRASTS.items()}

# Series numbers this tool stores its products under. Its own block, so it
# can't collide with the SEG (5000) or another tool's tables (hippocampus
# 5001-5003, FastSurfer 6001-6003, FastSurfer longitudinal 6100-6101, FSL
# registration 6100, TotalSegmentator 6200, BraTS 6300, screw 6400-6401).
# Each contrast takes four consecutive numbers from its own series_offset, so
# the T1 products land on 6500-6503 and the T2 ones on 6504-6507 -- a study
# segmented on both contrasts carries two complete, separately-numbered sets
# rather than one overwriting the other.
_SERIES_NUMBER_BASE = 6500
_SEG_SERIES_OFFSET = 0
_GLOBAL_VOLUME_SERIES_OFFSET = 1
_VOLUME_TABLE_SERIES_OFFSET = 2
_QC_TABLE_SERIES_OFFSET = 3


def _series_number(contrast, product_offset):
    """Series number for one contrast's one product -- see the block comment
    above for the layout."""
    return _SERIES_NUMBER_BASE + SYNTHSEG_CONTRASTS[contrast]['series_offset'] + product_offset

# SynthSeg's QC scores (one per structure group, 0-1) are its own estimate of
# how far this segmentation sits from what it sees on good data. The project
# README puts the "inspect this one" line at 0.65; it is a screening aid for
# picking the suspect cases out of a cohort, not a pass/fail on the anatomy,
# which is why the score itself stays in the table next to the flag.
QC_SCORE_THRESHOLD = 0.65

# Volume columns in mri_synthseg's --vol CSV that are whole structures, as
# opposed to the ~68 cortical-parcellation columns (ctx-lh-*/ctx-rh-*) that
# --parc adds. Kept apart because the parcellation columns *subdivide* the two
# cortex columns rather than adding to them -- sum(ctx-lh-*) equals 'left
# cerebral cortex' to the decimal -- so any global total built by summing
# every column would count the entire cortex twice. Every total below is
# therefore built from these names only.
_PAIRED_STRUCTURES = (
    'cerebral white matter', 'cerebral cortex', 'lateral ventricle',
    'inferior lateral ventricle', 'cerebellum white matter', 'cerebellum cortex',
    'thalamus', 'caudate', 'putamen', 'pallidum', 'hippocampus', 'amygdala',
    'accumbens area', 'ventral DC',
)
_UNPAIRED_STRUCTURES = ('3rd ventricle', '4th ventricle', 'brain-stem', 'csf')

# Structure groups the global table's totals are summed from. Spelled out
# rather than inferred, because each of these is a reporting decision that
# should be readable next to the number it produces:
#
#   grey matter  -- cortex + the subcortical grey structures + cerebellar
#                   cortex. Matches what FreeSurfer's TotalGray covers, so the
#                   figure is comparable to the FastSurfer report's
#                   "Total grey matter volume"; brainstem is excluded there
#                   and excluded here.
#   white matter -- cerebral white matter only, again matching FreeSurfer's
#                   CerebralWhiteMatter (cerebellar white matter and brainstem
#                   are not in it).
#   ventricles   -- the CSF spaces *inside* the brain. Broken out on its own
#                   because ventricular volume is the measure that actually
#                   moves in the atrophy and hydrocephalus questions this tool
#                   gets pointed at, and on a thick-slice scan it is far more
#                   reliably measured than cortical thickness.
_GREY_MATTER_STRUCTURES = (
    'left cerebral cortex', 'right cerebral cortex',
    'left cerebellum cortex', 'right cerebellum cortex',
    'left thalamus', 'right thalamus', 'left caudate', 'right caudate',
    'left putamen', 'right putamen', 'left pallidum', 'right pallidum',
    'left hippocampus', 'right hippocampus', 'left amygdala', 'right amygdala',
    'left accumbens area', 'right accumbens area',
    'left ventral DC', 'right ventral DC',
)
_WHITE_MATTER_STRUCTURES = ('left cerebral white matter', 'right cerebral white matter')
_VENTRICLE_STRUCTURES = (
    'left lateral ventricle', 'right lateral ventricle',
    'left inferior lateral ventricle', 'right inferior lateral ventricle',
    '3rd ventricle', '4th ventricle',
)

# The intracranial total mri_synthseg reports directly, and the one column of
# its --vol CSV that is neither a structure nor a parcellation region.
_TIV_COLUMN = 'total intracranial'

# Extracerebral CSF -- inside the intracranial vault but outside the brain, so
# it is the one structure column left out of the total-brain sum below.
_EXTRACEREBRAL_CSF = 'csf'


### ---------------------------------------------------------------------- ###
### running mri_synthseg                                                    ###
### ---------------------------------------------------------------------- ###

def _cuda11_library_path():
    """The lib directories of the CUDA 11.8 wheels in SYNTHSEG_CUDA11_ROOT,
    joined for LD_LIBRARY_PATH, or None when they aren't installed.

    None means the run falls back to the CPU rather than failing: a missing
    optional accelerator should cost a minute, not the whole result.
    """
    lib_dirs = sorted(glob.glob(os.path.join(SYNTHSEG_CUDA11_ROOT, 'nvidia', '*', 'lib')))
    return ':'.join(lib_dirs) if lib_dirs else None


def _estimated_gpu_need_gb(nifti_path):
    """Rough peak GPU memory SynthSeg will want for this volume, in GB -- see
    the UNET_* constants for the model behind the number."""
    shape = nib.load(nifti_path).shape[:3]
    padded = [
        int(np.ceil(dim / UNET_PAD_MULTIPLE) * UNET_PAD_MULTIPLE) for dim in shape
    ]
    level0_bytes = float(np.prod(padded)) * UNET_LEVEL0_CHANNELS * 4
    return level0_bytes * UNET_PEAK_MEMORY_FACTOR / 1024 ** 3


def _choose_device(nifti_path, gpu_index=None):
    """Decide which GPU this run should use, or None to run on the CPU.

    Returns the GPU index to pin to, or None meaning "the estimate says this
    volume will not fit in what is free, run on the CPU instead" -- a minute
    or so slower, rather than an OOM an hour into a cohort.

    The interactive route's card is the emptiest one (choose_gpu_index)
    rather than TensorFlow's cuda:0 default -- landing on GPU 0 regardless of
    load is exactly how the OOM above was hit while GPU 3 sat with 39 GB free.
    """
    device_index = choose_gpu_index(gpu_index)
    if device_index is None:
        log("Could not read GPU memory, leaving SynthSeg on the CPU")
        return None

    required_gb = _estimated_gpu_need_gb(nifti_path)
    free_gb = free_gpu_memory_gb(device_index)
    if free_gb is None:
        log(f"Could not read free memory on GPU {device_index}, running SynthSeg on the CPU")
        return None

    if free_gb < required_gb:
        log(
            f"GPU {device_index} has {free_gb:.1f} GB free but this volume needs about "
            f"{required_gb:.1f} GB, running SynthSeg on the CPU"
        )
        return None

    log(f"Running SynthSeg on GPU {device_index} ({free_gb:.1f} GB free, ~{required_gb:.1f} GB needed)")
    return device_index


def _synthseg_env(gpu_index=None):
    """Environment for the mri_synthseg subprocess: FreeSurfer's own home (its
    launcher script refuses to start without it), and the CUDA 11.8 libraries
    TF 2.13 needs prepended to LD_LIBRARY_PATH for this process only.

    gpu_index pins the run to one GPU via CUDA_VISIBLE_DEVICES, which is what
    the batch worker pool needs -- several cases run at once and must not all
    pile onto the same card.
    Because TF then sees exactly one device, it is always 'GPU:0' from inside,
    so nothing downstream has to know which physical card it got.

    None means a CPU run (_choose_device having picked one, or fallen back to
    one), and hides the GPUs outright rather than relying on mri_synthseg's
    --cpu alone: TensorFlow initialises a context on every visible card even
    when it computes on none of them, and on this shared box that is memory
    taken from whatever else is running there.
    """
    env = {
        **os.environ,
        'FREESURFER_HOME': FREESURFER_HOME,
        # mri_synthseg writes intermediate volumes through FreeSurfer's own
        # I/O, which honours this the way the rest of the project's FSL calls
        # do -- keep it explicit rather than inheriting whatever is set.
        'FSLOUTPUTTYPE': 'NIFTI_GZ',
        'TF_FORCE_GPU_ALLOW_GROWTH': TF_ALLOW_GROWTH,
    }

    cuda_libs = _cuda11_library_path()
    if cuda_libs:
        existing = env.get('LD_LIBRARY_PATH', '')
        env['LD_LIBRARY_PATH'] = f'{cuda_libs}:{existing}' if existing else cuda_libs
        # ptxas, for the kernels TF's XLA path compiles at run time. Harmless
        # when XLA never kicks in; a "ptxas not found" fallback with a
        # noticeable slowdown when it does and this is missing.
        nvcc_bin = os.path.join(SYNTHSEG_CUDA11_ROOT, 'nvidia', 'cuda_nvcc', 'bin')
        if os.path.isdir(nvcc_bin):
            env['PATH'] = f"{nvcc_bin}:{env.get('PATH', '')}"
    else:
        log(
            f"No CUDA 11.8 runtime at {SYNTHSEG_CUDA11_ROOT} -- SynthSeg will run on "
            "the CPU (about a minute slower per scan). See SYNTHSEG_CUDA11_ROOT in "
            "synthseg_functions.py for how to populate it."
        )

    env['CUDA_VISIBLE_DEVICES'] = '' if gpu_index is None else str(gpu_index)

    return env


def _run_synthseg(t1_nifti_path, output_dir, mode='standard', gpu_index=None):
    """Run mri_synthseg on `t1_nifti_path`, writing the segmentation and its
    volume/QC CSVs into output_dir. Returns (seg_path, volumes_csv, qc_csv).

    Unlike FastSurfer (a sibling container on the host docker daemon), this
    runs in-process: FreeSurfer is mounted into this container already, so a
    subprocess is both simpler and one less place for the host-vs-container
    path aliasing that brats_functions.py's TMPDIR comment describes.
    """
    if mode not in SYNTHSEG_MODES:
        raise ValueError(
            f"Unknown SynthSeg mode '{mode}' "
            f"(expected one of {', '.join(sorted(SYNTHSEG_MODES))})."
        )

    os.makedirs(output_dir, exist_ok=True)
    seg_path = os.path.join(output_dir, 'synthseg.nii.gz')
    volumes_csv = os.path.join(output_dir, 'synthseg_volumes.csv')
    qc_csv = os.path.join(output_dir, 'synthseg_qc.csv')

    def attempt(device_index):
        """One mri_synthseg invocation, on `device_index` or -- when it is
        None -- on the CPU. Returns (ok, stdout, stderr)."""
        cmd = [
            os.path.join(FREESURFER_HOME, 'bin', 'mri_synthseg'),
            '--i', t1_nifti_path,
            '--o', seg_path,
            '--vol', volumes_csv,
            '--qc', qc_csv,
            '--threads', str(SYNTHSEG_THREADS),
            *SYNTHSEG_MODES[mode]['flags'],
        ]
        if device_index is None:
            cmd.append('--cpu')

        result = run_logged(
            cmd, timeout=7200, env=_synthseg_env(device_index),
        )

        # mri_synthseg catches a failed prediction per input and carries on to
        # the next one, so a single-input run can report "ERROR: some problems
        # occured for the following inputs" and still exit 0 having written
        # nothing -- how the GPU OOM above first surfaced. Neither the exit
        # code nor the output alone is sufficient; both are checked.
        wrote_everything = all(
            os.path.exists(p) for p in (seg_path, volumes_csv, qc_csv)
        )
        return (result.returncode == 0 and wrote_everything), result.stdout, result.stderr

    log(f"Running SynthSeg ({mode}) on {t1_nifti_path}")
    device_index = _choose_device(t1_nifti_path, gpu_index)
    ok, stdout, stderr = attempt(device_index)

    # The preflight is an estimate over someone else's memory, and the card's
    # free memory can drop between the check and the allocation, so a GPU run
    # that fails anyway is retried once on the CPU rather than lost. Only
    # worth it when the GPU was actually used -- a CPU run that failed has
    # nothing left to fall back to.
    if not ok and device_index is not None:
        log(
            "SynthSeg failed on the GPU (most likely out of memory -- another job may "
            "have taken the card since the preflight); retrying on the CPU"
        )
        ok, stdout, stderr = attempt(None)

    if not ok:
        raise RuntimeError(
            f"SynthSeg ({mode}) failed on {t1_nifti_path}.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )

    return seg_path, volumes_csv, qc_csv


### ---------------------------------------------------------------------- ###
### parsing mri_synthseg's outputs                                          ###
### ---------------------------------------------------------------------- ###

def _read_single_row_csv(path):
    """mri_synthseg's --vol and --qc files are one wide row per input volume;
    we hand it a single image, so take that row as a {column: value} Series."""
    return pd.read_csv(path).iloc[0]


def _segmentation_labels_and_values(seg_path):
    """Build the (labels, values) upload_segmentation() needs by reading
    SynthSeg's own label/name tables out of $FREESURFER_HOME/models, then
    keeping only the labels this particular segmentation actually contains.

    Reading the shipped .npy tables rather than keeping a copy of FreeSurfer's
    ~1000-entry colour LUT in here is the same choice
    fastsurfer_functions._segmentation_labels_from_aseg_stats makes for the
    same reason -- and it means the names in PACS come from the same file the
    network was built against, so a FreeSurfer upgrade can't quietly desync
    them.

    The intersection with what is present matters: upload_segmentation()
    creates one DICOM segment per value handed to it, and the tables describe
    every label the model *can* emit (background and the unused entries
    included), not what came out of this scan.
    """
    models_dir = os.path.join(FREESURFER_HOME, 'models')
    pairs = {}
    # The parcellation tables are loaded too, and second: with --parc the
    # cortex labels 3/42 are replaced in the output by the ctx-* labels, so
    # both sets can legitimately appear and neither alone describes the file.
    for labels_file, names_file in (
        ('synthseg_segmentation_labels_2.0.npy', 'synthseg_segmentation_names_2.0.npy'),
        ('synthseg_parcellation_labels.npy', 'synthseg_parcellation_names.npy'),
    ):
        values = np.load(os.path.join(models_dir, labels_file))
        names = np.load(os.path.join(models_dir, names_file))
        for value, name in zip(values, names):
            # Label 0 is background, and the tables repeat it as filler for
            # unused slots -- never a segment.
            if int(value) != 0:
                pairs[int(value)] = str(name)

    present = {int(v) for v in np.unique(np.asanyarray(nib.load(seg_path).dataobj)) if int(v) != 0}
    values = sorted(present & set(pairs))

    unnamed = present - set(pairs)
    if unnamed:
        # Not fatal: an unnamed label is one segment missing from the SEG, not
        # a wrong number anywhere, and the volume tables come from the CSV
        # regardless. Worth a line in the log rather than a silent drop.
        log(f"SynthSeg produced labels with no name in the model tables, skipping: {sorted(unnamed)}")

    return [pairs[v] for v in values], values


def _structure_columns(volumes):
    """The whole-structure columns present in this run's volume row -- i.e.
    everything except the cortical-parcellation regions and the intracranial
    total. See _PAIRED_STRUCTURES on why the two must never be summed
    together."""
    names = [f'{side} {structure}'
             for structure in _PAIRED_STRUCTURES for side in ('left', 'right')]
    names += list(_UNPAIRED_STRUCTURES)
    return [name for name in names if name in volumes.index]


def _sum_volumes(volumes, names):
    """Total of `names` in mm3, skipping any the run didn't produce."""
    return float(sum(float(volumes[name]) for name in names if name in volumes.index))


def _global_volume_table(volumes):
    """Whole-brain summary: intracranial, brain, grey, white and ventricular
    volume, each also expressed as a percentage of total intracranial volume.

    The TIV percentage column is the point of this table rather than a
    decoration. Raw millilitres are not comparable between two people (or
    against any published range) without correcting for head size, and TIV is
    the standard correction -- SynthSeg estimating it directly is one of the
    concrete things it offers over the FastSurfer report, which quotes its
    global volumes in mm3 alone.
    """
    tiv = float(volumes[_TIV_COLUMN]) if _TIV_COLUMN in volumes.index else None

    # Everything inside the brain -- so ventricles and subcortical structures
    # are in (matching what FreeSurfer's BrainSeg counts), and extracerebral
    # CSF, which is intracranial but not brain, is out.
    brain_structures = [name for name in _structure_columns(volumes)
                        if name != _EXTRACEREBRAL_CSF]

    measures = [
        ('Total intracranial volume', tiv if tiv is not None else 0.0),
        ('Total brain volume', _sum_volumes(volumes, brain_structures)),
        ('Total grey matter volume', _sum_volumes(volumes, _GREY_MATTER_STRUCTURES)),
        ('Total white matter volume', _sum_volumes(volumes, _WHITE_MATTER_STRUCTURES)),
        ('Total ventricular volume', _sum_volumes(volumes, _VENTRICLE_STRUCTURES)),
    ]

    return pd.DataFrame({
        'Measure': [label for label, _ in measures],
        'Volume_mm3': [round(value, 2) for _, value in measures],
        'Volume_mL': [round(value / 1000, 2) for _, value in measures],
        '% of Total Intracranial': [
            round(value / tiv * 100, 2) if tiv else None for _, value in measures
        ],
    })


def _hemisphere_volume_table(volumes):
    """Left/right volume comparison with the asymmetry index, over both the
    paired subcortical structures and -- when the run used --parc -- the
    cortical regions.

    Deliberately the same shape and the same Asymmetry_% definition as
    fastsurfer_functions._hemisphere_volume_table, so the two tools' regional
    tables can be read side by side for one patient.
    """
    rows = []
    for structure in _PAIRED_STRUCTURES:
        left, right = f'left {structure}', f'right {structure}'
        if left in volumes.index and right in volumes.index:
            rows.append((structure, float(volumes[left]), float(volumes[right])))

    # The parcellation regions pair up by their ctx-lh-/ctx-rh- prefix, the
    # same convention FastSurfer's DKT rows use.
    for column in volumes.index:
        if column.startswith('ctx-lh-'):
            right = column.replace('ctx-lh-', 'ctx-rh-', 1)
            if right in volumes.index:
                rows.append((column[len('ctx-lh-'):], float(volumes[column]), float(volumes[right])))

    table = pd.DataFrame(rows, columns=['Region', 'Volume_mm3_Left', 'Volume_mm3_Right'])
    table['Asymmetry_%'] = _asymmetry_percent(
        table['Volume_mm3_Left'], table['Volume_mm3_Right']
    )
    return table.round(2)


def _qc_table(qc):
    """SynthSeg's per-structure-group QC scores, with the low ones flagged.

    FastSurfer has no equivalent, and on the low-quality scans this tool is
    for it is the part worth reading first: it is the difference between a
    cohort's odd numbers being noticed and being averaged in.
    """
    regions = [column for column in qc.index if column != 'subject']
    scores = [float(qc[region]) for region in regions]
    return pd.DataFrame({
        'Structure Group': regions,
        'QC_Score': [round(score, 3) for score in scores],
        'Flag': ['INSPECT' if score < QC_SCORE_THRESHOLD else 'OK' for score in scores],
    })


def _qc_highlight(table):
    """Per-row booleans marking the flagged QC rows, for
    upload_dataframe_as_sc()'s highlight_rows."""
    return (table['QC_Score'] < QC_SCORE_THRESHOLD).tolist()


def _qc_note():
    return (
        f'Highlighted rows: SynthSeg QC score below {QC_SCORE_THRESHOLD} -- '
        'inspect this segmentation before using its volumes'
    )


### ---------------------------------------------------------------------- ###
### pure local-file compute -- no dcm4chee access below this point         ###
### ---------------------------------------------------------------------- ###

def _segment_one_contrast(source_nifti_path, output_dir, mode='standard', gpu_index=None):
    """Segment a single already-staged NIfTI and parse the results into the
    per-contrast product dict synthseg_core() collects.

    Returns a dict:
      - 'source_nifti_path': the image this was segmented from
      - 'segmentation': label map resampled onto that image's own grid,
        for upload_segmentation()
      - 'segmentation_source_path': SynthSeg's own output, on its native grid
      - 'segmentation_labels' / 'segmentation_values': for upload_segmentation()
      - 'global_volume_table': intracranial/brain/grey/white/ventricular
        volumes, each also as a percentage of TIV
      - 'volume_table': per-region left/right comparison with asymmetry
      - 'qc_table': SynthSeg's own per-structure-group QC scores
      - 'volumes' / 'qc': the raw parsed CSV rows, for batch stats
    """
    os.makedirs(output_dir, exist_ok=True)

    seg_path, volumes_csv, qc_csv = _run_synthseg(
        source_nifti_path, output_dir, mode=mode, gpu_index=gpu_index
    )

    volumes = _read_single_row_csv(volumes_csv)
    qc = _read_single_row_csv(qc_csv)
    labels, values = _segmentation_labels_and_values(seg_path)

    # SynthSeg leaves the output on the input's grid when the input is already
    # near-isotropic, but resamples internally to 1 mm and returns *that* grid
    # for anything anisotropic -- i.e. precisely the thick-slice scans this
    # tool exists for. (Measured: a 0.98x0.98x1.0 mm T1 comes back on its own
    # 257x257x160 grid, the same volume resliced to 5 mm comes back
    # 252x251x161 at 1 mm.) upload_segmentation() assigns the label map to the
    # source series slice by slice, so it has to be put back on the source
    # grid either way; doing it here unconditionally costs a nearest-neighbour
    # resample that is a no-op when the grids already agree.
    #
    # The volumes are NOT recomputed from this resampled copy -- they come
    # from SynthSeg's own CSV, measured on its 1 mm working grid. Counting
    # voxels on a 5 mm source grid instead would throw away the sub-slice
    # accuracy that is the entire reason for using this tool on such a scan.
    #
    # (_resample_segmentation_to_t1_grid is named for its FastSurfer origin
    # but is a plain nearest-neighbour resample onto whatever reference volume
    # it is handed, so it serves the T2 pass just as well as the T1 one.)
    segmentation = _resample_segmentation_to_t1_grid(seg_path, source_nifti_path)

    return {
        'segmentation': segmentation,
        'source_nifti_path': source_nifti_path,
        'segmentation_source_path': seg_path,
        'segmentation_labels': labels,
        'segmentation_values': values,
        'global_volume_table': _global_volume_table(volumes),
        'volume_table': _hemisphere_volume_table(volumes),
        'qc_table': _qc_table(qc),
        'volumes': volumes,
        'qc': qc,
    }


def synthseg_core(contrast_paths, output_dir, mode='standard', gpu_index=None):
    """Segment every contrast in `contrast_paths` and parse the results into
    local-file products, without touching dcm4chee -- what the per-study route
    (synthseg_wrapper below) and the batch pipeline (routes.py's
    _run_synthseg_tool) share.

    `contrast_paths` is a {contrast: nifti_path} dict over SYNTHSEG_CONTRASTS,
    normally built by _resolve_study_series() below. Any non-empty subset is
    valid -- T1 only, T2 only, or both -- and when both are given the second
    is a fully independent segmentation of the same study rather than a
    refinement of the first.

    Each contrast is segmented into its own subdirectory of output_dir --
    mri_synthseg names its outputs after the flags, not the input, so two runs
    sharing a directory would overwrite each other's segmentation and CSVs.

    Returns {'contrasts': {contrast: <per-contrast dict>}}, each value being
    what _segment_one_contrast() documents. Keeping the two behind one key
    (rather than flattening the T1 case to the top level for convenience)
    means no caller can read a single-contrast result and silently miss that a
    T2 pass also ran.
    """
    if not contrast_paths:
        raise ValueError(
            "SynthSeg needs at least one image to segment "
            f"(any of {sorted(SYNTHSEG_CONTRASTS)}); got nothing."
        )

    unknown = set(contrast_paths) - set(SYNTHSEG_CONTRASTS)
    if unknown:
        raise ValueError(
            f"Unknown SynthSeg contrast(s) {sorted(unknown)} "
            f"(expected a subset of {sorted(SYNTHSEG_CONTRASTS)})."
        )

    os.makedirs(output_dir, exist_ok=True)

    contrasts = {}
    # Ordered by SYNTHSEG_CONTRASTS rather than by the caller's dict, so the
    # T1 pass always runs (and logs) first regardless of how the paths were
    # assembled.
    for contrast in SYNTHSEG_CONTRASTS:
        if contrast not in contrast_paths:
            continue
        label = SYNTHSEG_CONTRASTS[contrast]['label']
        log(f"SynthSeg ({mode}): segmenting the {label} image")
        contrasts[contrast] = _segment_one_contrast(
            contrast_paths[contrast], os.path.join(output_dir, contrast),
            mode=mode, gpu_index=gpu_index,
        )

    return {'contrasts': contrasts}


def synthseg_batch_stats(result):
    """Flatten synthseg_core()'s already-computed tables into a flat
    {metric_name: value} dict for the batch pipeline's results_overview.csv
    (see routes.py's _run_synthseg_tool), mirroring fastsurfer_batch_stats.

    Every metric is prefixed with its contrast ('T1_...', 'T2_...'), so a
    cohort where only some studies have a T2 still produces one coherent
    table: the T2 columns are simply empty for the studies that had none,
    instead of two contrasts' measurements of the same structure landing in
    one column and being averaged as though they were comparable.

    The QC scores are carried into the overview alongside the volumes on
    purpose: over a cohort, that column is what lets a bad segmentation be
    filtered out of a group analysis instead of quietly skewing it.
    """
    stats = {}

    for contrast, contrast_result in result['contrasts'].items():
        prefix = SYNTHSEG_CONTRASTS[contrast]['label']

        for _, row in contrast_result['global_volume_table'].iterrows():
            measure = row['Measure'].replace(' ', '_')
            stats[f'{prefix}_{measure}_mm3'] = float(row['Volume_mm3'])
            if row['% of Total Intracranial'] is not None:
                stats[f'{prefix}_{measure}_%_of_TIV'] = float(row['% of Total Intracranial'])

        for _, row in contrast_result['volume_table'].iterrows():
            region = row['Region'].replace(' ', '_')
            stats[f'{prefix}_{region}_Volume_mm3_Left'] = float(row['Volume_mm3_Left'])
            stats[f'{prefix}_{region}_Volume_mm3_Right'] = float(row['Volume_mm3_Right'])
            stats[f'{prefix}_{region}_Asymmetry_%'] = float(row['Asymmetry_%'])

        for _, row in contrast_result['qc_table'].iterrows():
            group = row['Structure Group'].replace(' ', '_').replace('+', '_')
            stats[f'{prefix}_QC_{group}'] = float(row['QC_Score'])

    return stats


### ---------------------------------------------------------------------- ###
### batch pipeline: write core outputs to a plain output tree (no dcm4chee) ###
### ---------------------------------------------------------------------- ###

def save_synthseg_core_outputs(result, out_dir):
    """Write synthseg_core()'s outputs to `out_dir` as plain files, for the
    batch pipeline's ZIP-in/ZIP-out route (POST /batch/run with 'synthseg'
    selected). No DICOM SEG here -- a batch case has no dcm4chee study to
    attach one to.

    Each contrast gets its own subdirectory ('t1n/', and 't2/' when the study
    had one), so the two segmentations and their tables stay unambiguous in a
    cohort's result ZIP -- a flat layout would need every filename prefixed
    anyway, and a reader opening 'volume_table.csv' should never have to
    work out which image it was measured from.

    Both grids are kept per contrast, as the hippocampus tool does: the
    native-grid copy is what the volumes were measured on and what to
    re-analyse from, while the source-grid copy is the one that overlays that
    contrast's own series as staged.
    """
    os.makedirs(out_dir, exist_ok=True)

    for contrast, contrast_result in result['contrasts'].items():
        contrast_dir = os.path.join(out_dir, contrast)
        os.makedirs(contrast_dir, exist_ok=True)

        source_affine = nib.load(contrast_result['source_nifti_path']).affine
        seg_img = nib.Nifti1Image(
            contrast_result['segmentation'].astype(np.int16), source_affine
        )
        nib.save(seg_img, os.path.join(contrast_dir, 'synthseg_on_source_grid.nii.gz'))

        shutil.copyfile(
            contrast_result['segmentation_source_path'],
            os.path.join(contrast_dir, 'synthseg.nii.gz'),
        )

        contrast_result['global_volume_table'].to_csv(
            os.path.join(contrast_dir, 'global_volume_table.csv'), index=False
        )
        contrast_result['volume_table'].to_csv(
            os.path.join(contrast_dir, 'volume_table.csv'), index=False
        )
        contrast_result['qc_table'].to_csv(
            os.path.join(contrast_dir, 'qc_table.csv'), index=False
        )


### ---------------------------------------------------------------------- ###
### wrapper: stage-in from dcm4chee -> core -> stage-out to dcm4chee       ###
### ---------------------------------------------------------------------- ###

def _resolve_study_series(studyInstanceUID):
    """Pick whichever of this study's T1 and T2 series exist -- returning the
    {contrast: SeriesInstanceUID} dict to stage. Raises only when the study
    has neither.

    find_required_seriesUIDs() is all-or-nothing (it returns None the moment
    any requested role has no match), so two optional contrasts cannot simply
    be handed to it as a list. Both are asked for together first, and only if
    that comes back empty is each tried on its own.

    Asking for the pair *together* first is what keeps the two roles from
    landing on the same series: that function assigns greedily and removes
    each match from the pool, a guarantee that only holds within one call.
    Falling straight through to two separate single-role calls would let one
    series score best for both roles and be segmented twice under two
    different names -- which is why the combined call is tried first and the
    per-contrast calls only ever run when the combined one has already
    established that at most one of the roles can be filled.
    """
    both = find_required_seriesUIDs(studyInstanceUID, SYNTHSEG_ROLES)
    if both:
        return {CONTRAST_FOR_ROLE[role]: uid for role, uid in both.items()}

    # One series can satisfy both roles' criteria (a description carrying both
    # keywords), and these single-role calls each search the whole study with
    # no shared pool -- so without this guard such a series would come back for
    # both contrasts and be segmented twice, producing two identical SEGs and
    # two identical volume tables labelled T1 and T2. First contrast in
    # SYNTHSEG_CONTRASTS order wins it.
    resolved, claimed = {}, set()
    for contrast, spec in SYNTHSEG_CONTRASTS.items():
        role = spec['role']
        found = find_required_seriesUIDs(studyInstanceUID, [role])
        if found and found[role] not in claimed:
            resolved[contrast] = found[role]
            claimed.add(found[role])

    if not resolved:
        raise ValueError(
            f"Study {studyInstanceUID} has neither a T1 nor a T2 series, so there is "
            "nothing for SynthSeg to segment."
        )

    missing = [SYNTHSEG_CONTRASTS[c]['label'] for c in SYNTHSEG_CONTRASTS if c not in resolved]
    present = [SYNTHSEG_CONTRASTS[c]['label'] for c in resolved]
    log(
        f"Study {studyInstanceUID} has no matching {'/'.join(missing)} series, "
        f"segmenting the {'/'.join(present)} only"
    )
    return resolved


def synthseg_wrapper(studyInstanceUID, mode='standard'):
    """End-to-end SynthSeg run for a study: download whichever of the T1 and
    T2 series the study has, segment each independently, and upload per
    contrast a DICOM SEG plus the global volume, regional left/right and QC
    tables. This is what the /synthseg/<mode>/<studyInstanceUID> route calls.

    A study with both contrasts comes back with two segmentations and two sets
    of tables -- the T2 SEG attached to the T2 series and the T1 SEG to the T1
    series, each on its own source geometry, so both overlay correctly on the
    series they were measured from. A study with only one of them comes back
    with one complete set; only a study with neither is an error.
    """
    study_dir = os.path.join(SYNTHSEG_STORAGE_ROOT, studyInstanceUID)
    output_dir = os.path.join(study_dir, 'output')

    # stage-in
    UID_dic = _resolve_study_series(studyInstanceUID)
    contrast_paths = {}
    for contrast, series_uid in UID_dic.items():
        nifti_path = os.path.join(study_dir, f'{contrast}.nii.gz')
        store_series_as_nifti(studyInstanceUID, series_uid, filename=nifti_path)
        contrast_paths[contrast] = nifti_path

    # core
    result = synthseg_core(contrast_paths, output_dir, mode=mode)

    # stage-out -- each contrast's products are attached to that contrast's
    # own series, and numbered from its own offset (see _series_number).
    uploaded, flagged_summary = [], []
    for contrast, contrast_result in result['contrasts'].items():
        label = SYNTHSEG_CONTRASTS[contrast]['label']
        series_uid = UID_dic[contrast]
        reference_instance = client.retrieve_series(
            study_instance_uid=studyInstanceUID, series_instance_uid=series_uid
        )[0]

        upload_segmentation(
            contrast_result['segmentation'],
            labels=contrast_result['segmentation_labels'],
            values=contrast_result['segmentation_values'],
            study_uid=studyInstanceUID,
            series_uid=series_uid,
            seg_description=f'SynthSeg {label} whole-brain segmentation ({mode})',
            seg_series_number=_series_number(contrast, _SEG_SERIES_OFFSET),
        )

        upload_dataframe_as_sc(
            contrast_result['global_volume_table'],
            title=f'SynthSeg ({label}): Global Brain Volumes (with % of intracranial volume)',
            study_uid=studyInstanceUID, reference_instance=reference_instance,
            series_description=f'SynthSeg {label} Global Brain Volumes',
            series_number=_series_number(contrast, _GLOBAL_VOLUME_SERIES_OFFSET),
        )

        upload_dataframe_as_sc(
            contrast_result['volume_table'],
            title=f'SynthSeg ({label}): Left/Right Volume Comparison (mm3)',
            study_uid=studyInstanceUID, reference_instance=reference_instance,
            series_description=f'SynthSeg {label} Volume L-R Comparison',
            series_number=_series_number(contrast, _VOLUME_TABLE_SERIES_OFFSET),
            highlight_rows=_asymmetry_highlight(contrast_result['volume_table'], 'Asymmetry_%'),
            highlight_note=_highlight_note(),
        )

        upload_dataframe_as_sc(
            contrast_result['qc_table'],
            title=f'SynthSeg ({label}): Automated Quality Control Scores',
            study_uid=studyInstanceUID, reference_instance=reference_instance,
            series_description=f'SynthSeg {label} QC Scores',
            series_number=_series_number(contrast, _QC_TABLE_SERIES_OFFSET),
            highlight_rows=_qc_highlight(contrast_result['qc_table']),
            highlight_note=_qc_note(),
        )

        uploaded.append(label)
        flagged = contrast_result['qc_table'][contrast_result['qc_table']['Flag'] == 'INSPECT']
        if not flagged.empty:
            flagged_summary.append(
                f"{label}: {', '.join(flagged['Structure Group'])}"
            )

    qc_summary = (
        f" QC flagged structure group(s) below {QC_SCORE_THRESHOLD} -- "
        + '; '.join(flagged_summary) + '.'
        if flagged_summary else " All QC scores nominal."
    )
    return (
        f"SynthSeg ({mode}) completed for study {studyInstanceUID} on "
        f"{' and '.join(uploaded)}: one SEG plus global volume, left/right and QC "
        f"tables per contrast uploaded to PACS.{qc_summary}"
    )
