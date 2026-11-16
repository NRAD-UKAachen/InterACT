"""
Turns an intensity NIfTI volume (not a mask) back into a real DICOM image
series, by copying the geometry/header of a reference series and
substituting its pixel data slice by slice. Used by the co-registration
pipeline to upload each coregistered moving series back into the study.

Ported from the coregistration prototype's upload_coregistered_nifti().
"""
import copy

import numpy as np
import highdicom

from .backend_helpers import client


def build_dicom_series_from_volume(
    volume,
    reference_instances,
    series_description,
    series_number,
    modality=None,
    window_center=None,
    window_width=None,
):
    """
    Build a new DICOM series by copying the geometry/header of
    ``reference_instances`` (already sorted along the slice normal, e.g. via
    sort_instances_by_slice_normal) and substituting ``volume``'s pixel data
    slice by slice.

    ``volume`` must be shape (rows, cols, num_slices) with
    num_slices == len(reference_instances), matching the orientation
    store_series_as_nifti() saves (and thus what brainles_preprocessing hands
    back after resampling onto the reference/center grid).

    Returns the list of new pydicom Datasets; nothing is uploaded yet, see
    store_dicom_series().
    """
    # A (rows, cols, num_slices, 3) uint8 array is stored as a true colour
    # (RGB) series instead of int16 greyscale, same as
    # build_dicom_series_from_reoriented_volume() does -- see its docstring.
    is_rgb = volume.ndim == 4 and volume.shape[-1] == 3
    if is_rgb:
        volume = volume.astype(np.uint8)
    rows, cols, num_slices = volume.shape[:3]
    if num_slices != len(reference_instances):
        raise ValueError(
            f"Volume has {num_slices} slices but {len(reference_instances)} reference instances were given"
        )

    new_series_uid = highdicom.UID()

    if not is_rgb and (window_center is None or window_width is None):
        mean_val = np.mean(volume)
        std_val = np.std(volume)
        window_center = int(mean_val) if window_center is None else window_center
        window_width = int(4 * std_val) if window_width is None else window_width

    slice_thickness = float(getattr(reference_instances[0], "SliceThickness", 1.0))
    pixel_spacing = reference_instances[0].PixelSpacing

    new_instances = []
    for i in range(num_slices):
        img = volume[:, :, i]
        source_ds = reference_instances[i]

        ds = source_ds.copy()
        ds.FrameOfReferenceUID = source_ds.FrameOfReferenceUID
        ds.file_meta = source_ds.file_meta.copy()
        ds.SOPInstanceUID = highdicom.UID()
        ds.SeriesInstanceUID = new_series_uid
        ds.SeriesDescription = series_description
        ds.SeriesNumber = series_number
        ds.InstanceNumber = source_ds.InstanceNumber
        if modality:
            ds.Modality = modality

        ds.Rows, ds.Columns = rows, cols
        ds.PixelData = (img.astype(np.uint8) if is_rgb else img.astype(np.int16)).tobytes()

        # Spatial fields are copied straight from the reference slice - the
        # volume is assumed to already be resampled onto that grid.
        ds.ImagePositionPatient = source_ds.ImagePositionPatient
        ds.ImageOrientationPatient = source_ds.ImageOrientationPatient
        ds.SliceThickness = slice_thickness
        ds.PixelSpacing = pixel_spacing

        if is_rgb:
            # Colour is stored pixel-interleaved (R,G,B per pixel), which is
            # PlanarConfiguration 0 and the layout `volume[:, :, i]` already
            # has. Rescale, window and pixel-range attributes are meaningless
            # for RGB and are removed rather than left over from the reference
            # instance, where a stale WindowCenter (or a
            # LargestImagePixelValue from a 16-bit source) makes viewers
            # mis-handle the series.
            ds.SamplesPerPixel = 3
            ds.PhotometricInterpretation = "RGB"
            ds.PlanarConfiguration = 0
            ds.BitsAllocated = 8
            ds.BitsStored = 8
            ds.HighBit = 7
            ds.PixelRepresentation = 0
            for attribute in ('RescaleIntercept', 'RescaleSlope',
                              'WindowCenter', 'WindowWidth',
                              'SmallestImagePixelValue', 'LargestImagePixelValue',
                              'PixelPaddingValue'):
                if attribute in ds:
                    delattr(ds, attribute)
        else:
            ds.SamplesPerPixel = 1
            ds.PhotometricInterpretation = "MONOCHROME2"
            ds.BitsAllocated = 16
            ds.BitsStored = 16
            ds.HighBit = 15
            ds.PixelRepresentation = 1

            ds.RescaleIntercept = 0
            ds.RescaleSlope = 1
            ds.WindowCenter = window_center
            ds.WindowWidth = window_width

        new_instances.append(ds)

    return new_instances


