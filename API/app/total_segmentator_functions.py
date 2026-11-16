import os
import shutil
import nibabel as nib
import pandas as pd

from .job_logging import run_logged
from .backend_helpers import (
    choose_gpu_index, log, segment_volume_stats, segment_volume_table, store_required_series_as_nifti,
    upload_segment_volume_table, upload_segmentation,
)
from .storage_paths import API_STORAGE_DIR

TOTALSEGMENTATOR_STORAGE_ROOT = f'{API_STORAGE_DIR}/totalsegmentator'

# Series number the per-study route's volumetry table is stored under. Its own
# block, so it can't collide with the SEG (5000) or another tool's tables
# (hippocampus 5001-5003, FastSurfer 6001-6003, FSL registration 6100).
_VOLUME_TABLE_SERIES_NUMBER = 6200

# Label maps for the TotalSegmentator tasks used here, taken from
# totalsegmentator.map_to_binary.class_map. upload_segmentation() needs
# these to build one binary mask per label from the multi-label output.
TASK_CLASS_MAPS = {
    'cerebral_bleed': {
        1: 'intracerebral_hemorrhage',
    },
    'brain_aneurysm': {
        1: 'brain_aneurysm',
    },
    'brain_structures': {
        1: 'brainstem',
        2: 'subarachnoid_space',
        3: 'venous_sinuses',
        4: 'septum_pellucidum',
        5: 'cerebellum',
        6: 'caudate_nucleus',
        7: 'lentiform_nucleus',
        8: 'insular_cortex',
        9: 'internal_capsule',
        10: 'ventricle',
        11: 'central_sulcus',
        12: 'frontal_lobe',
        13: 'parietal_lobe',
        14: 'occipital_lobe',
        15: 'temporal_lobe',
        16: 'thalamus',
    },
}

# Required series role(s) per TotalSegmentator task -- shared by the wrapper
# (dcm4chee stage-in) and the batch pipeline.
TASK_REQUIRED_SERIES = {
    'cerebral_bleed': ['ct_tissue'],
    'brain_aneurysm': ['tof'],
    'brain_structures': ['ct_tissue'],
}

# Modality each task's required series carries -- 'tof' is an MR sequence
# (time-of-flight MRA), the other two are CT. Only used by the batch
# pipeline, to resolve a NIfTI series folder's (unknowable) modality; DICOM
# series folders read their real Modality tag instead and don't need this.
TASK_MODALITY = {
    'cerebral_bleed': 'CT',
    'brain_aneurysm': 'MR',
    'brain_structures': 'CT',
}

# Some TotalSegmentator tasks (here: brain_structures) are gated behind a
# licence key; the package lists them in map_to_binary.commercial_models.
# The key lives in secrets/totalsegmentator/license.txt (TOTALSEG_LICENSE_DIR
# in the root .env), bind-mounted here by API/docker-compose.yml, and is
# passed as `-l` on every gated run. That is cheap: TotalSegmentator only
# validates it online when it differs from the one already stored in
# $TOTALSEG_HOME_DIR/config.json.
TOTALSEG_LICENSE_LOCAL = '/totalseg_license/license.txt'

TOTALSEG_LICENSE_HELP = (
    "TotalSegmentator licence not found. The '{task}' task is not openly "
    "available; a free non-commercial licence can be requested at "
    "https://backend.totalsegmentator.com/license-academic/. Save the key as "
    "secrets/totalsegmentator/license.txt, or point TOTALSEG_LICENSE_DIR in "
    "the root .env at the directory holding it. "
    "See secrets/totalsegmentator/README.md."
)


def totalseg_license_args(task_name):
    """CLI args carrying the licence key for `task_name`: [] for an open task,
    ['-l', <key>] for a gated one. Raises a self-explanatory error if a gated
    task has no key, rather than TotalSegmentator's own message on stdout
    after the input has already been staged."""
    from totalsegmentator.map_to_binary import commercial_models
    if task_name not in commercial_models:
        return []
    try:
        with open(TOTALSEG_LICENSE_LOCAL) as f:
            key = f.read().strip()
    except OSError:
        key = ''
    if not key:
        raise RuntimeError(TOTALSEG_LICENSE_HELP.format(task=task_name))
    return ['-l', key]


### ---------------------------------------------------------------------- ###
### pure local-file compute -- no dcm4chee access below this point         ###
### ---------------------------------------------------------------------- ###

