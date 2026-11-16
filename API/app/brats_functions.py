import os
import shutil
import docker as docker_sdk
import nibabel as nib
from .backend_helpers import (
    choose_gpu_index, log, segment_volume_stats, segment_volume_table, store_required_series_as_nifti,
    upload_segment_volume_table, upload_segmentation,
)
from brainles_preprocessing.modality import Modality, CenterModality
from brainles_preprocessing.normalization.percentile_normalizer import PercentileNormalizer
from brainles_preprocessing.preprocessor import AtlasCentricPreprocessor
from brats import GoATSegmenter, AdultGliomaPreAndPostTreatmentSegmenter, AdultGliomaPreTreatmentSegmenter, MeningiomaSegmenter, MetastasesSegmenter, PediatricSegmenter
from brats.constants import GoATAlgorithms
from .apply_transform_3dslicer_style import TransformApplier
from .brainles_assets import ensure_brainles_assets
from .job_logging import follow_file, timed
from .storage_paths import API_STORAGE_DIR

BRATS_STORAGE_ROOT = f'{API_STORAGE_DIR}/tumorseg'

# BraTS segmentation labels/values are the same across all segmenter variants.
BRATS_SEGMENTATION_LABELS = ['Non-enhancing', 'Edema', 'Enhancing']
BRATS_SEGMENTATION_VALUES = [1, 2, 3]

# Series number the per-study route's volumetry table is stored under. Its own
# block, so it can't collide with the SEG (5000) or another tool's tables
# (hippocampus 5001-5003, FastSurfer 6001-6003, FSL registration 6100,
# TotalSegmentator 6200).
_VOLUME_TABLE_SERIES_NUMBER = 6300


### ---------------------------------------------------------------------- ###
### pure local-file compute -- no dcm4chee access below this point         ###
### ---------------------------------------------------------------------- ###

def _atlas_preprocess(input_dir, output_dir, gpu_index=None):
    """Register t1c/flair/t1n/t2 NIfTIs (already staged in input_dir) onto
    the atlas, skull-strip, and normalize them via BrainLes -- the input
    format BraTS segmenters expect. Writes normalized_bet/*.nii.gz and the
    atlas-registration transforms under output_dir. Skips work if the output
    already exists from a previous run.

    gpu_index restricts preprocessing (skull-stripping) to one GPU via
    AtlasCentricPreprocessor's limit_cuda_visible_devices -- required by
    batch_jobs.py's worker pool (see brats_core's docstring). None (the
    interactive per-study route's default) keeps the original unrestricted
    behavior."""
    list_of_files = [os.path.join(output_dir, 'normalized_bet', f'{s}_bet_normalized.nii.gz') for s in ['t1n', 't1c', 't2', 'flair']]
    if all(os.path.exists(filepath) for filepath in list_of_files):
        log(f'Preprocessed files already exist in {output_dir}. Skip this step.')

    else:
        # specify a normalizer (required if using any normalized_* output paths)
        percentile_normalizer = PercentileNormalizer(
            lower_percentile=0.1,
            upper_percentile=99.9,
            lower_limit=0,
            upper_limit=1,
        )

        # define center modality with all possible outputs
        center = CenterModality(
            modality_name='t1c',
            input_path=os.path.join(input_dir, 't1c.nii.gz'),
            normalizer=percentile_normalizer,
            normalized_bet_output_path=os.path.join(output_dir, 'normalized_bet', 't1c_bet_normalized.nii.gz')
        )

        # define moving modalities
        moving_modalities = [
            Modality(
                modality_name='flair',
                input_path=os.path.join(input_dir, 'flair.nii.gz'),
                normalizer=percentile_normalizer,
                normalized_bet_output_path=os.path.join(output_dir, 'normalized_bet', 'flair_bet_normalized.nii.gz')
            ),
            Modality(
                modality_name="t1",
                input_path=os.path.join(input_dir, 't1n.nii.gz'),
                normalizer=percentile_normalizer,
                normalized_bet_output_path=os.path.join(output_dir, 'normalized_bet', 't1n_bet_normalized.nii.gz')
            ),
            Modality(
                modality_name="t2",
                input_path=os.path.join(input_dir, 't2.nii.gz'),
                normalizer=percentile_normalizer,
                normalized_bet_output_path=os.path.join(output_dir, 'normalized_bet', 't2_bet_normalized.nii.gz')
            )
        ]

        # Atlases/SynthStrip weights first (no-op once cached; the interactive
        # route has no batch-level prefetch) -- see brainles_assets.py.
        ensure_brainles_assets()

        # instantiate and run the preprocessor
        preprocessor = AtlasCentricPreprocessor(
            center_modality=center,
            moving_modalities=moving_modalities,
            limit_cuda_visible_devices=None if gpu_index is None else str(gpu_index),
        )

        # log_file keeps BrainLes' loguru file sink inside the case's output
        # dir under API_storage; without it the library drops a timestamped
        # brainles_preprocessing_*.log into the process' CWD (API/).
        os.makedirs(output_dir, exist_ok=True)
        # BrainLes logs through loguru, not stdlib logging, so its file sink is
        # followed to get those lines into the job log too.
        preprocessing_log = os.path.join(output_dir, 'brainles_preprocessing.log')
        with follow_file(preprocessing_log, name='brainles_preprocessing'), timed('BrainLes atlas preprocessing'):
            preprocessor.run(
                save_dir_transformations=os.path.join(output_dir, 'transformations'),
                log_file=preprocessing_log,
            )


