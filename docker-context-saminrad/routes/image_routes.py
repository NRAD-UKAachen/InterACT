# -*- coding: utf-8 -*-
"""
Image routes for RADSAM.
Contains routes for retrieving images, overlays, and masks.
"""

import os
import base64
import logging
import numpy as np
import pydicom
from flask import request, jsonify, send_from_directory, abort

from . import image_bp
from services.image_processing import get_slice_from_volume, apply_windowing, image_to_base64
from services.archive_service import create_archive_folder
from utils.helpers import is_safe_filename
import state

logger = logging.getLogger(__name__)


def _encode_raw_data(pixel_array):
    """
    Encodes raw data for the frontend (for thresholding).

    Args:
        pixel_array: numpy array with pixel data

    Returns:
        tuple: (base64_string, dtype_string, shape_list)
    """
    # Convert to float32 for consistent processing
    raw_data = pixel_array.astype(np.float32)
    raw_bytes = raw_data.tobytes()
    raw_base64 = base64.b64encode(raw_bytes).decode('utf-8')
    return raw_base64, 'float32', list(raw_data.shape)


@image_bp.route('/get_image/<int:index>')
def get_image_route(index):
    """
    Returns an image for a given slice index.
    Supports DICOM series and volumetric files (NIfTI, NRRD).
    """
    # Parameters from the request
    axis = request.args.get('axis', 'axial')
    ww = request.args.get('ww', type=float)
    wl = request.args.get('wl', type=float)

    try:
        if state.CURRENT_FILE_TYPE == 'dicom_series':
            return _get_dicom_image(index, ww, wl)
        elif state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
            return _get_volume_image(index, axis, ww, wl)
        else:
            return jsonify({'error': 'No valid file type loaded'}), 400

    except Exception as e:
        logger.error(f"Error retrieving image: {e}")
        return jsonify({'error': str(e)}), 500


def _get_dicom_image(index, ww, wl):
    """Returns a DICOM image."""
    if not state.DICOM_IMAGE_FILES:
        return jsonify({'error': 'No DICOM files loaded'}), 400

    if index < 0 or index >= len(state.DICOM_IMAGE_FILES):
        return jsonify({'error': f'Invalid index: {index}'}), 400

    dicom_path = state.DICOM_IMAGE_FILES[index]
    ds = pydicom.dcmread(dicom_path, force=True)
    pixel_array = ds.pixel_array.astype(np.float32)

    # Apply rescale values
    slope = float(getattr(ds, 'RescaleSlope', 1))
    intercept = float(getattr(ds, 'RescaleIntercept', 0))
    if slope != 1 or intercept != 0:
        pixel_array = pixel_array * slope + intercept

    # Compute data range for dynamic slider ranges
    finite_vals = pixel_array[np.isfinite(pixel_array)]
    data_min = float(np.min(finite_vals)) if finite_vals.size > 0 else 0.0
    data_max = float(np.max(finite_vals)) if finite_vals.size > 0 else 1.0

    # Apply windowing
    np_rgb, pil_rgb, orig_size, used_ww, used_wl = apply_windowing(pixel_array, ww, wl, ds)

    # Convert to base64
    base64_img = image_to_base64(pil_rgb)

    # Encode raw data for thresholding
    raw_base64, raw_dtype, raw_shape = _encode_raw_data(pixel_array)

    return jsonify({
        'image_data': base64_img,
        'width': orig_size[0],
        'height': orig_size[1],
        'used_ww': used_ww,
        'used_wl': used_wl,
        'data_min': data_min,
        'data_max': data_max,
        'global_data_min': state.GLOBAL_DATA_MIN,
        'global_data_max': state.GLOBAL_DATA_MAX,
        'index': index,
        'filename': os.path.basename(dicom_path),
        'raw_pixel_data_b64': raw_base64,
        'raw_data_dtype': raw_dtype,
        'raw_data_shape': raw_shape,
        'file_type': state.CURRENT_FILE_TYPE,
        'slice_counts': state.SLICE_COUNTS,
        'current_axis': 'axial',
        'current_slice_index': index,
        'original_filename': state.CURRENT_ORIGINAL_FILENAME
    })


