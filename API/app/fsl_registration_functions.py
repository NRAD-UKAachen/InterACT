import os
import shutil
import tempfile

import numpy as np
from PIL import Image

from .backend_helpers import client, log, store_required_series_as_nifti, upload_images_as_sc
from .fsl_registration_core import run_registration_qc
from .storage_paths import API_STORAGE_DIR

# FLIRT/FNIRT run in this container, via fsl_registration_core. They used to
# be driven over HTTP against interact-gui's /fsl-clinical/api/registration/qc
# because FSL was installed there and not here; that stopped being true when
# FSL was bind-mounted into this container for
# hippocampus_functions.run_first_all (API/docker-compose.yml), and the
# round-trip -- along with the FSL_CLINICAL_SERVICE_TOKEN that authenticated
# it and the HTTP fetch of each product back out of interact-gui's static
# folder -- outlived its reason.
#
# Registration variants offered as the batch tool's `mode` (see
# _batch_tool_spec in routes.py). 'flirt12' is exactly what the interactive
# /fsl_register_qc/<studyInstanceUID> route sends, so selecting it in a batch
# reproduces that button per study.
#
# 'fnirt' runs FLIRT first and then FNIRT on top of its affine (the endpoint
# chains them itself), and brain-extracts first: FNIRT's default
# T1_2_MNI152_2mm configuration expects the whole head, but BET here also
# keeps the FLIRT stage from being pulled off by neck/shoulder coverage,
# which is the common failure on this project's clinical T1s -- it is the
# slower, more fragile option, offered for cohorts where the affine result
# isn't good enough rather than as the default.
FSL_REGISTRATION_MODES = {
    'flirt12': {'label': 'FLIRT affine, 12 DOF', 'dof': 12, 'cost': 'corratio',
                'bet': False, 'nonlinear': False},
    'flirt6': {'label': 'FLIRT rigid, 6 DOF', 'dof': 6, 'cost': 'corratio',
               'bet': False, 'nonlinear': False},
    'fnirt': {'label': 'FLIRT + FNIRT non-linear (BET first)', 'dof': 12,
              'cost': 'corratio', 'bet': True, 'nonlinear': True},
}


def _run_registration(image_path, params, work_dir):
    """Run one registration+QC job in a scratch directory and return its
    metadata ({status, steps, qc_image, registered_image, quality_score}),
    raising RuntimeError if it did not complete. `params` is one
    FSL_REGISTRATION_MODES entry's parameters.

    Shared by the per-study wrapper and the batch core so both run the same
    pipeline and report a registration that ran but did not converge the
    same way. `work_dir` holds the intermediates (reoriented input, optional
    brain extraction, transform matrix, the 12 QC tiles); callers copy out
    what they want to keep and drop the rest.
    """
    log(f"Running FLIRT registration+QC for {image_path}")
    result = run_registration_qc(
        image_path, work_dir,
        dof=params['dof'], cost=params['cost'],
        bet=params['bet'], nonlinear=params['nonlinear'],
    )
    if result.get("status") != "completed" or not result.get("qc_image"):
        failed = [s for s in result.get("steps") or [] if not s.get("success")]
        detail = "; ".join(
            f"{s.get('description')}: {s.get('error') or (s.get('stderr') or '').strip()}"
            for s in failed
        )
        raise RuntimeError(
            "FSL registration did not complete successfully."
            + (f" {detail}" if detail else "")
        )
    return result


def _read_qc_image(result):
    """The QC overlay snapshot of a finished registration as an RGB uint8
    array, ready for upload_images_as_sc()."""
    return np.array(Image.open(result["qc_image"]).convert("RGB"))


def _fnirt_output(result):
    """The warped volume of a non-linear run, or None if FNIRT didn't
    produce one. `registered_image` always names the FLIRT output, so
    FNIRT's own result has to be picked out of the per-step list alongside
    it (fnirt_nonlinear_register sets 'output_image' only on success)."""
    for step in reversed(result.get('steps') or []):
        if isinstance(step, dict) and 'fnirt' in (step.get('description') or '').lower():
            return step.get('output_image')
    return None