def _build_segmenter(segmenter_name, cuda_devices='0'):
    """Construct the BraTS segmenter object for `segmenter_name`. Shared by
    _run_brats_inference() (runs inference) and ensure_segmenter_image_pulled()
    (only needs .algorithm.run_args.docker_image off it, never runs
    inference) so the segmenter_name -> class mapping lives in one place."""
    if segmenter_name=='glioprepost':
        return AdultGliomaPreAndPostTreatmentSegmenter(cuda_devices=cuda_devices)
    elif segmenter_name=='gliomapre':
        return AdultGliomaPreTreatmentSegmenter(cuda_devices=cuda_devices)
    elif segmenter_name=='meningioma':
        return MeningiomaSegmenter(cuda_devices=cuda_devices)
    elif segmenter_name=='metastases':
        return MetastasesSegmenter(cuda_devices=cuda_devices)
    elif segmenter_name=='pediatric':
        return PediatricSegmenter(cuda_devices=cuda_devices)
    else:
        # The BraTS25 GoAT images are not published on Docker Hub yet, so the
        # default (BraTS25_1A / brats25_goat_cmengynn) fails with a pull 404.
        # Pin to the 2024 winner, whose image is available.
        return GoATSegmenter(algorithm=GoATAlgorithms.BraTS24_1, cuda_devices=cuda_devices)


def ensure_segmenter_image_pulled(segmenter_name):
    """Pull the docker image `segmenter_name` needs, synchronously, if it
    isn't already present on this host.

    Call this once, before starting any batch of concurrent BraTS cases (see
    routes.py's batch_run_route) -- not from inside brats_core()/
    _run_brats_inference(), which run per-case *inside* batch_jobs.py's
    concurrent worker pool. The `brats` package's own lazy pull
    (brats.core.docker._ensure_image, invoked from inside infer_single() the
    first time a case needs an image not yet cached) renders a progress bar
    via a `rich.Progress()` created with no explicit `console=`, which
    defaults to rich's process-wide singleton console
    (rich.get_console()). Two worker threads racing to pull the same
    not-yet-cached image at once -- easy the first time a given segmenter
    runs on this host, or after `docker image prune` -- crash one of them
    with rich.errors.LiveError("Only one live display may be active at
    once"), since a Console only tracks one active Live display at a time.
    Pulling here first, synchronously and single-threaded, means every
    worker's own pull-check later finds the image already local and never
    touches that racy code path. Uses the plain docker SDK directly (already
    a dependency of the `brats` package) instead of `brats`'s internal
    helper, so this doesn't touch `rich` at all.

    This is one of two assets that need pre-warming this way before a batch
    -- see ensure_brats_preprocessing_assets_pulled() for the other
    (brainles_preprocessing's registration atlases / SynthStrip weights,
    fetched a step earlier in the pipeline, in _atlas_preprocess()). Both
    are called together from routes.py's batch_run_route.
    """
    segmenter = _build_segmenter(segmenter_name)
    image = segmenter.algorithm.run_args.docker_image
    log(f"Ensuring BraTS segmenter docker image is present: {image} (segmenter={segmenter_name})")
    docker_sdk.from_env().images.pull(image)


