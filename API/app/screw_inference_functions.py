"""
Runs the trained screw-segmentation model directly against a study's own CT
(retrieved fresh from PACS) as a GPU sibling-container, and uploads the
result as a DICOM SEG.

This needs no pre-processing step and no registry entry, so it works on any CT
a colleague uploads, not just the pre-processed micro-CT specimens the model
was trained on. It replaced an earlier two-step flow whose first half lived in
the portal (see screw_functions.py).
"""
import os

import docker as docker_sdk
import nrrd
import numpy as np
import SimpleITK as sitk

from .backend_helpers import (
    choose_gpu_index, log, segment_volume_stats, segment_volume_table, store_series_as_nifti,
    upload_segment_volume_table, upload_segmentation,
)
from .job_logging import run_container_logged
from .screw_functions import _find_ct_series_uid, _drop_small_components
from .storage_paths import API_STORAGE_DIR

STORAGE_ROOT = f"{API_STORAGE_DIR}/screw_inference"

# The screw-segmentation model ships as a public image with the weights baked
# in, rather than as a checkpoint this deployment has to obtain and mount. That
# image is not published yet, so there is no default: an unset value makes the
# route fail with the message below instead of silently running against
# whatever happens to be in a local path.
#
# The image must provide:
#   /app/infer_and_visualize.py   entrypoint script
#   the trained weights, at the path its --model default points at
# and, given `--ct /data/<file>.nrrd --out /output`, write
# /output/<file>_screw_mask.nrrd. See _run_screw_inference below.
SCREW_MODEL_IMAGE = os.environ.get("SCREW_MODEL_IMAGE", "").strip()

# GPU 3 is reserved for interactive per-study routes -- batch_jobs.py caps
# its worker pool to GPUs 0-2 (BATCH_JOB_NUM_GPUS=3 in docker-compose.yml)
# specifically so this slot stays free for routes like this one. A batch run
# passes its worker slot's own GPU instead (see screw_inference_core).
GPU_INDEX = "3"

# Label/value the screw mask is uploaded and saved under, in both the
# per-study and the batch path.
SCREW_LABELS = ['Screw']
SCREW_VALUES = [1]

# Series number the per-study route's volumetry table is stored under -- see
# screw_functions.py's _VOLUME_TABLE_SERIES_NUMBER (6400) for the block.
_VOLUME_TABLE_SERIES_NUMBER = 6401


def _rescale_and_convert_to_nrrd(nifti_path, nrrd_path):
    """
    Min-max rescale intensity to the model's expected 8-bit [0,255] range and
    save as NRRD. Mirrors the conversion the training data went through: that
    data is uint8 [0,255], but a real DICOM-sourced CT is typically 12-bit
    [0,4095], and infer_and_visualize.py's ScaleIntensityRange(a_min=0,
    a_max=255) assumes the former -- fed the latter unscaled, nearly
    everything (including the screw itself, the densest/brightest material)
    clips into an indistinguishable blob and detection collapses to a handful
    of stray voxels.
    """
    img = sitk.ReadImage(nifti_path)
    arr = sitk.GetArrayFromImage(img).astype(np.float32)

    mn, mx = float(arr.min()), float(arr.max())
    if mx > mn:
        arr = (arr - mn) * (255.0 / (mx - mn))
    arr = np.clip(arr, 0, 255).astype(np.uint8)

    out = sitk.GetImageFromArray(arr)
    out.CopyInformation(img)  # keep original spacing/origin/direction
    sitk.WriteImage(out, nrrd_path)


def _read_inference_mask_nrrd(mask_path, ct_nrrd_path):
    """
    Read the mask NRRD produced by inference. Unlike screw_functions.py's
    _read_screw_mask_nrrd() (used by the registry-based flow, where the mask
    comes from an independently-run offline script with no guaranteed
    relationship to any file this API built), this pipeline builds ct_nrrd_path
    itself and feeds it straight to inference, so the mask is guaranteed to
    come back in that exact same (rows, cols, slices) array shape/order --
    no DICOM Rows/Columns-based row/col matching needed, which is fortunate,
    because that approach is fundamentally unable to detect a needed swap
    when a CT's Rows and Columns are equal (any square reconstruction matrix,
    extremely common on real scanners) -- confirmed on a real 768x768
    clinical CT, where that heuristic silently transposed an otherwise
    correct mask into a 90-degree-rotated one (voxel-level check: the
    non-transposed mask covered 99.7% of the CT's brightest, most
    metal-like voxels; the transposed one covered 10%).
    """
    mask_data, _ = nrrd.read(mask_path)
    ct_shape = nrrd.read_header(ct_nrrd_path)['sizes']
    if tuple(mask_data.shape) != tuple(ct_shape):
        raise ValueError(
            f"Screw mask shape {mask_data.shape} doesn't match the CT it was run on {tuple(ct_shape)}."
        )
    return (mask_data > 0).astype(np.uint8)


