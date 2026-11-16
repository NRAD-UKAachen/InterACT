# -*- coding: utf-8 -*-
"""
File loading service for RADSAM.
Contains functions for loading NIfTI, NRRD and DICOM files.
"""

import os
import logging
import traceback
import numpy as np
import nibabel as nib
import pydicom

from config import Config

logger = logging.getLogger(__name__)

# NRRD import (optional)
try:
    import nrrd
    NRRD_AVAILABLE = True
except ImportError:
    logger.warning("pynrrd module not found. NRRD files cannot be loaded.")
    NRRD_AVAILABLE = False


def load_nifti_volume(filepath):
    """
    Loads a NIfTI volume.

    Args:
        filepath: path to the NIfTI file

    Returns:
        tuple: (data_array, affine, voxel_spacing, header) or (None, None, None, None) on error
    """
    try:
        img = nib.load(filepath)
        data_array = np.asarray(img.get_fdata(caching='unchanged', dtype=np.float32))
        affine = img.affine.copy()
        header = img.header.copy()
        voxel_spacing = np.array(header.get_zooms()[:3], dtype=float)
        voxel_spacing[~np.isfinite(voxel_spacing)] = 1.0
        voxel_spacing[voxel_spacing <= 1e-6] = 1.0

        if not np.all(np.isfinite(affine)):
            logger.warning(f"NIfTI affine in {filepath} contains non-finite values. Attempting correction.")
            if not np.all(np.isfinite(affine[:3, :3])):
                affine[:3, :3] = np.diag(voxel_spacing)
            if not np.all(np.isfinite(affine[:3, 3])):
                affine[:3, 3] = 0.0
            affine[3, :] = [0, 0, 0, 1]
            if not np.all(np.isfinite(affine)):
                affine = np.diag(np.append(voxel_spacing, 1.0))

        logger.info(f"NIfTI loaded: {filepath}. Shape: {data_array.shape}, voxel spacing: {voxel_spacing}")
        return data_array, affine, tuple(voxel_spacing), header

    except Exception as e:
        logger.error(f"Error loading the NIfTI file {filepath}: {e}\n{traceback.format_exc()}")
        return None, None, None, None


def load_nrrd_volume(filepath):
    """
    Loads an NRRD volume.

    Args:
        filepath: path to the NRRD file

    Returns:
        tuple: (data_array, affine, voxel_spacing, header) or (None, None, None, None) on error
    """
    if not NRRD_AVAILABLE:
        logger.error("NRRD module not available")
        return None, None, None, None

    try:
        data_array, header = nrrd.read(filepath)
        data_array = data_array.astype(np.float32)
        affine = np.eye(4)

        # Process space directions
        space_dims_key = 'space directions' if 'space directions' in header else 'space_directions'
        sdirs_valid = False

        if space_dims_key in header and header[space_dims_key] is not None:
            sdirs_raw = np.array(header[space_dims_key])
            if sdirs_raw.ndim == 2 and sdirs_raw.shape[0] >= min(3, data_array.ndim) and sdirs_raw.shape[1] == 3:
                for i in range(min(3, data_array.ndim)):
                    if np.all(np.isfinite(sdirs_raw[i])):
                        affine[:3, i] = sdirs_raw[i]
                sdirs_valid = True
            else:
                logger.warning(f"NRRD 'space directions' has an unexpected shape: {sdirs_raw.shape}")

        if not sdirs_valid:
            logger.warning(f"'space directions' invalid/missing in {filepath}. Using 'spacings'.")
            spacings = np.array(header.get('spacings', [1.0] * data_array.ndim)[:min(3, data_array.ndim)], dtype=float)
            spacings[~np.isfinite(spacings)] = 1.0
            spacings[spacings <= 1e-6] = 1.0
            for i in range(len(spacings)):
                affine[i, i] = spacings[i]

        # Process space origin
        space_origin_key = 'space origin' if 'space origin' in header else 'space_origin'
        if space_origin_key in header and header[space_origin_key] is not None:
            origin = np.array(header[space_origin_key][:3], dtype=float)
            if np.all(np.isfinite(origin)) and len(origin) == 3:
                affine[:3, 3] = origin
            else:
                logger.warning(f"'space origin' in {filepath} is invalid or contains non-finite values.")

        # Compute the voxel spacing
        voxel_spacing = np.sqrt(np.sum(affine[:3, :3] * affine[:3, :3], axis=0))
        voxel_spacing[~np.isfinite(voxel_spacing)] = 1.0
        voxel_spacing[voxel_spacing <= 1e-6] = 1.0

        if not np.all(np.isfinite(affine)):
            logger.warning(f"Constructed NRRD affine for {filepath} is not finite. Using fallback affine.")
            affine = np.diag(np.append(voxel_spacing, 1.0))
            if space_origin_key in header and np.all(np.isfinite(np.array(header[space_origin_key][:3]))):
                affine[:3, 3] = header[space_origin_key][:3]

        logger.info(f"NRRD loaded: {filepath}. Shape: {data_array.shape}, voxel spacing: {voxel_spacing}")
        return data_array, affine, tuple(voxel_spacing), header

    except Exception as e:
        logger.error(f"Error loading the NRRD file {filepath}: {e}\n{traceback.format_exc()}")
        return None, None, None, None