def _work_dir(prefix):
    """Scratch directory for one registration's intermediates.

    Under TMPDIR, which API/docker-compose.yml points into API_storage
    because /tmp is not shared with the host -- the same reason brats needs
    it. Cleaned up by the TemporaryDirectory context that owns it, so a
    100-case cohort no longer leaves a job directory per case behind (which
    is what the batch path used to call interact-gui's DELETE endpoint for).
    """
    return tempfile.TemporaryDirectory(prefix=prefix)


### ---------------------------------------------------------------------- ###
### pure local-file compute -- no dcm4chee access below this point         ###
### ---------------------------------------------------------------------- ###

def fsl_register_core(t1_nifti_path, output_dir, mode='flirt12'):
    """Register an already-staged local T1 NIfTI to the MNI152 template and
    bring the products back onto local disk, without touching dcm4chee --
    the batch equivalent of the other tools' *_core() functions, and what
    routes.py's _run_fsl_register_tool calls.

    `mode` is one of FSL_REGISTRATION_MODES. There is no gpu_index
    parameter -- FLIRT/FNIRT are CPU-only.

    The registered volume and the QC snapshot are written into `output_dir`;
    the intermediates go to a scratch directory that is removed afterwards,
    so a 100-case cohort doesn't leave 100 job directories behind.

    Returns a dict:
      - 'registered_path': the MNI-space registered volume (FLIRT's)
      - 'warped_path': FNIRT's non-linearly warped volume, for mode='fnirt'
        only -- None otherwise (see below)
      - 'qc_image_path': the QC overlay PNG (registered volume over template)
      - 'qc_image': the same snapshot as an RGB uint8 array
      - 'correlation': fslcc correlation with the template, or None if the
        quality score could not be computed
      - 'mode' / 'dof' / 'cost' / 'nonlinear': what was actually run

    In mode='fnirt' both volumes come back, and the QC snapshot and
    correlation still describe the *affine* stage: the overlay is rendered
    and scored inside the FLIRT step, before FNIRT runs on top of its matrix
    (see flirt_register_with_qc / fnirt_nonlinear_register in
    fsl_registration_core). Saving both is what makes that visible rather
    than quietly handing back an affine result labelled non-linear.
    """
    if mode not in FSL_REGISTRATION_MODES:
        raise ValueError(
            f"Unknown FSL registration mode '{mode}' "
            f"(expected one of {', '.join(sorted(FSL_REGISTRATION_MODES))})."
        )
    params = FSL_REGISTRATION_MODES[mode]
    os.makedirs(output_dir, exist_ok=True)

    # The scratch directory goes away with this block, on the failure path
    # too -- over a cohort, one left behind per failed case adds up the same
    # way a successful one does, and the container log still holds what FSL
    # said about the run. Everything worth keeping is copied into
    # `output_dir` before then.
    with _work_dir('fsl_regqc_') as work_dir:
        result = _run_registration(t1_nifti_path, params, work_dir)

        qc_image = _read_qc_image(result)
        qc_image_path = os.path.join(output_dir, 'qc_overlay.png')
        Image.fromarray(qc_image).save(qc_image_path)

        registered_path = shutil.copyfile(
            result["registered_image"], os.path.join(output_dir, 'registered.nii.gz')
        )

        warped_path = None
        if params['nonlinear']:
            warped = _fnirt_output(result)
            if warped is None:
                raise RuntimeError(
                    "FNIRT was requested but no warped volume was produced "
                    "-- the non-linear step did not run or did not succeed."
                )
            warped_path = shutil.copyfile(
                warped, os.path.join(output_dir, 'registered_fnirt.nii.gz')
            )

    return {
        'registered_path': registered_path,
        'warped_path': warped_path,
        'qc_image_path': qc_image_path,
        'qc_image': qc_image,
        'correlation': (result.get("quality_score") or {}).get("correlation"),
        'mode': mode,
        'dof': params['dof'],
        'cost': params['cost'],
        'nonlinear': params['nonlinear'],
    }