def ensure_brats_preprocessing_assets_pulled():
    """Fetch brainles_preprocessing's registration atlases and SynthStrip
    skull-stripping weights from Zenodo, synchronously, if either isn't
    already present on this host.

    Call this once, before starting any batch of concurrent BraTS cases (see
    routes.py's batch_run_route) -- not from inside brats_core()/
    _atlas_preprocess(), which run per-case *inside* batch_jobs.py's
    concurrent worker pool. _atlas_preprocess()'s AtlasCentricPreprocessor
    lazily fetches both assets itself, the first time a case needs one not
    yet cached, via brainles_preprocessing.utils.zenodo.fetch_atlases()
    (called from modality.py) and fetch_synthstrip() (called from
    brain_extraction/synthstrip.py) -- each rendering a progress bar via a
    `rich.Progress()` created with no explicit `console=`, same
    process-wide-singleton-console failure mode as
    ensure_segmenter_image_pulled's docstring describes for the BraTS
    segmenter's own docker image, just one pipeline step earlier (this step
    runs before the segmenter's image is even touched). Several worker
    threads racing to fetch the same not-yet-cached asset at once crash all
    but one of them with rich.errors.LiveError("Only one live display may
    be active at once"). This -- not the segmenter's docker image, which
    ensure_segmenter_image_pulled already covered -- is what actually took
    down almost every case of the Aaa_Brats_20260730 GoAT batch run on this
    host (2026-08-06): SynthStrip's weights had never been downloaded here
    before, so all 9 concurrent workers (BATCH_JOB_CONCURRENCY) raced to
    fetch them at once on their first case. Fetching both here first,
    synchronously and single-threaded, means every worker's own fetch-check
    later finds everything already local and never touches either racy code
    path.
    """
    log("Ensuring BraTS preprocessing assets (registration atlases, SynthStrip weights) are present")
    ensure_brainles_assets()


def _run_brats_inference(preprocessed_dir, output_dir, segmenter_name='goat', gpu_index=None):
    """Run the chosen BraTS segmenter on the preprocessed NIfTIs from
    _atlas_preprocess() and write segmentation_<segmenter_name>.nii.gz (in
    atlas space) to output_dir. Returns the path to that file.

    gpu_index pins the segmenter's container to one GPU (cuda_devices) --
    required by batch_jobs.py's worker pool (see brats_core's docstring).
    None (the interactive per-study route's default) keeps the original
    hardcoded '0' behavior. Assumes the segmenter's docker image is already
    pulled if running as part of a batch -- see
    ensure_segmenter_image_pulled()."""
    cuda_devices = '0' if gpu_index is None else str(gpu_index)
    segmenter = _build_segmenter(segmenter_name, cuda_devices=cuda_devices)

    output_file = os.path.join(output_dir, f'segmentation_{segmenter_name}.nii.gz')
    inference_log = os.path.join(output_dir, f'brats_inference_{segmenter_name}.log')
    log(f"Running BraTS segmenter '{segmenter_name}' on {preprocessed_dir} (cuda_devices={cuda_devices})")
    # The brats package runs and captures the container itself; following its
    # log_file is the only way to see that output while it runs.
    with follow_file(inference_log, name=f'brats_{segmenter_name}'), timed(f'BraTS {segmenter_name} inference'):
        segmenter.infer_single(
            t1c=os.path.join(preprocessed_dir, 't1c_bet_normalized.nii.gz'),
            t1n=os.path.join(preprocessed_dir, 't1n_bet_normalized.nii.gz'),
            t2f=os.path.join(preprocessed_dir, 'flair_bet_normalized.nii.gz'),
            t2w=os.path.join(preprocessed_dir, 't2_bet_normalized.nii.gz'),
            output_file=output_file,
            # Capture the container's stdout/stderr so a silent "0 output files"
            # failure is diagnosable. Inspect this file if inference produces
            # nothing. Named per segmenter_name, not a plain 'brats_inference.log'
            # -- output_dir is shared across every BraTS mode selected for one
            # batch case (see routes.py's _run_brats_tool), so an unqualified
            # name would have each mode silently overwrite the previous mode's
            # log.
            log_file=inference_log,
        )
    return output_file


