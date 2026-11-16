"""
Converts a user-uploaded NIfTI (.nii/.nii.gz) volume into a DICOM series and
STOWs it to dcm4chee, for the OHIF viewer's "Upload" flow. Geometry
(ImageOrientationPatient/ImagePositionPatient/PixelSpacing/SliceThickness) is
derived from the NIfTI affine by nii2dcm; patient/study identifiers come from
the caller (the user-filled or randomized metadata form in OHIF), since a
bare NIfTI file has no DICOM header to source them from.
"""
import os
import shutil
import tempfile

import numpy as np
import nibabel as nib
import pydicom
from nii2dcm.run import run_nii2dcm

from .backend_helpers import client, log

# SOP Class UIDs for the modalities exposed in the upload form. Unrecognized
# modalities fall back to Secondary Capture, which has no modality-specific
# pixel/geometry constraints.
SOP_CLASS_UIDS = {
    'CT': '1.2.840.10008.5.1.4.1.1.2',
    'MR': '1.2.840.10008.5.1.4.1.1.4',
    'PT': '1.2.840.10008.5.1.4.1.1.128',
    'OT': '1.2.840.10008.5.1.4.1.1.7',
}


def _shift_nifti_to_nonnegative(nifti_path, work_dir):
    """nii2dcm casts voxel data straight to uint16 with no offset, so any
    negative values (e.g. CT Hounsfield units, which routinely go below zero)
    would silently wrap around to huge positive integers instead of clipping
    sensibly. Shift the volume so its minimum is 0 before handing it to
    nii2dcm, and return the shift so it can be restored via RescaleIntercept
    (true_value = stored_value + intercept).

    Also validates the file is a single-volume (3D) NIfTI, since nii2dcm only
    supports that (a true 4D/timeseries file crashes it with an unrelated
    NameError).
    """
    img = nib.load(nifti_path)
    if img.header['dim'][4] > 1:
        raise ValueError(
            'Only single-volume (3D) NIfTI files are supported for upload; '
            f'this file has {int(img.header["dim"][4])} volumes (4D/timeseries).'
        )

    data = img.get_fdata()
    min_val = float(np.min(data))
    if min_val >= 0:
        return nifti_path, 0.0

    shifted = nib.Nifti1Image(data - min_val, img.affine, img.header)
    shifted_path = os.path.join(work_dir, 'shifted.nii.gz')
    nib.save(shifted, shifted_path)
    return shifted_path, min_val


def convert_and_store_nifti(nifti_path, metadata, study_instance_uid=None, series_number=1):
    """Convert a NIfTI file to a DICOM series and STOW it to dcm4chee.

    `metadata` is a dict of the patient/study fields the upload form
    collected (PatientName, PatientID, PatientBirthDate, PatientSex,
    StudyDate, AccessionNumber, StudyDescription, SeriesDescription,
    Modality). `study_instance_uid`, if given, forces the series into an
    existing study (e.g. when uploading several NIfTI files as multiple
    series of one batch); otherwise nii2dcm mints a fresh one.

    Returns (study_instance_uid, series_instance_uid).
    """
    modality = (metadata.get('Modality') or 'MR').upper()
    sop_class_uid = SOP_CLASS_UIDS.get(modality, SOP_CLASS_UIDS['OT'])

    work_dir = tempfile.mkdtemp(prefix='nifti_upload_')
    dcm_dir = os.path.join(work_dir, 'dcm')
    os.makedirs(dcm_dir, exist_ok=True)
    try:
        source_path, intercept = _shift_nifti_to_nonnegative(nifti_path, work_dir)

        # DicomMRI is the only nii2dcm template that fully populates the
        # Image Pixel module (BitsAllocated/PhotometricInterpretation/etc);
        # its generic 'Dicom' class leaves those blank regardless of what
        # modality we actually want. Modality/SOPClassUID/bit depth are
        # overwritten unconditionally below.
        run_nii2dcm(source_path, dcm_dir, dicom_type='MR')

        slice_files = sorted(os.listdir(dcm_dir))
        if not slice_files:
            raise RuntimeError('nii2dcm produced no DICOM output')

        study_uid = study_instance_uid
        series_instance_uid = None
        instances = []
        for idx, fname in enumerate(slice_files, start=1):
            ds = pydicom.dcmread(os.path.join(dcm_dir, fname))

            if study_uid:
                ds.StudyInstanceUID = study_uid
            study_uid = ds.StudyInstanceUID
            series_instance_uid = ds.SeriesInstanceUID

            ds.Modality = modality
            ds.SOPClassUID = sop_class_uid
            ds.file_meta.MediaStorageSOPClassUID = sop_class_uid

            # nii2dcm's MR template reserves only 12 of the 16 allocated
            # bits, but pixel data is cast to full-range uint16 -- widen
            # BitsStored to match, and restore the negative-value shift.
            ds.BitsAllocated = 16
            ds.BitsStored = 16
            ds.HighBit = 15
            ds.PixelRepresentation = 0
            ds.SamplesPerPixel = 1
            ds.PhotometricInterpretation = 'MONOCHROME2'
            ds.RescaleSlope = 1
            ds.RescaleIntercept = intercept

            ds.PatientName = metadata.get('PatientName', '')
            ds.PatientID = metadata.get('PatientID', '')
            ds.PatientBirthDate = metadata.get('PatientBirthDate', '')
            ds.PatientSex = metadata.get('PatientSex', '')
            ds.StudyDate = metadata.get('StudyDate', '')
            ds.AccessionNumber = metadata.get('AccessionNumber', '')
            ds.StudyDescription = metadata.get('StudyDescription', '')
            ds.SeriesDescription = metadata.get('SeriesDescription', '')
            ds.StudyID = '1'
            ds.SeriesNumber = series_number
            ds.InstanceNumber = idx

            instances.append(ds)

        log(f"Storing {len(instances)} converted instance(s) for series '{metadata.get('SeriesDescription')}'")
        for ds in instances:
            client.store_instances([ds])

        return study_uid, series_instance_uid
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