def fsl_register_batch_stats(result):
    """Flat {metric: value} row for the batch pipeline's
    results_overview.csv (see routes.py's _run_fsl_register_tool) -- mirrors
    fastsurfer_batch_stats/ldct_batch_stats.

    The one number a template registration produces is its agreement with
    the template (fslcc correlation, the same score the per-study route puts
    in the uploaded series description) -- which is exactly what a cohort
    needs sortable in one column, since it is how outlier cases whose
    registration silently failed get found. The parameters are carried along
    so a results_overview.csv holding two registration modes side by side
    still says which column was which.

    For mode='fnirt' the correlation is the affine stage's, not the warped
    result's: it is computed inside the FLIRT step, before FNIRT runs on top
    of its matrix. Registration_Nonlinear marks those rows."""
    return {
        'Registration_Correlation': result['correlation'],
        'Registration_DOF': result['dof'],
        'Registration_Cost': result['cost'],
        'Registration_Nonlinear': result['nonlinear'],
    }


### ---------------------------------------------------------------------- ###
### batch pipeline: write core outputs to a plain output tree (no dcm4chee) ###
### ---------------------------------------------------------------------- ###

def save_fsl_register_core_outputs(result, out_dir):
    """Write fsl_register_core()'s products to `out_dir` as plain files, for
    the batch pipeline (POST /batch/run with 'fsl_register' selected). No
    DICOM Secondary Capture here -- a batch case has no dcm4chee study to
    attach one to, so the QC snapshot the per-study route uploads as a
    series is just the PNG. A non-linear run additionally writes FNIRT's
    warped volume next to the affine one the QC snapshot actually shows (see
    fsl_register_core)."""
    os.makedirs(out_dir, exist_ok=True)
    shutil.copyfile(result['registered_path'], os.path.join(out_dir, 'registered_mni.nii.gz'))
    shutil.copyfile(result['qc_image_path'], os.path.join(out_dir, 'qc_overlay.png'))
    if result.get('warped_path'):
        shutil.copyfile(result['warped_path'], os.path.join(out_dir, 'registered_mni_fnirt.nii.gz'))


### ---------------------------------------------------------------------- ###
### wrapper: stage-in from dcm4chee -> core -> stage-out to dcm4chee       ###
### ---------------------------------------------------------------------- ###

def fsl_register_qc_wrapper(studyInstanceUID):
    """Register the study's T1 series to the MNI152 template with FSL FLIRT
    (default params: 12 DOF, corratio cost, no BET/FNIRT) and upload the QC
    overlay snapshot as a DICOM Secondary Capture series. This is what the
    /fsl_register_qc/<studyInstanceUID> route calls."""
    required_series = ['t1n']
    storage_path = f'{API_STORAGE_DIR}/fsl_registration/{studyInstanceUID}/'
    UID_dic = store_required_series_as_nifti(studyInstanceUID, required_series, storage_path)
    if not UID_dic or 't1n' not in UID_dic:
        raise ValueError(f"Study {studyInstanceUID} is missing a T1 series required for FSL registration.")

    # The interactive route keeps its original behavior: one study at a
    # time, and its products stay on disk to be looked at afterwards. They
    # just sit in this study's own API_storage directory now instead of a
    # job directory inside interact-gui. Pruning them is
    # fsl_register_core()'s cohort-scale concern, not this route's.
    result = _run_registration(
        os.path.join(storage_path, 't1n.nii.gz'),
        FSL_REGISTRATION_MODES['flirt12'],
        os.path.join(storage_path, 'registration_qc'),
    )
    qc_image_array = _read_qc_image(result)

    correlation = (result.get("quality_score") or {}).get("correlation")
    series_description = "FSL Registration QC"
    if correlation is not None:
        series_description += f" (correlation={correlation:.3f})"

    reference_instance = client.retrieve_series(
        study_instance_uid=studyInstanceUID, series_instance_uid=UID_dic['t1n']
    )[0]

    upload_images_as_sc(
        [qc_image_array], study_uid=studyInstanceUID, reference_instance=reference_instance,
        series_description=series_description, series_number=6100,
    )