def _run_screw_inference(ct_nrrd_path, output_dir, gpu_index=None):
    """
    Run the trained screw-segmentation model on ct_nrrd_path as a GPU
    sibling-container (mirrors wp4_pilot_dataset/training/run_inference.sh),
    pinned to `gpu_index` (every GPU when None). Returns the path to the
    produced mask NRRD.

    Sibling containers share the *host's* docker daemon -- this API
    container only has docker.sock/the docker CLI mounted in, not its own
    separate container runtime -- so every path handed to a volume mount
    must be a host path that resolves identically inside this container
    too, which is exactly why ct_nrrd_path and output_dir must live under
    API_storage (see docker-compose.yml's comment on TMPDIR for the same
    constraint applying to brats_functions.py).
    """
    if not SCREW_MODEL_IMAGE:
        raise RuntimeError(
            "Screw segmentation is unavailable: the model image has not been "
            "published yet. Once it is, set SCREW_MODEL_IMAGE in the root .env "
            "to its reference (see SCREW_MODEL_IMAGE in this module for the "
            "interface the image has to provide)."
        )

    os.makedirs(output_dir, exist_ok=True)
    ct_filename = os.path.basename(ct_nrrd_path)
    ct_dir = os.path.dirname(ct_nrrd_path)

    docker_client = docker_sdk.from_env()
    log(f"Running screw inference (GPU {gpu_index}) on {ct_nrrd_path}")
    try:
        output = docker_client.containers.run(
            image=SCREW_MODEL_IMAGE,
            command=(
                f"python3 /app/infer_and_visualize.py "
                f"--ct /data/{ct_filename} --out /output"
            ),
            # Only this study's input and output. The script and the weights
            # come from the image itself, so nothing model-related is mounted.
            volumes={
                ct_dir: {'bind': '/data', 'mode': 'ro'},
                output_dir: {'bind': '/output', 'mode': 'rw'},
            },
            # Inherit this container's proxy settings rather than repeating
            # them: the sibling container has to reach the same places we do,
            # and both come from HTTP_PROXY in the root .env.
            environment={
                'HTTP_PROXY': os.environ.get('HTTP_PROXY', ''),
                'HTTPS_PROXY': os.environ.get('HTTPS_PROXY', ''),
                'NO_PROXY': os.environ.get('NO_PROXY', ''),
                'NVIDIA_VISIBLE_DEVICES': 'all' if gpu_index is None else str(gpu_index),
            },
            runtime='nvidia',
            remove=True,
        )
        log(f"Screw inference container output:\n{output.decode(errors='replace')}")
    except docker_sdk.errors.ContainerError as e:
        raise RuntimeError(f"Screw inference container failed: {e.stderr.decode(errors='replace') if e.stderr else e}")

    stem = os.path.splitext(ct_filename)[0]
    mask_path = os.path.join(output_dir, f"{stem}_screw_mask.nrrd")
    if not os.path.exists(mask_path):
        raise RuntimeError(f"Screw inference did not produce the expected output: {mask_path}")
    return mask_path


### ---------------------------------------------------------------------- ###
### pure local-file compute -- no dcm4chee access below this point         ###
### ---------------------------------------------------------------------- ###

def screw_inference_core(ct_nifti_path, output_dir, gpu_index=None):
    """Run screw-segmentation inference on an already-staged local CT NIfTI
    and return the cleaned mask, without touching dcm4chee -- the batch
    equivalent of the other tools' *_core() functions, and what
    routes.py's _run_screw_tool calls.

    `ct_nifti_path` is the CT to segment (the per-study route's own
    ct.nii.gz, or the batch pipeline's materialized ct_screw.nii.gz).
    Intermediates (the rescaled NRRD the model expects, the container's raw
    output) are written under output_dir. gpu_index pins the inference
    container to one GPU -- required by batch_jobs.py's worker pool, where
    several of these run at once and must not all land on the same device.
    None (the
    interactive per-study route's default) picks the emptiest card
    (choose_gpu_index).

    Returns a dict:
      - 'segmentation': cleaned (rows, cols, slices) uint8 mask, aligned to
        the CT it was run on -- for upload_segmentation() or
        save_screw_inference_core_outputs()
      - 'ct_nifti_path' / 'ct_nrrd_path' / 'mask_path': the inputs and the
        container's raw output, kept for saving/debugging
      - 'voxel_volume_mm3': voxel volume of the CT grid, for
        screw_inference_batch_stats()
      - 'volume_table': screw volumetry DataFrame (segment_volume_table)
    """
    os.makedirs(output_dir, exist_ok=True)

    ct_nrrd_path = os.path.join(output_dir, 'ct.nrrd')
    _rescale_and_convert_to_nrrd(ct_nifti_path, ct_nrrd_path)

    mask_path = _run_screw_inference(
        ct_nrrd_path, os.path.join(output_dir, 'inference'),
        gpu_index=choose_gpu_index(gpu_index),
    )

    segmentation = _read_inference_mask_nrrd(mask_path, ct_nrrd_path)
    segmentation = _drop_small_components(segmentation)

    spacing = sitk.ReadImage(ct_nifti_path).GetSpacing()
    voxel_volume_mm3 = float(np.prod(spacing))
    return {
        'segmentation': segmentation,
        'ct_nifti_path': ct_nifti_path,
        'ct_nrrd_path': ct_nrrd_path,
        'mask_path': mask_path,
        'voxel_volume_mm3': voxel_volume_mm3,
        # An affine isn't passed (unlike BraTS/TotalSegmentator) because this
        # mask never becomes a NIfTI/affine pair inside the core: it comes back
        # from the model as an NRRD array carrying no affine, so the voxel
        # volume comes off the source CT's spacing instead.
        'volume_table': segment_volume_table(
            segmentation, SCREW_LABELS, SCREW_VALUES, voxel_volume_mm3=voxel_volume_mm3,
        ),
    }