def build_dicom_series_from_reoriented_volume(
    volume,
    affine_ras,
    reference_instance,
    series_description,
    series_number,
    modality="MR",
    window_center=None,
    window_width=None,
):
    """
    Build a new DICOM series directly from a NIfTI-space volume + its RAS+
    affine (e.g. after fslreorient2std), instead of copying an existing
    series' per-slice geometry like build_dicom_series_from_volume() does.

    Use this when the volume's own orientation/slice count no longer matches
    any existing series in the study (a genuine reorientation, not a
    same-grid resample) -- e.g. uploading a properly axis-aligned copy of an
    obliquely-acquired series so plain axial/coronal/sagittal viewing (and
    MPR) shows true anatomical planes instead of oblique cuts. See
    docker-context-gui's fsl_clinical.py reorient_to_std() and
    API/app/hippocampus_functions.py's _reorient_to_std() for why an oblique
    acquisition needs this in the first place.

    ``volume`` must be a (rows, cols, num_slices) array in nibabel's own
    array layout -- i.e. exactly what nib.load(path).get_fdata() returns,
    paired with that same image's ``affine`` (nibabel affines are always
    RAS+ world by convention, regardless of the source data's orientation).
    A (rows, cols, num_slices, 3) uint8 array is also accepted and stored as
    a true colour (RGB) series instead of int16 greyscale -- for volumes
    whose information *is* the colour rather than an intensity, such as a
    diffusion direction-encoded colour-FA map or a tract density overlaid on
    FA (see dti_functions.py). Colour series carry no rescale or windowing;
    the stored bytes are what gets displayed.

    This is the exact inverse of the LPS-to-RAS conversion
    backend_helpers.store_series_as_nifti() performs when building a NIfTI
    from DICOM: same variable roles (row/col cosine, row/col/slice
    spacing, origin), solved in the opposite direction. Because
    fslreorient2std only ever applies 0/90/180/270-degree axis
    permutations/flips (never an oblique rotation), the resulting
    ImageOrientationPatient comes out axis-aligned (e.g. components are 0,
    1, or -1), not an approximation.

    ``reference_instance`` supplies patient/study/equipment identifiers
    only (e.g. any instance from the series that was reoriented) -- none of
    its spatial fields are used.

    Returns the list of new pydicom Datasets; nothing is uploaded yet, see
    store_dicom_series().
    """
    is_rgb = volume.ndim == 4 and volume.shape[-1] == 3
    if is_rgb:
        volume = volume.astype(np.uint8)
    rows, cols, num_slices = volume.shape[:3]

    # RAS -> LPS is the same self-inverse diagonal flip used the other way
    # in store_series_as_nifti().
    ras_to_lps = np.diag([-1, -1, 1, 1])
    affine_lps = ras_to_lps @ affine_ras

    row_vec = affine_lps[0:3, 1]   # voxel axis 1 (columns) -> row direction cosine
    col_vec = affine_lps[0:3, 0]   # voxel axis 0 (rows) -> column direction cosine
    slice_vec = affine_lps[0:3, 2]  # voxel axis 2 (slices) -> slice normal
    origin = affine_lps[0:3, 3]

    row_spacing = float(np.linalg.norm(row_vec))
    col_spacing = float(np.linalg.norm(col_vec))
    slice_thickness = float(np.linalg.norm(slice_vec))
    row_cosine = row_vec / row_spacing
    col_cosine = col_vec / col_spacing

    new_series_uid = highdicom.UID()
    frame_of_reference_uid = highdicom.UID()

    if not is_rgb and (window_center is None or window_width is None):
        mean_val = np.mean(volume)
        std_val = np.std(volume)
        window_center = int(mean_val) if window_center is None else window_center
        window_width = int(4 * std_val) if window_width is None else window_width

    new_instances = []
    for i in range(num_slices):
        img = volume[:, :, i]

        # Dataset.copy() is a shallow copy: it shares the underlying
        # DataElement objects with the source, so mutating an attribute on
        # one "copy" mutates all of them (and the original) in place. Since
        # every iteration here copies the SAME single reference_instance
        # (unlike build_dicom_series_from_volume, which copies a different
        # source per slice), that shared state would otherwise leave every
        # slice with the last iteration's values. deepcopy avoids it.
        ds = copy.deepcopy(reference_instance)
        ds.FrameOfReferenceUID = frame_of_reference_uid
        ds.file_meta = copy.deepcopy(reference_instance.file_meta)
        ds.SOPInstanceUID = highdicom.UID()
        ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        ds.SeriesInstanceUID = new_series_uid
        ds.SeriesDescription = series_description
        ds.SeriesNumber = series_number
        ds.InstanceNumber = i + 1
        if modality:
            ds.Modality = modality

        ds.Rows, ds.Columns = rows, cols
        ds.PixelData = (img.astype(np.uint8) if is_rgb else img.astype(np.int16)).tobytes()

        ds.ImagePositionPatient = list(origin + i * slice_vec)
        ds.ImageOrientationPatient = list(row_cosine) + list(col_cosine)
        ds.SliceThickness = slice_thickness
        # PixelSpacing is [adjacent row spacing, adjacent column spacing]:
        # first the distance covered when the *row* index advances, which is
        # the step along voxel axis 0 (col_spacing here, since col_vec is that
        # axis), then the one for the column index (row_spacing). Ordering
        # these the other way round is invisible for the square in-plane
        # voxels every acquisition in this project has, and silently wrong for
        # anything else -- no distortion, just wrong distances in an image
        # that looks entirely normal.
        ds.PixelSpacing = [col_spacing, row_spacing]

        if is_rgb:
            # Colour is stored pixel-interleaved (R,G,B per pixel), which is
            # PlanarConfiguration 0 and the layout `volume[:, :, i]` already
            # has. Rescale/window attributes are meaningless for RGB and are
            # removed rather than left over from the reference instance,
            # where a stale WindowCenter makes viewers mis-handle the series.
            ds.SamplesPerPixel = 3
            ds.PhotometricInterpretation = "RGB"
            ds.PlanarConfiguration = 0
            ds.BitsAllocated = 8
            ds.BitsStored = 8
            ds.HighBit = 7
            ds.PixelRepresentation = 0
            for attribute in ('RescaleIntercept', 'RescaleSlope',
                              'WindowCenter', 'WindowWidth'):
                if attribute in ds:
                    delattr(ds, attribute)
        else:
            ds.SamplesPerPixel = 1
            ds.PhotometricInterpretation = "MONOCHROME2"
            ds.BitsAllocated = 16
            ds.BitsStored = 16
            ds.HighBit = 15
            ds.PixelRepresentation = 1

            ds.RescaleIntercept = 0
            ds.RescaleSlope = 1
            ds.WindowCenter = window_center
            ds.WindowWidth = window_width

        new_instances.append(ds)

    return new_instances


def build_and_store_dicom_series_from_reoriented_volume(
    volume,
    affine_ras,
    reference_instance,
    series_description,
    series_number,
    modality="MR",
    window_center=None,
    window_width=None,
):
    """Convenience: build_dicom_series_from_reoriented_volume() + store_dicom_series()."""
    new_instances = build_dicom_series_from_reoriented_volume(
        volume, affine_ras, reference_instance, series_description, series_number,
        modality=modality, window_center=window_center, window_width=window_width,
    )
    store_dicom_series(new_instances)
    return new_instances


def store_dicom_series(new_instances):
    """STOW a list of datasets one at a time."""
    for ds in new_instances:
        client.store_instances([ds])


def build_and_store_dicom_series_from_volume(
    volume,
    reference_instances,
    series_description,
    series_number,
    modality=None,
    window_center=None,
    window_width=None,
):
    """Convenience: build_dicom_series_from_volume() + store_dicom_series()."""
    new_instances = build_dicom_series_from_volume(
        volume,
        reference_instances,
        series_description,
        series_number,
        modality=modality,
        window_center=window_center,
        window_width=window_width,
    )
    store_dicom_series(new_instances)
    return new_instances
