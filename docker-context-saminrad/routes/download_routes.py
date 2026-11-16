# -*- coding: utf-8 -*-
"""
Download routes for RADSAM.
Contains routes for downloading segmentation results.
"""

import os
import json
import shutil
import logging
import io
import gzip
import tempfile
import zipfile

import numpy as np
import nibabel as nib
import pydicom
from flask import jsonify, request, send_file

from . import download_bp
from services.archive_service import create_archive_folder
from services.file_loader import current_series_uids
import state

logger = logging.getLogger(__name__)

# How the mask was stacked depends on the segmentation axis: the 2D masks
# are always stacked along the last axis, regardless of which axis was
# traversed (see get_slice_from_volume and create_nifti_mask_from_arrays).
# These transpositions bring the stack back into the layout of the volume,
# i.e. (X=columns, Y=rows, Z=slices).
_STACK_TO_VOLUME_LAYOUT = {
    'axial': None,          # stacked along axis 2 - already the volume layout
    'sagittal': (2, 0, 1),  # (Y, Z, X) -> (X, Y, Z)
    'coronal': (0, 2, 1),   # (X, Z, Y) -> (X, Y, Z)
}

# Name under which the segmentation appears in the database. SeriesNumber
# 5000 is the one under which the API's automatic segmentations are also
# stored (backend_helpers.upload_segmentation).
SEG_SERIES_NUMBER = 5000


@download_bp.route('/download_latest_segmentation')
def download_latest_segmentation():
    """
    Downloads the latest segmentation as a ZIP archive.
    Contains all masks, overlays, cutouts and 3D models.
    """
    try:
        archive_folder = create_archive_folder()
        results_folder = os.path.join(archive_folder, "segmentation_results")

        if not os.path.exists(results_folder):
            return jsonify({'error': 'No segmentation results found'}), 404

        # Create temporary ZIP file
        temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
        temp_zip_path = temp_zip.name
        temp_zip.close()

        # Create ZIP archive
        with zipfile.ZipFile(temp_zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(results_folder):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, results_folder)
                    zipf.write(file_path, arcname)

        # Filename for download
        filename = state.CURRENT_ORIGINAL_FILENAME or 'segmentation'
        download_name = f"{filename}_segmentation_results.zip"

        logger.info(f"Segmentation results ready for download: {download_name}")

        return send_file(
            temp_zip_path,
            as_attachment=True,
            download_name=download_name,
            mimetype='application/zip'
        )

    except Exception as e:
        logger.error(f"Error during download: {e}")
        return jsonify({'error': str(e)}), 500