def screw_inference_batch_stats(result):
    """Flat {metric: value} row for the batch pipeline's
    results_overview.csv (see routes.py's _run_screw_tool) -- mirrors
    brats_batch_stats/fastsurfer_batch_stats, flattening the volume table
    screw_inference_core() already computed rather than reloading the mask.
    Adds the raw voxel count and a found/not-found flag on top, since a screw
    either being there or not is the first thing to read off a batch overview.
    """
    voxels = int(np.count_nonzero(result['segmentation']))
    return {
        **segment_volume_stats(result['volume_table']),
        'Screw_Voxels': voxels,
        'Screw_Detected': bool(voxels),
    }


### ---------------------------------------------------------------------- ###
### batch pipeline: write core outputs to a plain output tree (no dcm4chee) ###
### ---------------------------------------------------------------------- ###

def save_screw_inference_core_outputs(result, out_dir):
    """Write screw_inference_core()'s mask to `out_dir` as a plain NIfTI on
    the source CT's grid plus its volumetry table as CSV, for the batch
    pipeline (POST /batch/run with 'screw_infer' selected). No DICOM SEG here
    -- a batch case has no dcm4chee study to attach one to.

    The mask array is in the NRRD's own axis order (x, y, z -- what
    SimpleITK wrote ct.nrrd as, and what the model handed back in that same
    shape), while SimpleITK's array interface is (z, y, x), hence the
    transpose before CopyInformation() stamps the CT's spacing/origin/
    direction onto it. Going through the CT image rather than the mask
    NRRD's own header is deliberate: it guarantees the saved mask lines up
    with the series it was run on even if the inference container's writer
    drops or alters geometry."""
    os.makedirs(out_dir, exist_ok=True)

    ct_image = sitk.ReadImage(result['ct_nifti_path'])
    mask_image = sitk.GetImageFromArray(
        np.transpose(result['segmentation'], (2, 1, 0)).astype(np.uint8)
    )
    mask_image.CopyInformation(ct_image)
    sitk.WriteImage(mask_image, os.path.join(out_dir, 'screw_segmentation.nii.gz'))

    result['volume_table'].to_csv(os.path.join(out_dir, 'volume_table.csv'), index=False)


### ---------------------------------------------------------------------- ###
### wrapper: stage-in from dcm4chee -> core -> stage-out to dcm4chee       ###
### ---------------------------------------------------------------------- ###

def screw_inference_wrapper(studyInstanceUID):
    """
    End-to-end live screw segmentation for a study: retrieve its CT from
    PACS, rescale/convert to what the model expects, run inference, drop
    small false-positive components, and upload the result as a DICOM SEG
    plus a screw volumetry table as a Secondary Capture.
    """
    study_dir = os.path.join(STORAGE_ROOT, studyInstanceUID)
    os.makedirs(study_dir, exist_ok=True)

    # stage-in
    ct_series_uid = _find_ct_series_uid(studyInstanceUID)
    nifti_path = os.path.join(study_dir, 'ct.nii.gz')
    store_series_as_nifti(studyInstanceUID, ct_series_uid, filename=nifti_path)

    # core
    result = screw_inference_core(nifti_path, os.path.join(study_dir, 'output'))

    # stage-out
    upload_segmentation(
        result['segmentation'],
        labels=SCREW_LABELS,
        values=SCREW_VALUES,
        series_uid=ct_series_uid,
        study_uid=studyInstanceUID,
        seg_description='MONAI Screw Segmentation (live inference)',
        colors=[(60, -10, -50)],  # blue, matching screw_functions.py's registry-based SEG
    )

    upload_segment_volume_table(
        result['volume_table'],
        title='MONAI Screw Segmentation (live inference): Screw Volume',
        study_uid=studyInstanceUID, series_uid=ct_series_uid,
        series_description='Screw Segmentation (live inference) Volumes',
        series_number=_VOLUME_TABLE_SERIES_NUMBER,
    )