def setup_volume_data(data, affine, spacing, header, file_type, filename):
    """
    Sets up the volume data in the global state.

    Args:
        data: 3D NumPy array with the volume data
        affine: 4x4 affine matrix
        spacing: voxel spacing as a tuple
        header: header object (NIfTI or NRRD)
        file_type: file type ('nifti', 'nrrd', 'dicom_series')
        filename: original filename
    """
    import state

    state.CURRENT_VOLUME_DATA = data
    state.CURRENT_VOLUME_AFFINE = affine
    state.CURRENT_VOLUME_VOXEL_SPACING = spacing
    state.CURRENT_VOLUME_HEADER = header
    state.CURRENT_FILE_TYPE = file_type
    state.CURRENT_ORIGINAL_FILENAME = filename

    # Detect absurdly anisotropic voxel spacing (likely a header error, e.g. z=1080 instead of 1)
    # -> invisible in the 2D viewer, but stretches the 3D model. Flagged for the upload confirmation prompt.
    state.SPACING_ANOMALY = None
    try:
        arr = np.abs(np.array(spacing, dtype=float)[:3])
        finite = arr[np.isfinite(arr) & (arr > 0)]
        if finite.size == 3:
            med, mx, mn = float(np.median(finite)), float(finite.max()), float(finite.min())
            if mx / max(mn, 1e-9) > 20.0 or mx > 50.0:
                bad_axis = int(np.argmax(arr))
                cur, sugg = float(arr[bad_axis]), med
                axname = {0: 'X', 1: 'Y', 2: 'Z'}.get(bad_axis, str(bad_axis))
                state.SPACING_ANOMALY = {
                    'axis_index': bad_axis, 'axis_name': axname,
                    'current': round(cur, 3), 'suggested': round(sugg, 3),
                    'message': (f"Axis {axname} has a voxel spacing of {cur:g} mm, while the others are "
                                f"~{sugg:g} mm — likely a header error (otherwise the "
                                f"3D model would be stretched ~{cur/max(sugg,1e-9):.0f}x).")
                }
                logger.warning(f"Unusual voxel spacing: {state.SPACING_ANOMALY['message']}")
    except Exception as e:
        logger.debug(f"Spacing anomaly check skipped: {e}")

    # Compute the slice count for each axis
    for axis in state.AXIS_INDEX_FOR_SLICING:
        state.SLICE_COUNTS[axis] = data.shape[state.AXIS_INDEX_FOR_SLICING[axis]]

    # Compute global auto-windowing (percentile-based, like 3D Slicer)
    try:
        flat = data.flatten()
        flat = flat[np.isfinite(flat)]
        if flat.size > 0:
            p_low  = float(np.percentile(flat, 0.5))
            p_high = float(np.percentile(flat, 99.5))
            state.DEFAULT_WL = (p_high + p_low) / 2.0
            state.DEFAULT_WW = max(1.0, p_high - p_low)
            # Global HU range of the entire volume (for the threshold slider)
            state.GLOBAL_DATA_MIN = float(flat.min())
            state.GLOBAL_DATA_MAX = float(flat.max())
            logger.info(f"Auto-windowing computed: WL={state.DEFAULT_WL:.1f}, WW={state.DEFAULT_WW:.1f} "
                        f"(p0.5={p_low:.1f}, p99.5={p_high:.1f}); "
                        f"global HU range: [{state.GLOBAL_DATA_MIN:.0f}, {state.GLOBAL_DATA_MAX:.0f}]")
        else:
            state.DEFAULT_WL = None
            state.DEFAULT_WW = None
            state.GLOBAL_DATA_MIN = None
            state.GLOBAL_DATA_MAX = None
    except Exception as e:
        logger.warning(f"Auto-windowing failed: {e}")
        state.DEFAULT_WL = None
        state.DEFAULT_WW = None
        state.GLOBAL_DATA_MIN = None
        state.GLOBAL_DATA_MAX = None