def totalsegmentator_core(input_dir, output_dir, task_name, gpu_index=None):
    """Run the TotalSegmentator CLI for `task_name` on an already-staged
    local NIfTI input and return its output, without touching dcm4chee.

    input_dir must contain exactly one '*.nii.gz' file. The multi-label
    segmentation is written to output_dir/segmentation.nii.gz. gpu_index pins
    the run to one GPU (`--device gpu:<idx>`) instead of TotalSegmentator's
    own device selection -- required by batch_jobs.py's worker pool, where
    several of these run at once and must not all default to/contend for the
    same device. None (the interactive per-study route's default) picks the emptiest card
    (choose_gpu_index), falling back to plain `--device gpu` if that can't be
    read. Returns a dict:
      - 'segmentation_path': path to the written multi-label segmentation NIfTI
      - 'segmentation': the segmentation as a (rows, cols, slices) array
      - 'segmentation_labels' / 'segmentation_values': for upload_segmentation(),
        from TASK_CLASS_MAPS[task_name]
      - 'volume_table': per-segment volumetry DataFrame (segment_volume_table)
    `task_name` must be a key of TASK_CLASS_MAPS.
    """
    os.makedirs(output_dir, exist_ok=True)

    nii_files = [f for f in os.listdir(input_dir) if f.endswith('.nii.gz')]
    if len(nii_files) != 1:
        raise RuntimeError(
            f"Expected exactly one .nii.gz file in {input_dir}, found {len(nii_files)}."
        )
    input_file = os.path.join(input_dir, nii_files[0])
    output_path = os.path.join(output_dir, 'segmentation.nii.gz')
    license_args = totalseg_license_args(task_name)
    gpu_index = choose_gpu_index(gpu_index)
    device = 'gpu' if gpu_index is None else f'gpu:{gpu_index}'

    # The container-wide TMPDIR (API_storage/tmp, see docker-compose.yml) is
    # too long for nnU-Net's multiprocessing.Manager, which binds a Unix
    # socket under $TMPDIR/pymp-*/ and dies with "AF_UNIX path too long" past
    # 107 chars. TotalSegmentator launches no sibling containers, so it
    # doesn't need the host/container-identical path -- local /tmp is fine.
    env = {**os.environ, "TMPDIR": "/tmp"}

    log(f"Running TotalSegmentator task '{task_name}' on {input_file}")
    result = run_logged(
        ["TotalSegmentator", "-i", input_file, "-o", output_path,
         "--task", task_name, "--device", device, "--ml", *license_args],
        env=env,
        redact=license_args[1:],
    )
    if result.returncode != 0:
        # TotalSegmentator often reports the actual problem (e.g. missing
        # license for a gated task) on stdout with an empty stderr, so a
        # stderr-only message can look blank even though the real cause was
        # printed above.
        raise RuntimeError(
            f"TotalSegmentator error (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    class_map = TASK_CLASS_MAPS[task_name]
    labels, values = list(class_map.values()), list(class_map.keys())
    seg_img = nib.load(output_path)
    segmentation = seg_img.get_fdata()

    return {
        'segmentation_path': output_path,
        'segmentation': segmentation,
        'segmentation_labels': labels,
        'segmentation_values': values,
        # Generic across tasks: segment_volume_table() just walks the
        # labels/values already resolved from TASK_CLASS_MAPS[task_name]
        # above, whatever the task. Computed here (off the array and affine
        # already in hand) so the per-study route, the batch overview row and
        # the batch CSV all report one set of numbers.
        'volume_table': segment_volume_table(segmentation, labels, values, affine=seg_img.affine),
    }


def totalsegmentator_batch_stats(result):
    """Physical volume (mm3) of every segment TotalSegmentator produced, for
    the batch pipeline's results_overview.csv (see routes.py's
    _run_totalsegmentator_tool) -- flattened from the volume table
    totalsegmentator_core() already computed."""
    return segment_volume_stats(result['volume_table'])


### ---------------------------------------------------------------------- ###
### batch pipeline: write core outputs to a plain output tree (no dcm4chee) ###
### ---------------------------------------------------------------------- ###

def save_totalsegmentator_core_outputs(result, out_dir):
    """Write totalsegmentator_core()'s outputs to `out_dir` as plain files,
    for the batch pipeline's ZIP-in/ZIP-out route (POST /batch/run, when
    'totalsegmentator' is one of the selected tools). No DICOM SEG here -- a
    batch case has no dcm4chee study to attach one to. Copies the
    multi-label segmentation NIfTI plus a label-map CSV describing what
    each value means and the per-segment volumetry table."""
    os.makedirs(out_dir, exist_ok=True)
    shutil.copyfile(result['segmentation_path'], os.path.join(out_dir, 'segmentation.nii.gz'))
    pd.DataFrame({
        'value': result['segmentation_values'],
        'label': result['segmentation_labels'],
    }).to_csv(os.path.join(out_dir, 'labels.csv'), index=False)
    result['volume_table'].to_csv(os.path.join(out_dir, 'volume_table.csv'), index=False)


### ---------------------------------------------------------------------- ###
### wrapper: stage-in from dcm4chee -> core -> stage-out to dcm4chee       ###
### ---------------------------------------------------------------------- ###

def totalsegmentator_wrapper(studyInstanceUID, task_name):
    """End-to-end TotalSegmentator run for a study: download the series
    `task_name` needs, run TotalSegmentator, and upload the result as a
    DICOM SEG using the label map for `task_name` from TASK_CLASS_MAPS, plus
    a per-segment volumetry table as a Secondary Capture.
    This is what the /totalsegmentator/<task_name>/<studyInstanceUID> route
    calls; `task_name` must be a key of TASK_CLASS_MAPS."""
    required_series = TASK_REQUIRED_SERIES[task_name]
    totalseg_license_args(task_name)  # fail before the stage-in, not after
    study_dir = os.path.join(TOTALSEGMENTATOR_STORAGE_ROOT, studyInstanceUID, task_name)
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
    result = totalsegmentator_core(study_dir, output_dir, task_name)

    # stage-out
    source_series_uid = UID_dic[required_series[0]]
    upload_segmentation(
        result['segmentation'],
        labels=result['segmentation_labels'], values=result['segmentation_values'],
        study_uid=studyInstanceUID,
        series_uid=source_series_uid,
        seg_description='TotalSegmentator ' + task_name,
    )

    upload_segment_volume_table(
        result['volume_table'],
        title=f'TotalSegmentator ({task_name}): Segment Volumes',
        study_uid=studyInstanceUID, series_uid=source_series_uid,
        series_description=f'TotalSegmentator {task_name} Volumes',
        series_number=_VOLUME_TABLE_SERIES_NUMBER,
    )