def _get_volume_image(index, axis, ww, wl):
    """Returns an image from a volumetric dataset."""
    if state.CURRENT_VOLUME_DATA is None:
        return jsonify({'error': 'No volume loaded'}), 400

    max_index = state.SLICE_COUNTS.get(axis, 0) - 1
    if index < 0 or index > max_index:
        return jsonify({'error': f'Invalid index: {index} (max: {max_index})'}), 400

    # Extract slice
    slice_2d = get_slice_from_volume(state.CURRENT_VOLUME_DATA, index, axis)
    if slice_2d is None:
        return jsonify({'error': f'Could not extract slice {index}'}), 500

    # Compute data range for dynamic slider ranges
    finite_vals = slice_2d[np.isfinite(slice_2d)]
    data_min = float(np.min(finite_vals)) if finite_vals.size > 0 else 0.0
    data_max = float(np.max(finite_vals)) if finite_vals.size > 0 else 1.0

    # Global auto-windowing as fallback (instead of SliceCalc)
    if ww is None and state.DEFAULT_WW is not None:
        ww = state.DEFAULT_WW
    if wl is None and state.DEFAULT_WL is not None:
        wl = state.DEFAULT_WL

    # Apply windowing
    np_rgb, pil_rgb, orig_size, used_ww, used_wl = apply_windowing(slice_2d, ww, wl)

    # Convert to base64
    base64_img = image_to_base64(pil_rgb)

    # Encode raw data for thresholding
    raw_base64, raw_dtype, raw_shape = _encode_raw_data(slice_2d)

    return jsonify({
        'image_data': base64_img,
        'width': orig_size[0],
        'height': orig_size[1],
        'used_ww': used_ww,
        'used_wl': used_wl,
        'data_min': data_min,
        'data_max': data_max,
        'global_data_min': state.GLOBAL_DATA_MIN,
        'global_data_max': state.GLOBAL_DATA_MAX,
        'index': index,
        'axis': axis,
        'raw_pixel_data_b64': raw_base64,
        'raw_data_dtype': raw_dtype,
        'raw_data_shape': raw_shape,
        'file_type': state.CURRENT_FILE_TYPE,
        'slice_counts': state.SLICE_COUNTS,
        'current_axis': axis,
        'current_slice_index': index,
        'original_filename': state.CURRENT_ORIGINAL_FILENAME
    })


@image_bp.route('/slice/<axis>/<int:index>')
@image_bp.route('/serve_original_slice/<axis>/<int:index>')
def serve_original_slice(axis, index):
    """
    Returns an original slice without overlay.
    Used for the preview.
    """
    ww = request.args.get('ww', type=float)
    wl = request.args.get('wl', type=float)

    try:
        if state.CURRENT_FILE_TYPE == 'dicom_series':
            return _get_dicom_image(index, ww, wl)
        elif state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
            return _get_volume_image(index, axis, ww, wl)
        else:
            return jsonify({'error': 'No valid file type loaded'}), 400

    except Exception as e:
        logger.error(f"Error retrieving original slice: {e}")
        return jsonify({'error': str(e)}), 500


@image_bp.route('/overlays_archive/<filename>')
@image_bp.route('/overlay_from_archive/<filename>')
def serve_overlay_from_archive(filename):
    """
    Returns an overlay image from the archive folder.
    """
    if not is_safe_filename(filename):
        abort(400, "Invalid file name")

    archive_folder = create_archive_folder()
    overlays_folder = os.path.join(archive_folder, "segmentation_results", "overlays")

    if not os.path.exists(overlays_folder):
        abort(404, "Overlay folder not found")

    return send_from_directory(overlays_folder, filename)


@image_bp.route('/overlays_fewshot_archive/<filename>')
@image_bp.route('/fewshot_overlay_from_archive/<filename>')
def serve_fewshot_overlay_from_archive(filename):
    """
    Returns a few-shot overlay image from the archive folder.
    """
    if not is_safe_filename(filename):
        abort(400, "Invalid file name")

    archive_folder = create_archive_folder()
    overlays_folder = os.path.join(archive_folder, "segmentation_results", "overlays_few_shot")

    if not os.path.exists(overlays_folder):
        abort(404, "Few-shot overlay folder not found")

    return send_from_directory(overlays_folder, filename)


@image_bp.route('/overlays_s1sfq_archive/<filename>')
@image_bp.route('/s1sfq_overlay_from_archive/<filename>')
def serve_s1sfq_overlay_from_archive(filename):
    """
    Returns an S1SFQ overlay image from the archive folder.
    """
    if not is_safe_filename(filename):
        abort(400, "Invalid file name")

    archive_folder = create_archive_folder()
    overlays_folder = os.path.join(archive_folder, "segmentation_results", "overlays_s1sfq")

    if not os.path.exists(overlays_folder):
        abort(404, "S1SFQ overlay folder not found")

    return send_from_directory(overlays_folder, filename)


@image_bp.route('/overlays_memory_archive/<filename>')
@image_bp.route('/memory_overlay_from_archive/<filename>')
def serve_memory_overlay_from_archive(filename):
    """
    Returns a memory-based few-shot overlay image from the archive folder.
    """
    if not is_safe_filename(filename):
        abort(400, "Invalid file name")

    archive_folder = create_archive_folder()
    overlays_folder = os.path.join(archive_folder, "segmentation_results", "overlays_memory")

    if not os.path.exists(overlays_folder):
        abort(404, "Memory overlay folder not found")

    return send_from_directory(overlays_folder, filename)