def read_dicomdir_records(dicomdir_path):
    """
    Reads a DICOMDIR file and returns a list of image records with full
    series metadata.

    Returns:
        list of dict: [{path, series, series_description, modality, study_description, instance}]
    """
    base_dir = os.path.dirname(dicomdir_path)
    ds = pydicom.dcmread(dicomdir_path)

    records = []
    current_series = 0
    current_series_desc = ''
    current_modality = ''
    current_study_desc = ''

    for record in ds.DirectoryRecordSequence:
        rtype = record.DirectoryRecordType
        if rtype == 'STUDY':
            current_study_desc = str(getattr(record, 'StudyDescription', '') or '')
        elif rtype == 'SERIES':
            current_series = int(getattr(record, 'SeriesNumber', 0))
            current_series_desc = str(getattr(record, 'SeriesDescription', '') or '')
            current_modality = str(getattr(record, 'Modality', '') or '')
        elif rtype == 'IMAGE':
            ref_id = getattr(record, 'ReferencedFileID', None)
            if ref_id is None:
                continue
            if hasattr(ref_id, '__iter__') and not isinstance(ref_id, str):
                rel_path = os.path.join(*[str(p) for p in ref_id])
            else:
                rel_path = str(ref_id).replace('\\', os.sep)
            full_path = os.path.join(base_dir, rel_path)
            if not os.path.exists(full_path):
                continue
            records.append({
                'path': full_path,
                'series': current_series,
                'series_description': current_series_desc,
                'modality': current_modality,
                'study_description': current_study_desc,
                'instance': int(getattr(record, 'InstanceNumber', 9999)),
            })

    records.sort(key=lambda x: (x['series'], x['instance']))
    logger.info(f"DICOMDIR: {len(records)} images found in {dicomdir_path}.")
    return records


def summarize_series(records):
    """
    Groups records by series number and returns metadata per series.

    Returns:
        list of dict: [{number, description, modality, study, count}] sorted by series number
    """
    by_series = {}
    for r in records:
        sn = r['series']
        if sn not in by_series:
            by_series[sn] = {
                'number': sn,
                'description': r['series_description'],
                'modality': r['modality'],
                'study': r['study_description'],
                'count': 0,
            }
        by_series[sn]['count'] += 1
    return [by_series[k] for k in sorted(by_series.keys())]


def pick_default_series(series_summary):
    """
    Selects the most sensible default series:
    prefers CT series, description contains 'ax' (axial), not 'vrt' (3D rendering),
    with the most images. Fallback: series with the most images.
    """
    if not series_summary:
        return None

    def score(s):
        desc = (s['description'] or '').lower()
        is_axial = 'ax' in desc
        is_vrt = 'vrt' in desc or 'vr' == desc.strip()
        is_ct = (s['modality'] or '').upper() == 'CT'
        # higher = better; axial + CT is preferred, VRT is penalized
        return (is_axial and not is_vrt, is_ct and not is_vrt, -1 if is_vrt else 0, s['count'])

    return max(series_summary, key=score)