def _transform_brats_segmentation(segmentation_file, transform_file, reference_file, output_file):
    """Warp a BraTS segmentation (in atlas space) back into the original t1c
    geometry, using the inverse of the atlas-registration transform
    _atlas_preprocess() saved."""
    applier = TransformApplier(verbose=True)
    applier.apply_transform(
        segmentation_file=segmentation_file,
        transform_file=transform_file,
        reference_file=reference_file,
        output_file=output_file,
    )


def brats_core(input_dir, output_dir, segmenter_name='goat', gpu_index=None):
    """Run the full BraTS tumor segmentation pipeline on already-staged local
    NIfTI input, without touching dcm4chee: register/skull-strip/normalize
    onto the atlas, run `segmenter_name`, and warp the result back into the
    input's native t1c geometry.

    input_dir must contain t1n.nii.gz, t1c.nii.gz, t2.nii.gz, flair.nii.gz.
    Intermediate and final files are written under output_dir. Callers may
    reuse the same output_dir across several calls with different
    segmenter_name for the same input (e.g. several BraTS modes selected for
    one batch case, see routes.py's _run_brats_tool) to run the
    (segmenter-independent, and slow) registration/skull-strip/normalize
    step only once: _atlas_preprocess() already skips it when its output
    files are already there. Every other file this writes is namespaced by
    segmenter_name, so re-running with a different mode against the same
    output_dir never clobbers a prior mode's result. gpu_index pins both the
    preprocessing and inference steps to one GPU -- required by
    batch_jobs.py's worker pool, where several of these run at once and must
    not all default to/contend for the same device. None (the interactive per-study
    route's default) picks the emptiest card (choose_gpu_index), so both
    steps still share one. Returns a dict:
      - 'segmentation_path': path to the written native-space segmentation NIfTI
      - 'segmentation': the segmentation as a (rows, cols, slices) array
      - 'segmentation_labels' / 'segmentation_values': for upload_segmentation()
      - 'volume_table': per-tumor-zone volumetry DataFrame (segment_volume_table)
    """
    gpu_index = choose_gpu_index(gpu_index)
    _atlas_preprocess(input_dir, output_dir, gpu_index=gpu_index)

    preprocessed_dir = os.path.join(output_dir, 'normalized_bet')
    segmentation_atlas_space = _run_brats_inference(
        preprocessed_dir, output_dir, segmenter_name=segmenter_name, gpu_index=gpu_index
    )

    transform_file = os.path.join(output_dir, 'transformations', 't1c', '2_M_atlas__t1c.mat')
    reference_file = os.path.join(input_dir, 't1c.nii.gz')
    output_file = os.path.join(output_dir, f'segmentation_transformed_{segmenter_name}.nii.gz')
    _transform_brats_segmentation(segmentation_atlas_space, transform_file, reference_file, output_file)

    seg_img = nib.load(output_file)
    segmentation = seg_img.get_fdata()

    return {
        'segmentation_path': output_file,
        'segmentation': segmentation,
        'segmentation_labels': BRATS_SEGMENTATION_LABELS,
        'segmentation_values': BRATS_SEGMENTATION_VALUES,
        # BraTS itself returns no volumetry -- unlike FastSurfer -- so it's
        # computed from the mask here, once, for both the per-study route's
        # PACS table and the batch pipeline's overview row/CSV.
        'volume_table': segment_volume_table(
            segmentation, BRATS_SEGMENTATION_LABELS, BRATS_SEGMENTATION_VALUES,
            affine=seg_img.affine,
        ),
    }