@image_bp.route('/nifti_masks_sam/<filename>')
def serve_nifti_mask_sam(filename):
    """
    Returns a SAM NIfTI mask from the archive folder.
    """
    if not is_safe_filename(filename):
        abort(400, "Invalid file name")

    archive_folder = create_archive_folder()
    nifti_folder = os.path.join(archive_folder, "segmentation_results", "3d_models")

    if not os.path.exists(nifti_folder):
        abort(404, "NIfTI folder not found")

    return send_from_directory(nifti_folder, filename)


@image_bp.route('/nifti_masks_thresh/<filename>')
def serve_nifti_mask_thresh(filename):
    """
    Returns a threshold NIfTI mask from the archive folder.
    """
    if not is_safe_filename(filename):
        abort(400, "Invalid file name")

    archive_folder = create_archive_folder()
    nifti_folder = os.path.join(archive_folder, "segmentation_results", "3d_models")

    if not os.path.exists(nifti_folder):
        abort(404, "NIfTI folder not found")

    return send_from_directory(nifti_folder, filename)


@image_bp.route('/list_segmented_masks')
def list_segmented_masks():
    """
    Returns a list of all already segmented SAM2 masks (PNG) in the current archive folder.
    """
    try:
        archive_folder = create_archive_folder()
        masks_folder = os.path.join(archive_folder, "segmentation_results", "masks")
        masks = []
        if os.path.exists(masks_folder):
            for fname in sorted(os.listdir(masks_folder)):
                if fname.endswith('_mask.png'):
                    display_name = fname
                    if '_' in fname:
                        parts = fname.split('_')
                        for p in parts:
                            if p.isdigit():
                                display_name = f"Slice {p}"
                                break
                    masks.append({
                        'filename': fname,
                        'display_name': display_name
                    })
        return jsonify({'success': True, 'masks': masks})
    except Exception as e:
        logger.error(f"Error listing masks: {e}")
        return jsonify({'success': False, 'error': str(e), 'masks': []}), 500


@image_bp.route('/list_fewshot_masks')
def list_fewshot_masks():
    """
    Returns a list of all available masks for few-shot segmentation.
    Combines regular SAM masks and few-shot masks.
    """
    try:
        archive_folder = create_archive_folder()
        results_folder = os.path.join(archive_folder, "segmentation_results")
        masks_folder = os.path.join(results_folder, "masks")
        fs_masks_folder = os.path.join(results_folder, "masks_few_shot")

        all_masks = []

        # Collect regular SAM masks
        if os.path.exists(masks_folder):
            for fname in sorted(os.listdir(masks_folder)):
                if fname.endswith('_mask.png'):
                    display_name = fname
                    slice_info = "Unknown"

                    if state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
                        parts = fname.split('_')
                        for i, part in enumerate(parts):
                            if part in ['axial', 'sagittal', 'coronal'] and i + 1 < len(parts):
                                try:
                                    slice_idx = int(parts[i + 1])
                                    slice_info = f"{part.capitalize()} Slice {slice_idx}"
                                    display_name = f"{part.capitalize()} Slice {slice_idx + 1}"
                                    break
                                except ValueError:
                                    continue
                    else:
                        base_name = fname.replace('_mask.png', '')
                        slice_info = f"DICOM: {base_name}"
                        display_name = f"DICOM: {base_name}"

                    all_masks.append({
                        'filename': fname,
                        'display_name': display_name,
                        'slice_info': slice_info,
                        'type': 'sam',
                        'folder': 'masks'
                    })

        # Collect few-shot masks
        if os.path.exists(fs_masks_folder):
            for fname in sorted(os.listdir(fs_masks_folder)):
                if fname.endswith('_mask.png'):
                    display_name = fname
                    slice_info = "Unknown"

                    if state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
                        parts = fname.split('_')
                        for i, part in enumerate(parts):
                            if part in ['axial', 'sagittal', 'coronal'] and i + 1 < len(parts):
                                try:
                                    slice_idx = int(parts[i + 1])
                                    slice_info = f"{part.capitalize()} Slice {slice_idx} (Few-Shot)"
                                    display_name = f"{part.capitalize()} Slice {slice_idx} (Few-Shot)"
                                    break
                                except ValueError:
                                    continue
                    else:
                        base_name = fname.replace('_mask.png', '')
                        slice_info = f"DICOM: {base_name} (Few-Shot)"
                        display_name = f"DICOM: {base_name} (Few-Shot)"

                    all_masks.append({
                        'filename': fname,
                        'display_name': display_name,
                        'slice_info': slice_info,
                        'type': 'few_shot',
                        'folder': 'masks_few_shot'
                    })

        all_masks.sort(key=lambda x: x['display_name'])

        return jsonify({
            'success': True,
            'masks': all_masks,
            'total_count': len(all_masks),
            'sam_count': len([m for m in all_masks if m['type'] == 'sam']),
            'fewshot_count': len([m for m in all_masks if m['type'] == 'few_shot'])
        })
    except Exception as e:
        logger.error(f"Error listing few-shot masks: {e}")
        return jsonify({'success': False, 'error': str(e), 'masks': []}), 500