def find_dicomdir_recursive(root, max_depth=3):
    """
    Recursively searches (up to max_depth) for a DICOMDIR file below root.
    Returns the path or None.
    """
    if not os.path.isdir(root):
        return None
    for depth_dir, dirs, files in os.walk(root):
        rel = os.path.relpath(depth_dir, root)
        depth = 0 if rel == '.' else rel.count(os.sep) + 1
        if depth > max_depth:
            dirs[:] = []
            continue
        for name in files:
            if name in ('DICOMDIR', 'dicomdir'):
                return os.path.join(depth_dir, name)
    return None


def load_dicom_files_from_dicomdir(dicomdir_path, series_number=None):
    """
    Compatibility wrapper: returns the file paths, optionally filtered to one series.
    """
    records = read_dicomdir_records(dicomdir_path)
    if series_number is not None:
        records = [r for r in records if r['series'] == int(series_number)]
    return [r['path'] for r in records]


def load_dicom_series_as_volume(paths):
    """
    Stacks a list of DICOM slice files into an actual 3D volume.

    The slices are sorted by their spatial position (ImagePositionPatient,
    projected onto the slice normal), so the stacking is anatomically correct
    regardless of the input order. The volume is returned in
    (X, Y, Z) layout (axis 0 = columns/X, axis 1 = rows/Y,
    axis 2 = slice/Z), matching the app's volume axis convention
    (axial = axis 2), so axial, sagittal and coronal views are all possible.

    Args:
        paths: list of DICOM file paths (order does not matter)

    Returns:
        tuple: (data, affine, voxel_spacing, header) or (None, None, None, None) on error
    """
    if not paths:
        return None, None, None, None

    try:
        first = pydicom.dcmread(paths[0], force=True, stop_before_pixels=True)
        rows = int(first.Rows)
        cols = int(first.Columns)

        # PixelSpacing = [row_spacing (Y), col_spacing (X)]
        ps = getattr(first, 'PixelSpacing', None) or [1.0, 1.0]
        row_spacing = float(ps[0])
        col_spacing = float(ps[1])

        # Direction cosines (IOP): first 3 = column direction (X), last 3 = row direction (Y)
        iop = [float(v) for v in (getattr(first, 'ImageOrientationPatient', None) or [1, 0, 0, 0, 1, 0])]
        col_cos = np.array(iop[0:3], dtype=float)
        row_cos = np.array(iop[3:6], dtype=float)
        normal = np.cross(col_cos, row_cos)

        # Pass 1: read positions (header only) and sort by slice position
        infos = []
        for p in paths:
            try:
                ds = pydicom.dcmread(p, force=True, stop_before_pixels=True)
                ipp = getattr(ds, 'ImagePositionPatient', None)
                pos = np.array([float(x) for x in ipp], dtype=float) if ipp is not None else None
                proj = float(np.dot(pos, normal)) if pos is not None else float(getattr(ds, 'InstanceNumber', 0) or 0)
                infos.append({'path': p, 'pos': pos, 'proj': proj})
            except Exception as e:
                logger.warning(f"DICOM header of {p} not readable: {e}")
                infos.append({'path': p, 'pos': None, 'proj': float('inf')})

        infos.sort(key=lambda x: x['proj'])
        n = len(infos)

        # Pass 2: write pixel data in sorted order into a preallocated volume (X, Y, Z)
        volume = np.empty((cols, rows, n), dtype=np.float32)
        for k, info in enumerate(infos):
            ds = pydicom.dcmread(info['path'], force=True)
            arr = ds.pixel_array.astype(np.float32)
            slope = float(getattr(ds, 'RescaleSlope', 1))
            intercept = float(getattr(ds, 'RescaleIntercept', 0))
            if slope != 1 or intercept != 0:
                arr = arr * slope + intercept
            if arr.shape != (rows, cols):
                logger.warning(
                    f"DICOM slice {info['path']} has a differing size {arr.shape} != {(rows, cols)}. "
                    f"Volume stacking aborted, falling back to axial mode."
                )
                return None, None, None, None
            volume[:, :, k] = arr.T  # (Y, X) -> (X, Y)

        # Determine slice spacing and direction from the positions
        positions = [info['pos'] for info in infos]
        origin = positions[0] if positions[0] is not None else np.zeros(3)
        if n > 1 and positions[0] is not None and positions[-1] is not None:
            slice_vec = (positions[-1] - positions[0]) / (n - 1)
            slice_spacing = float(np.linalg.norm(slice_vec))
            if slice_spacing > 1e-6:
                slice_dir = slice_vec / slice_spacing
            else:
                slice_spacing = float(getattr(first, 'SliceThickness', 1.0) or 1.0)
                slice_dir = normal
        else:
            slice_spacing = float(getattr(first, 'SliceThickness', 1.0) or 1.0)
            slice_dir = normal

        # Build the affine in (X, Y, Z) layout
        affine = np.eye(4)
        affine[:3, 0] = col_cos * col_spacing      # Axis 0: columns (X)
        affine[:3, 1] = row_cos * row_spacing      # Axis 1: rows (Y)
        affine[:3, 2] = slice_dir * slice_spacing  # Axis 2: slices (Z)
        affine[:3, 3] = origin

        # Remember the stacking order: axis 2 of the mask corresponds exactly to this
        # list. For DICOM SEG export this is the mapping mask frame ->
        # source slice, and the headers provide the study/series UID of the series.
        import state as _state
        _state.DICOM_SOURCE_FILES = [info['path'] for info in infos]

        voxel_spacing = (float(col_spacing), float(row_spacing), float(slice_spacing))
        logger.info(f"DICOM volume stacked: shape={volume.shape}, voxel spacing={voxel_spacing}, {n} slices.")
        return volume, affine, voxel_spacing, first

    except Exception as e:
        logger.error(f"Error stacking the DICOM series into a volume: {e}\n{traceback.format_exc()}")
        return None, None, None, None


