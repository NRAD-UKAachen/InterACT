"""
Converts a user-uploaded NRRD (.nrrd) volume into a DICOM series and STOWs it
to dcm4chee, for the OHIF viewer's "Upload" flow. Reuses the NIfTI pipeline
(nifti_upload.convert_and_store_nifti) for the actual DICOM construction --
the only NRRD-specific work is building a nibabel-compatible affine from the
NRRD header, so both formats go through the same tested geometry, negative-
value handling, and metadata-override logic.
"""
import logging
import os
import shutil
import tempfile

import nibabel as nib
import numpy as np
import nrrd

from .nifti_upload import convert_and_store_nifti


def _nrrd_to_affine_volume(nrrd_path):
    """Read a NRRD file and return (volume, affine) in the convention
    nibabel/NIfTI use (world = affine @ [i, j, k, 1], RAS+ world axes)."""
    data, header = nrrd.read(nrrd_path)
    if data.ndim != 3:
        raise ValueError(
            f"Only single-volume (3D) NRRD files are supported for upload (got {data.ndim} dimensions)."
        )

    space_directions = header.get('space directions')
    if space_directions is None:
        raise ValueError("NRRD file is missing a 'space directions' field; cannot determine voxel geometry.")

    direction_matrix = np.array(space_directions, dtype=float)  # row i = direction+spacing of axis i
    affine = np.eye(4)
    affine[:3, :3] = direction_matrix.T
    affine[:3, 3] = header.get('space origin', [0.0, 0.0, 0.0])

    # NRRD's 'space' field declares the coordinate handedness the directions/
    # origin above are given in. Medical NRRDs (3D Slicer, plastimatch, etc.)
    # are almost always LPS, like DICOM; flip to RAS+ for the nibabel/NIfTI
    # convention convert_and_store_nifti() expects.
    space = str(header.get('space', '')).lower()
    if space.startswith('left'):
        affine = np.diag([-1.0, -1.0, 1.0, 1.0]) @ affine
    elif not space.startswith('right'):
        logging.getLogger('interact.nrrd_upload').warning(
            "NRRD 'space' field ('%s') is not left-*/right-*; assuming RAS.", header.get('space'))

    return data.astype(np.float64), affine


def convert_and_store_nrrd(nrrd_path, metadata, study_instance_uid=None, series_number=1):
    """Convert a NRRD file to a DICOM series and STOW it to dcm4chee.

    Converts to an intermediate NIfTI (same voxel array + a world-space
    affine) and hands off to convert_and_store_nifti(), so NRRD gets the
    exact same geometry derivation, negative-value shift, and metadata
    overrides as a direct NIfTI upload. Returns
    (study_instance_uid, series_instance_uid).
    """
    work_dir = tempfile.mkdtemp(prefix='nrrd_upload_')
    try:
        volume, affine = _nrrd_to_affine_volume(nrrd_path)
        nifti_path = os.path.join(work_dir, 'converted.nii.gz')
        nib.save(nib.Nifti1Image(volume, affine), nifti_path)

        return convert_and_store_nifti(
            nifti_path, metadata,
            study_instance_uid=study_instance_uid,
            series_number=series_number,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