def brats_batch_stats(result):
    """Physical volume (mm3) of every BraTS tumor zone, for the batch
    pipeline's results_overview.csv (see routes.py's _run_brats_tool) --
    flattened from the volume table brats_core() already computed."""
    return segment_volume_stats(result['volume_table'])


### ---------------------------------------------------------------------- ###
### batch pipeline: write core outputs to a plain output tree (no dcm4chee) ###
### ---------------------------------------------------------------------- ###

def save_brats_core_outputs(result, out_dir):
    """Write brats_core()'s outputs to `out_dir` as plain files, for the
    batch pipeline's ZIP-in/ZIP-out route (POST /batch/run, when 'brats' is
    one of the selected tools). No DICOM SEG here -- a batch case has no
    dcm4chee study to attach one to. Copies just the native-space
    segmentation NIfTI plus its volumetry table -- the atlas-space
    preprocessing intermediates under output_dir (normalized_bet/,
    transformations/) stay in scratch, not the result ZIP."""
    os.makedirs(out_dir, exist_ok=True)
    shutil.copyfile(
        result['segmentation_path'],
        os.path.join(out_dir, os.path.basename(result['segmentation_path'])),
    )
    result['volume_table'].to_csv(os.path.join(out_dir, 'volume_table.csv'), index=False)


### ---------------------------------------------------------------------- ###
### wrapper: stage-in from dcm4chee -> core -> stage-out to dcm4chee       ###
### ---------------------------------------------------------------------- ###

def brats_wrapper(studyInstanceUID, segmenter_name='goat'):
    """End-to-end BraTS tumor segmentation for a study: download the required
    MR series, preprocess/register them to the atlas, run `segmenter_name`,
    warp the result back to native space, and upload it as a DICOM SEG plus a
    per-tumor-zone volumetry table as a Secondary Capture.
    This is what the /tumorseg/<segmenter_name>/<studyInstanceUID> route calls."""
    required_series = ['t1n', 't1c', 't2', 'flair']
    study_dir = os.path.join(BRATS_STORAGE_ROOT, studyInstanceUID)
    output_dir = os.path.join(study_dir, 'output')

    # stage-in
    UID_dic = store_required_series_as_nifti(studyInstanceUID,
                                              required_series=required_series,
                                              storage_path=study_dir)
    # store_required_series_as_nifti returns a falsy dict when a modality is
    # missing; fail early with an actionable message instead of crashing deep
    # inside preprocessing / on a KeyError below.
    if not UID_dic or any(s not in UID_dic for s in required_series):
        raise ValueError(
            f"Study {studyInstanceUID} is missing one or more required series. "
            f"Segmentation needs all of: {', '.join(required_series)}."
        )

    # core
    result = brats_core(study_dir, output_dir, segmenter_name=segmenter_name)

    # stage-out
    upload_segmentation(
        result['segmentation'],
        labels=result['segmentation_labels'], values=result['segmentation_values'],
        study_uid=studyInstanceUID,
        series_uid=UID_dic['t1c'],
        seg_description='BRATS ' + segmenter_name,
    )

    upload_segment_volume_table(
        result['volume_table'],
        title=f'BraTS ({segmenter_name}): Tumor Zone Volumes',
        study_uid=studyInstanceUID, series_uid=UID_dic['t1c'],
        series_description=f'BRATS {segmenter_name} Volumes',
        series_number=_VOLUME_TABLE_SERIES_NUMBER,
    )