def current_series_uids():
    """Study and series instance UID of the currently loaded DICOM series.

    The source is the headers of the stored files themselves, not what the
    browser reported during import: after a series change (/select_series)
    that information would be stale, whereas the headers never are. When
    importing from dcm4chee, WADO-RS delivers the original instances, so the
    UIDs there are exactly those of the series in the database.

    Returns:
        tuple: (study_uid, series_uid) or (None, None) if no DICOM series is
        loaded, or the slices don't all belong to the same series - a
        mixed selection cannot be assigned to any single series as a SEG.
    """
    import state

    paths = state.DICOM_SOURCE_FILES or state.DICOM_IMAGE_FILES
    if not paths:
        return None, None

    study_uids, series_uids = set(), set()
    for path in paths:
        try:
            ds = pydicom.dcmread(path, force=True, stop_before_pixels=True,
                                 specific_tags=['StudyInstanceUID', 'SeriesInstanceUID'])
        except Exception as e:
            logger.warning(f"DICOM header of {path} not readable for UID determination: {e}")
            return None, None
        study_uids.add(getattr(ds, 'StudyInstanceUID', None))
        series_uids.add(getattr(ds, 'SeriesInstanceUID', None))

    if len(study_uids) != 1 or len(series_uids) != 1:
        logger.info(f"Loaded slices come from {len(study_uids)} stud(y/ies) and "
                    f"{len(series_uids)} series - no unambiguous source series.")
        return None, None

    study_uid, series_uid = study_uids.pop(), series_uids.pop()
    if not study_uid or not series_uid:
        return None, None
    return str(study_uid), str(series_uid)


def setup_dicom_series_volume(paths, filename):
    """
    Builds a volume from DICOM slice paths and sets up the state as a
    volumetric dataset (file_type='nifti'), so all three axes
    (axial, sagittal, coronal) are available just as with NIfTI/NRRD.

    Args:
        paths: list of DICOM file paths
        filename: display name (e.g. folder/series name)

    Returns:
        bool: True on success, False on error (the caller then uses the axial fallback)
    """
    data, affine, spacing, header = load_dicom_series_as_volume(paths)
    if data is None or data.ndim != 3:
        return False
    setup_volume_data(data, affine, spacing, header, 'nifti', filename)
    return True