@download_bp.route('/download_simple_results')
def download_simple_results():
    """
    Downloads only the most important results:
    - 3D models (NIfTI)
    - Segmentation parameters (JSON)
    """
    try:
        archive_folder = create_archive_folder()
        results_folder = os.path.join(archive_folder, "segmentation_results")

        if not os.path.exists(results_folder):
            return jsonify({'error': 'No segmentation results found'}), 404

        # Create temporary ZIP file
        temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
        temp_zip_path = temp_zip.name
        temp_zip.close()

        # Create ZIP archive with only the most important files
        with zipfile.ZipFile(temp_zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # 3D models
            models_folder = os.path.join(results_folder, "3d_models")
            if os.path.exists(models_folder):
                for file in os.listdir(models_folder):
                    file_path = os.path.join(models_folder, file)
                    zipf.write(file_path, os.path.join("3d_models", file))

            # Parameters file
            params_file = os.path.join(results_folder, "segmentation_parameters.json")
            if os.path.exists(params_file):
                zipf.write(params_file, "segmentation_parameters.json")

        # Filename for download
        filename = state.CURRENT_ORIGINAL_FILENAME or 'segmentation'
        download_name = f"{filename}_3d_models.zip"

        logger.info(f"Simple results ready for download: {download_name}")

        return send_file(
            temp_zip_path,
            as_attachment=True,
            download_name=download_name,
            mimetype='application/zip'
        )

    except Exception as e:
        logger.error(f"Error during download: {e}")
        return jsonify({'error': str(e)}), 500


@download_bp.route('/download_nifti/<filename>')
def download_nifti(filename):
    """
    Downloads a specific NIfTI file.
    """
    try:
        archive_folder = create_archive_folder()
        nifti_folder = os.path.join(archive_folder, "segmentation_results", "3d_models")

        if not os.path.exists(nifti_folder):
            return jsonify({'error': 'NIfTI folder not found'}), 404

        # basename: the filename comes from the URL, and without this a
        # '../..' in it could serve up any file in the container.
        filename = os.path.basename(filename)
        file_path = os.path.join(nifti_folder, filename)
        if not os.path.exists(file_path):
            return jsonify({'error': f'File not found: {filename}'}), 404

        return send_file(
            file_path,
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        logger.error(f"Error during NIfTI download: {e}")
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Completing the segmentation: download the result, place it in iRODS, or
# write it back to the database as DICOM-SEG.
#
# The latter two paths deliberately go through the browser and the endpoints
# of the InterACT GUI (/irods/upload and /api-proxy/segmentation_upload) -
# just like the import on the upload page. This container holds neither an
# iRODS nor a Keycloak credential, and only the GUI knows who is currently
# working, so that the PHI audit trail stays complete. What lives here,
# therefore, is only what works without that login: providing the results
# themselves and reporting which paths are even possible.
# ---------------------------------------------------------------------------

def _results_folder():
    """Folder with the results of this session, or None."""
    archive_folder = create_archive_folder()
    results_folder = os.path.join(archive_folder, "segmentation_results")
    return results_folder if os.path.isdir(results_folder) else None


def _analysis_mask_info(results_folder):
    """(path, axis, method) of this session's analysis mask.

    Found in segmentation_parameters.json, which both segmentation paths
    write on completion - SAM under 'segmentation_axis', threshold under
    'view_axis'. If nothing is found there, the newest analysis mask in the
    3d_models folder is used and the axis is read from its filename
    (create_nifti_mask_from_arrays puts it there).

    Returns:
        tuple: (path, axis, method) or (None, None, None)
    """
    models_folder = os.path.join(results_folder, "3d_models")
    if not os.path.isdir(models_folder):
        return None, None, None

    params_file = os.path.join(results_folder, "segmentation_parameters.json")
    if os.path.isfile(params_file):
        try:
            with open(params_file, 'r', encoding='utf-8') as f:
                params = json.load(f)
            filename = (params.get('output_files') or {}).get('analysis_nifti')
            tool_params = params.get('parameters') or {}
            axis = tool_params.get('segmentation_axis') or tool_params.get('view_axis')
            if filename:
                path = os.path.join(models_folder, os.path.basename(filename))
                if os.path.isfile(path):
                    return path, axis, params.get('method')
        except Exception as e:
            logger.warning(f"segmentation_parameters.json could not be parsed: {e}")

    candidates = [f for f in os.listdir(models_folder)
                  if f.endswith('_analysis_mask.nii.gz') and not f.startswith('viewer_')]
    if not candidates:
        return None, None, None

    newest = max(candidates, key=lambda f: os.path.getmtime(os.path.join(models_folder, f)))
    axis = next((a for a in _STACK_TO_VOLUME_LAYOUT if f"_{a}_" in newest), None)
    method = 'sam' if '_SAM_analysis_mask' in newest else 'threshold'
    return os.path.join(models_folder, newest), axis, method


def _seg_upload_availability():
    """Whether the result can be written to the database as DICOM-SEG.

    Returns:
        dict: {'available': bool, 'reason': str|None, 'study_uid', 'series_uid',
               'series_description'}
    """
    info = {'available': False, 'reason': None, 'study_uid': None,
            'series_uid': None, 'series_description': None}

    if state.SOURCE_ORIGIN != 'dcm4chee':
        info['reason'] = ('The data was not imported from the dcm4chee database. '
                          'A SEG object can only be saved to a series that already '
                          'exists there.')
        return info

    # Only the volume path guarantees that axis 2 of the mask corresponds
    # slice for slice to the series (load_dicom_series_as_volume sorts along
    # the slice normal and remembers the order). In the axial fallback,
    # sorting is by SliceLocation/InstanceNumber - that can diverge, and a
    # mask assigned to the wrong slice would be worse in the database than
    # none at all.
    if not state.DICOM_SOURCE_FILES:
        info['reason'] = ('The series could not be stacked as a volume; the '
                          'mapping of mask slices to the original images is '
                          'therefore not guaranteed.')
        return info

    study_uid, series_uid = current_series_uids()
    if not study_uid or not series_uid:
        info['reason'] = 'The loaded images do not belong to a unique series.'
        return info

    try:
        ds = pydicom.dcmread(state.DICOM_SOURCE_FILES[0], force=True, stop_before_pixels=True)
        info['series_description'] = str(getattr(ds, 'SeriesDescription', '') or '')
    except Exception as e:
        logger.warning(f"SeriesDescription could not be read: {e}")

    info.update({'available': True, 'study_uid': study_uid, 'series_uid': series_uid})
    return info


@download_bp.route('/export_options')
def export_options():
    """What can be done with this session's segmentation result.

    The viewer queries this after the 3D mask is completed and builds the
    choices from it: download, save to iRODS, or load into the database as
    DICOM-SEG. The latter two require the logged-in GUI session, and the SEG
    option additionally requires a series imported from dcm4chee.
    """
    results_folder = _results_folder()
    if results_folder is None:
        return jsonify({'results_available': False,
                        'error': 'No segmentation results found'}), 404

    mask_path, axis, method = _analysis_mask_info(results_folder)
    base_name = state.CURRENT_ORIGINAL_FILENAME or 'segmentation'
    method_label = 'Threshold' if method == 'threshold' else 'SAM2'

    seg = _seg_upload_availability()
    if seg['available'] and mask_path is None:
        seg['available'] = False
        seg['reason'] = 'No 3D mask has been created yet.'

    return jsonify({
        'results_available': True,
        'origin': state.SOURCE_ORIGIN,
        'embedded_in_gui': bool(request.headers.get('X-Forwarded-Prefix')),
        'base_name': base_name,
        'method': method,
        'download_filename': f"{base_name}_segmentation_results.zip",
        'mask_available': mask_path is not None,
        'mask_axis': axis,
        'seg': {
            # available/reason/study_uid/series_uid/series_description from
            # _seg_upload_availability(), plus the labels under which the
            # segmentation should appear in the database.
            **seg,
            'segment_label': f"{method_label} Segmentation",
            'seg_series_description': f"{method_label} Segmentation (SAMinRAD)",
            'seg_series_number': SEG_SERIES_NUMBER,
            'model_name': f"SAMinRAD ({method_label})",
        },
    })


@download_bp.route('/export_mask_nifti')
def export_mask_nifti():
    """Provides the analysis mask on the voxel grid of the source series.

    The archived mask is not sufficient for the SEG upload: stacking always
    happens along the axis traversed last, so that a sagittally or coronally
    segmented mask lies in a different axis layout than the volume (the
    stored affine, on the other hand, is always that of the volume). Here it
    is rotated back into the volume layout - i.e. (columns, rows, slices),
    slices ascending along the slice normal as in
    load_dicom_series_as_volume. This is exactly the mapping expected by
    backend_helpers.mask_array_for_series on the other side.
    """
    try:
        results_folder = _results_folder()
        if results_folder is None:
            return jsonify({'error': 'No segmentation results found'}), 404

        mask_path, axis, _method = _analysis_mask_info(results_folder)
        if mask_path is None:
            return jsonify({'error': 'No analysis mask found'}), 404

        img = nib.load(mask_path)
        data = np.rint(np.asanyarray(img.dataobj)).astype(np.uint8)
        if data.ndim != 3:
            return jsonify({'error': f'Mask is not 3D (shape {data.shape})'}), 500

        if axis not in _STACK_TO_VOLUME_LAYOUT:
            return jsonify({'error': f'Unknown segmentation axis: {axis!r}'}), 500

        transposition = _STACK_TO_VOLUME_LAYOUT[axis]
        if transposition is not None:
            data = data.transpose(*transposition)

        volume = state.CURRENT_VOLUME_DATA
        if volume is not None and data.shape != volume.shape:
            return jsonify({
                'error': (f'Mask {data.shape} does not match the loaded volume '
                          f'{volume.shape} - the axis mapping is incorrect.')
            }), 500

        affine = np.array(state.CURRENT_VOLUME_AFFINE, dtype=float)
        out_img = nib.Nifti1Image(np.ascontiguousarray(data), affine)
        out_img.set_sform(affine, code=1)
        out_img.set_qform(affine, code=1)

        base_name = state.CURRENT_ORIGINAL_FILENAME or 'segmentation'
        out_name = f"{base_name}_mask_source_grid.nii.gz"
        # Via memory instead of a temporary file: the mask is only a few
        # hundred kilobytes once compressed, and this way nothing is left
        # behind if the browser aborts the download.
        payload = io.BytesIO(gzip.compress(out_img.to_bytes()))

        logger.info(f"Mask provided for SEG export: {out_name} "
                    f"(axis {axis}, shape {data.shape})")

        return send_file(payload, as_attachment=True, download_name=out_name,
                         mimetype='application/gzip')

    except Exception as e:
        logger.error(f"Error providing the mask: {e}")
        return jsonify({'error': str(e)}), 500