def initialize_data_source():
    """
    Initializes the data source based on the available files in the image folder.
    Supports NRRD, NIfTI and DICOM series.
    """
    import state

    # Reset all global variables
    state.CURRENT_VOLUME_DATA = None
    state.CURRENT_VOLUME_AFFINE = np.eye(4)
    state.CURRENT_VOLUME_VOXEL_SPACING = (1, 1, 1)
    state.CURRENT_VOLUME_HEADER = None
    state.CURRENT_FILE_TYPE = None
    state.CURRENT_ORIGINAL_FILENAME = None
    state.DICOM_IMAGE_FILES = []
    state.DICOM_SOURCE_FILES = []
    state.SLICE_COUNTS = {'axial': 0, 'sagittal': 0, 'coronal': 0}
    state.AVAILABLE_SERIES = []
    state.SELECTED_SERIES_NUMBER = None
    state.DICOMDIR_PATH = None
    state.GLOBAL_DATA_MIN = None
    state.GLOBAL_DATA_MAX = None

    try:
        # Check whether the image folder exists and contains files
        if not os.path.exists(state.IMAGE_FOLDER):
            logger.error(f"Image folder {state.IMAGE_FOLDER} not found.")
            return

        files_in_folder = os.listdir(state.IMAGE_FOLDER)
        if not files_in_folder:
            logger.warning(f"No files found in folder {state.IMAGE_FOLDER}.")
            return

        # 1st attempt: load NRRD files
        if NRRD_AVAILABLE:
            for f in files_in_folder:
                if f.lower().endswith(".nrrd"):
                    data, aff, spacing, header = load_nrrd_volume(os.path.join(state.IMAGE_FOLDER, f))
                    if data is not None and data.ndim == 3:
                        setup_volume_data(data, aff, spacing, header, 'nrrd', os.path.splitext(f)[0])
                        logger.info(f"NRRD workflow. Slices(A,S,C): ({state.SLICE_COUNTS['axial']},{state.SLICE_COUNTS['sagittal']},{state.SLICE_COUNTS['coronal']})")
                        return

        # 2nd attempt: load NIfTI files
        for f in files_in_folder:
            if f.lower().endswith((".nii", ".nii.gz")):
                data, aff, spacing, header = load_nifti_volume(os.path.join(state.IMAGE_FOLDER, f))
                if data is not None and data.ndim == 3:
                    setup_volume_data(data, aff, spacing, header, 'nifti', os.path.splitext(os.path.splitext(f)[0])[0])
                    logger.info(f"NIfTI workflow. Slices(A,S,C): ({state.SLICE_COUNTS['axial']},{state.SLICE_COUNTS['sagittal']},{state.SLICE_COUNTS['coronal']})")
                    return

        # 3a. attempt: detect a DICOMDIR structure (directly or in subfolders up to depth 3)
        dicomdir_path = None
        for name in ['DICOMDIR', 'dicomdir']:
            candidate = os.path.join(state.IMAGE_FOLDER, name)
            if os.path.isfile(candidate):
                dicomdir_path = candidate
                break
        if not dicomdir_path:
            dicomdir_path = find_dicomdir_recursive(state.IMAGE_FOLDER, max_depth=3)

        if dicomdir_path:
            try:
                records = read_dicomdir_records(dicomdir_path)
                if records:
                    series_summary = summarize_series(records)
                    default_series = pick_default_series(series_summary)
                    selected_number = default_series['number'] if default_series else None

                    state.DICOMDIR_PATH = dicomdir_path
                    state.AVAILABLE_SERIES = series_summary
                    state.SELECTED_SERIES_NUMBER = selected_number

                    image_files = [r['path'] for r in records if r['series'] == selected_number]

                    if image_files:
                        # Original name = folder of the DICOMDIR file (e.g. RPS06_42960_Mg_4w)
                        original_filename = os.path.basename(os.path.dirname(dicomdir_path))
                        # Preferred: stack the series into a volume -> all three axes available
                        if setup_dicom_series_volume(image_files, original_filename):
                            logger.info(
                                f"DICOMDIR workflow (volume): {len(series_summary)} series found, "
                                f"default series {selected_number} ({default_series['description']}) "
                                f"loaded as volume {state.CURRENT_VOLUME_DATA.shape}. "
                                f"Slices(A,S,C): ({state.SLICE_COUNTS['axial']},{state.SLICE_COUNTS['sagittal']},{state.SLICE_COUNTS['coronal']})"
                            )
                        else:
                            # Fallback: axial file-list mode (axial view only)
                            state.CURRENT_FILE_TYPE = 'dicom_series'
                            state.CURRENT_ORIGINAL_FILENAME = original_filename
                            state.DICOM_IMAGE_FILES = image_files
                            state.SLICE_COUNTS['axial'] = len(image_files)
                            state.SLICE_COUNTS['sagittal'] = 0
                            state.SLICE_COUNTS['coronal'] = 0
                            logger.info(
                                f"DICOMDIR workflow (axial fallback): {len(series_summary)} series found, "
                                f"default series {selected_number} ({default_series['description']}) "
                                f"loaded with {len(image_files)} slices."
                            )
                        for s in series_summary:
                            marker = '*' if s['number'] == selected_number else ' '
                            logger.info(f"  {marker} Series {s['number']:>3}: {s['count']:>4} img | {s['modality']:<3} | {s['description']}")
                        return
            except Exception as e:
                logger.warning(f"DICOMDIR could not be read, trying fallback: {e}")

        # 3b. attempt: load classic .dcm files
        all_dicom_files = [fname for fname in files_in_folder if fname.lower().endswith((".dcm", ".dicom"))]

        # 3c. fallback: try reading extensionless files as DICOM (e.g. IMG00001 format)
        if not all_dicom_files:
            logger.info("No .dcm files found. Trying to read extensionless files as DICOM...")
            extensionless = [fname for fname in files_in_folder if '.' not in fname]
            for fname in extensionless[:1]:  # Test only the first file
                try:
                    pydicom.dcmread(os.path.join(state.IMAGE_FOLDER, fname), stop_before_pixels=True)
                    all_dicom_files = extensionless
                    logger.info(f"{len(all_dicom_files)} extensionless DICOM files detected.")
                    break
                except Exception:
                    pass

        if not all_dicom_files:
            logger.warning(f"No supported files in {state.IMAGE_FOLDER}.")
            return

        original_filename = os.path.basename(state.IMAGE_FOLDER)

        # Sort the DICOM files
        file_info = []
        for f_dicom in all_dicom_files:
            try:
                dicom_path = os.path.join(state.IMAGE_FOLDER, f_dicom)
                ds = pydicom.dcmread(dicom_path, stop_before_pixels=True)
                file_info.append({
                    'name': f_dicom,
                    'path': dicom_path,
                    'location': float(getattr(ds, 'SliceLocation', float('inf'))),
                    'instance': float(getattr(ds, 'InstanceNumber', float('inf')))
                })
            except Exception as e:
                logger.warning(f"Error reading the DICOM file {f_dicom}: {e}")
                file_info.append({
                    'name': f_dicom,
                    'path': os.path.join(state.IMAGE_FOLDER, f_dicom),
                    'location': float('inf'),
                    'instance': float('inf')
                })

        # Sort by SliceLocation and InstanceNumber
        file_info.sort(key=lambda x: (x['location'], x['instance']))
        sorted_paths = [item['path'] for item in file_info]

        if sorted_paths:
            # Preferred: stack the series into a volume -> all three axes available
            if setup_dicom_series_volume(sorted_paths, original_filename):
                logger.info(
                    f"DICOM series workflow (volume): {state.CURRENT_VOLUME_DATA.shape}. "
                    f"Slices(A,S,C): ({state.SLICE_COUNTS['axial']},{state.SLICE_COUNTS['sagittal']},{state.SLICE_COUNTS['coronal']})"
                )
            else:
                # Fallback: axial file-list mode (axial view only)
                state.CURRENT_FILE_TYPE = 'dicom_series'
                state.CURRENT_ORIGINAL_FILENAME = original_filename
                state.DICOM_IMAGE_FILES = sorted_paths
                state.SLICE_COUNTS['axial'] = len(sorted_paths)
                state.SLICE_COUNTS['sagittal'] = 0
                state.SLICE_COUNTS['coronal'] = 0
                logger.info(f"DICOM series workflow (axial fallback). {len(sorted_paths)} axial slices.")
        else:
            state.CURRENT_FILE_TYPE = None
            logger.warning("No valid DICOM files found.")

    except Exception as e:
        logger.error(f"Error initializing the data source: {e}")
        logger.debug(f"Stack trace: {traceback.format_exc()}")
