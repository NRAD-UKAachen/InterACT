# -*- coding: utf-8 -*-
"""
Few-shot segmentation routes for RADSAM.
Contains routes for few-shot interpolation and S1SFQ segmentation.
"""

import os
import re
import json
import logging
import traceback
import numpy as np
import torch
import pydicom
from PIL import Image
from flask import request, jsonify

from . import fewshot_bp
from services.sam_service import get_predictor, is_sam_available, get_device
from services.image_processing import get_slice_from_volume, apply_windowing, save_cutout_image, blend_mask_overlay
from services.archive_service import create_archive_folder
from services.mask_processing import apply_threshold_refinement_to_mask
from utils.temp_slices import create_temp_slices_folder_for_axis, create_temp_slices_folder_dicom, cleanup_temp_slices_folder
import state

logger = logging.getLogger(__name__)


def _resolve_slice_range(data, total_slices):
    """
    Reads `slice_start` / `slice_end` from the request data, clamps to
    [0, total-1] and swaps them if necessary. Returns (lo, hi) inclusive,
    defaults = (0, total-1).
    """
    s = data.get('slice_start')
    e = data.get('slice_end')
    lo = int(s) if s is not None else 0
    hi = int(e) if e is not None else (total_slices - 1)
    if lo > hi:
        lo, hi = hi, lo
    lo = max(0, min(lo, total_slices - 1))
    hi = max(0, min(hi, total_slices - 1))
    return lo, hi


def _filter_masks_by_range(masks_dict, lo, hi):
    """Removes all slice indices outside [lo, hi] from the mask dict (in-place)."""
    out_of_range = [k for k in masks_dict.keys() if k < lo or k > hi]
    for k in out_of_range:
        del masks_dict[k]
    return len(out_of_range)


@fewshot_bp.route('/upload_support_mask', methods=['POST'])
def upload_support_mask():
    """
    Accepts an uploaded mask (PNG), validates it and saves it with the
    correct filename in the masks folder of the current session.
    """
    if 'mask_file' not in request.files:
        return jsonify({'error': 'No file found in the request'}), 400

    mask_file = request.files['mask_file']
    slice_index = request.form.get('slice_index', type=int)
    axis = request.form.get('axis', 'axial')

    if mask_file.filename == '' or slice_index is None:
        return jsonify({'error': 'Missing file or slice index'}), 400

    if not mask_file.filename.lower().endswith('.png'):
        return jsonify({'error': 'Invalid file format. Please upload a PNG file'}), 400

    try:
        # Determine target dimensions
        target_shape = None
        if state.CURRENT_FILE_TYPE == 'dicom_series':
            if 0 <= slice_index < len(state.DICOM_IMAGE_FILES):
                ds = pydicom.dcmread(state.DICOM_IMAGE_FILES[slice_index])
                target_shape = (ds.Columns, ds.Rows)
        elif state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
            slice_data = get_slice_from_volume(state.CURRENT_VOLUME_DATA, slice_index, axis)
            if slice_data is not None:
                target_shape = (slice_data.shape[1], slice_data.shape[0])

        if target_shape is None:
            return jsonify({'error': f'Could not determine target dimensions for slice {slice_index}'}), 500

        # Load and validate mask
        mask_image = Image.open(mask_file.stream)
        if mask_image.size != target_shape:
            return jsonify({
                'error': f'Dimension error: mask ({mask_image.size[0]}x{mask_image.size[1]}) does not match the slice size ({target_shape[0]}x{target_shape[1]})'
            }), 400

        # Generate filename
        if state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
            mask_filename_base = f"{state.CURRENT_ORIGINAL_FILENAME}_{axis}_{slice_index:03d}"
        else:
            dicom_basename = os.path.splitext(os.path.basename(state.DICOM_IMAGE_FILES[slice_index]))[0]
            mask_filename_base = dicom_basename

        mask_filename = f"{mask_filename_base}_mask.png"

        # Save
        archive_folder = create_archive_folder()
        masks_folder = os.path.join(archive_folder, "segmentation_results", "masks")
        os.makedirs(masks_folder, exist_ok=True)
        save_path = os.path.join(masks_folder, mask_filename)

        mask_image.convert('L').save(save_path, "PNG")

        logger.info(f"Support mask saved: {save_path}")
        return jsonify({
            'success': True,
            'message': f'Mask for slice {slice_index + 1} uploaded successfully'
        })

    except Exception as e:
        logger.error(f"Error processing the support mask: {e}")
        return jsonify({'error': str(e)}), 500


@fewshot_bp.route('/few_shot_segment_interpolation', methods=['POST'])
def few_shot_segment_interpolation_route():
    """
    Runs few-shot segmentation with interpolation.
    """
    if not is_sam_available():
        return jsonify({'success': False, 'error': 'SAM2 is not available'}), 503

    slices_dir = None
    try:
        data = request.json
        support_mask_files = data.get('support_masks', [])
        axis = data.get('axis', 'axial')
        current_ww = data.get('ww')
        current_wl = data.get('wl')
        apply_threshold_refinement = data.get('apply_threshold_refinement', False)
        threshold_lower = data.get('threshold_lower')
        threshold_upper = data.get('threshold_upper')

        if not support_mask_files:
            return jsonify({'success': False, 'error': 'Support masks required'}), 400

        # Preparation
        archive_folder = create_archive_folder()
        manual_masks_folder = os.path.join(archive_folder, "segmentation_results", "masks")

        if state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
            total_slices = state.SLICE_COUNTS.get(axis, 0)
        else:
            total_slices = len(state.DICOM_IMAGE_FILES)

        lo, hi = _resolve_slice_range(data, total_slices)

        # Extract support indices
        support_indices_map = _extract_support_indices(support_mask_files, axis)
        if not support_indices_map:
            return jsonify({'success': False, 'error': 'No valid support masks found'}), 400

        support_indices = sorted(support_indices_map.keys())
        out_of_range_support = [s for s in support_indices if s < lo or s > hi]
        warning = None
        if out_of_range_support:
            warning = f"Support slice(s) {out_of_range_support} are outside the range [{lo}, {hi}] and will be set to zero"
            logger.warning(warning)

        final_masks = {}

        # Create temporary slices folder
        logger.info("Creating temporary slices folder for interpolation...")
        if state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
            slices_dir = create_temp_slices_folder_for_axis(state.CURRENT_VOLUME_DATA, axis, ww=current_ww, wl=current_wl)
        else:
            slices_dir = create_temp_slices_folder_dicom(state.DICOM_IMAGE_FILES, ww=current_ww, wl=current_wl)

        predictor = get_predictor()
        device = get_device()

        # Perform propagation
        def propagate_segment_sequentially_get_logits(start_idx, end_idx, direction):
            segment_logits = {}
            inference_state = predictor.init_state(video_path=slices_dir)

            start_mask_file = support_indices_map[start_idx]
            mask_np = (np.array(Image.open(os.path.join(manual_masks_folder, start_mask_file)).convert('L')) > 128).astype(np.uint8)
            predictor.add_new_mask(inference_state=inference_state, frame_idx=start_idx, obj_id="segment_1",
                                   mask=torch.from_numpy(mask_np).to(device))

            with torch.cuda.amp.autocast(dtype=torch.bfloat16):
                prop_range = range(start_idx + 1, end_idx + 1) if direction == 'forward' else range(start_idx - 1, end_idx - 1, -1)

                for current_slice_idx in prop_range:
                    for _, _, masks_batch in predictor.propagate_in_video(inference_state, start_frame_idx=current_slice_idx, max_frame_num_to_track=1):
                        logit_tensor = masks_batch[0].cpu().numpy()[0]
                        segment_logits[current_slice_idx] = logit_tensor

                        binary_mask = (logit_tensor > 0.5).astype(np.uint8)
                        new_mask_tensor = torch.from_numpy(binary_mask).to(device=device, dtype=torch.float32)
                        predictor.add_new_mask(inference_state=inference_state, frame_idx=current_slice_idx,
                                               obj_id="segment_1", mask=new_mask_tensor)
                        break
            return segment_logits

        # Extrapolation start
        if support_indices[0] > 0:
            logger.info(f"Extrapolating backward from slice {support_indices[0]} to 0...")
            backward_logits = propagate_segment_sequentially_get_logits(support_indices[0], 0, 'backward')
            for idx, logit in backward_logits.items():
                final_masks[idx] = (logit > 0.5).astype(np.uint8)

        # Interpolation between support slices
        if len(support_indices) > 1:
            for i in range(len(support_indices) - 1):
                s_prev, s_curr = support_indices[i], support_indices[i + 1]
                if s_prev + 1 >= s_curr:
                    continue

                logger.info(f"Interpolating between slice {s_prev} and {s_curr}...")
                forward_logits = propagate_segment_sequentially_get_logits(s_prev, s_curr - 1, 'forward')
                backward_logits = propagate_segment_sequentially_get_logits(s_curr, s_prev + 1, 'backward')

                total_distance = s_curr - s_prev

                for idx in range(s_prev + 1, s_curr):
                    if idx in forward_logits and idx in backward_logits:
                        dist_from_start = idx - s_prev
                        weight_fwd = (s_curr - idx) / total_distance
                        weight_bwd = dist_from_start / total_distance
                        interpolated_logit = (forward_logits[idx] * weight_fwd) + (backward_logits[idx] * weight_bwd)
                        final_masks[idx] = (interpolated_logit > 0.5).astype(np.uint8)
                    elif idx in forward_logits:
                        final_masks[idx] = (forward_logits[idx] > 0.5).astype(np.uint8)
                    elif idx in backward_logits:
                        final_masks[idx] = (backward_logits[idx] > 0.5).astype(np.uint8)

        # Extrapolation end
        if support_indices[-1] < total_slices - 1:
            logger.info(f"Extrapolating forward from slice {support_indices[-1]} to {total_slices - 1}...")
            forward_logits = propagate_segment_sequentially_get_logits(support_indices[-1], total_slices - 1, 'forward')
            for idx, logit in forward_logits.items():
                final_masks[idx] = (logit > 0.5).astype(np.uint8)

        # Add support masks
        for s_idx, mask_file in support_indices_map.items():
            final_masks[s_idx] = (np.array(Image.open(os.path.join(manual_masks_folder, mask_file)).convert('L')) > 128).astype(np.uint8)

        # Apply slice range: indices outside are not saved -> NIfTI gets zero there
        dropped = _filter_masks_by_range(final_masks, lo, hi)
        if dropped:
            logger.info(f"Slice range [{lo}, {hi}]: {dropped} masks outside discarded")

        # Save results
        _save_fewshot_results(final_masks, axis, current_ww, current_wl, apply_threshold_refinement,
                              threshold_lower, threshold_upper, 'few_shot')

        message = f"Interpolation completed. {len(final_masks)} of {total_slices} masks processed."
        if warning:
            message += f" | Warning: {warning}"
        return jsonify({'success': True, 'message': message, 'warning': warning})

    except Exception as e:
        logger.error(f"Error during interpolation: {e}")
        logger.debug(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cleanup_temp_slices_folder(slices_dir)


@fewshot_bp.route('/s1sfq_segment', methods=['POST'])
@fewshot_bp.route('/few_shot_segment', methods=['POST'])
def s1sfq_segment_route():
    """
    Runs few-shot segmentation with the S1SFQ method (from the center outward).
    """
    if not is_sam_available():
        return jsonify({'success': False, 'error': 'SAM2 is not available'}), 503

    slices_dir = None
    try:
        data = request.json
        support_mask_files = data.get('support_masks', [])
        axis = data.get('axis', 'axial')
        current_ww = data.get('ww')
        current_wl = data.get('wl')
        apply_threshold_refinement = data.get('apply_threshold_refinement', False)
        threshold_lower = data.get('threshold_lower')
        threshold_upper = data.get('threshold_upper')

        if not support_mask_files or len(support_mask_files) != 1:
            return jsonify({'success': False, 'error': 'Exactly ONE support mask must be selected for S1SFQ'}), 400

        # Preparation
        archive_folder = create_archive_folder()
        manual_masks_folder = os.path.join(archive_folder, "segmentation_results", "masks")

        if state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
            total_slices = state.SLICE_COUNTS.get(axis, 0)
        else:
            total_slices = len(state.DICOM_IMAGE_FILES)

        lo, hi = _resolve_slice_range(data, total_slices)

        # Extract support index
        support_indices = _extract_support_indices_list(support_mask_files, axis)
        if not support_indices:
            return jsonify({'success': False, 'error': 'No valid support masks found'}), 400

        out_of_range_support = [s for s in support_indices if s < lo or s > hi]
        warning = None
        if out_of_range_support:
            warning = f"Support slice(s) {out_of_range_support} are outside the range [{lo}, {hi}] and will be set to zero"
            logger.warning(warning)

        all_slice_indices = list(range(total_slices))
        query_indices = sorted(list(set(all_slice_indices) - set(support_indices)))
        s_idx = support_indices[0]
        first_half_queries = sorted([i for i in query_indices if i < s_idx], reverse=True)
        second_half_queries = sorted([i for i in query_indices if i > s_idx])

        generated_masks = {}

        # Create temporary slices folder
        if state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
            slices_dir = create_temp_slices_folder_for_axis(state.CURRENT_VOLUME_DATA, axis, ww=current_ww, wl=current_wl)
        else:
            slices_dir = create_temp_slices_folder_dicom(state.DICOM_IMAGE_FILES, ww=current_ww, wl=current_wl)

        predictor = get_predictor()
        device = get_device()

        # Load support mask
        mask_path = os.path.join(manual_masks_folder, support_mask_files[0])
        mask_np = (np.array(Image.open(mask_path).convert('L')) > 128).astype(np.uint8)
        mask_tensor = torch.from_numpy(mask_np).to(device=device, dtype=torch.float32)

        # Process first half
        logger.info(f"S1SFQ: Processing first half ({len(first_half_queries)} slices)...")
        with torch.cuda.amp.autocast(dtype=torch.bfloat16):
            inference_state_first = predictor.init_state(video_path=slices_dir)
            predictor.add_new_mask(inference_state=inference_state_first, frame_idx=s_idx, obj_id="segment_1", mask=mask_tensor)

            for q_idx in first_half_queries:
                for _, _, masks_batch in predictor.propagate_in_video(inference_state_first, start_frame_idx=q_idx, max_frame_num_to_track=1):
                    mask_logit = masks_batch[0].cpu().numpy()[0]
                    binary_mask = (mask_logit > 0.5).astype(np.uint8)
                    generated_masks[q_idx] = binary_mask
                    new_mask_tensor = torch.from_numpy(binary_mask).to(device=device, dtype=torch.float32)
                    predictor.add_new_mask(inference_state=inference_state_first, frame_idx=q_idx, obj_id="segment_1", mask=new_mask_tensor)

        # Process second half
        logger.info(f"S1SFQ: Processing second half ({len(second_half_queries)} slices)...")
        with torch.cuda.amp.autocast(dtype=torch.bfloat16):
            inference_state_second = predictor.init_state(video_path=slices_dir)
            predictor.add_new_mask(inference_state=inference_state_second, frame_idx=s_idx, obj_id="segment_1", mask=mask_tensor)

            for q_idx in second_half_queries:
                for _, _, masks_batch in predictor.propagate_in_video(inference_state_second, start_frame_idx=q_idx, max_frame_num_to_track=1):
                    mask_logit = masks_batch[0].cpu().numpy()[0]
                    binary_mask = (mask_logit > 0.5).astype(np.uint8)
                    generated_masks[q_idx] = binary_mask
                    new_mask_tensor = torch.from_numpy(binary_mask).to(device=device, dtype=torch.float32)
                    predictor.add_new_mask(inference_state=inference_state_second, frame_idx=q_idx, obj_id="segment_1", mask=new_mask_tensor)

        # Add support mask
        generated_masks[s_idx] = mask_np

        # Apply slice range
        dropped = _filter_masks_by_range(generated_masks, lo, hi)
        if dropped:
            logger.info(f"Slice range [{lo}, {hi}]: {dropped} masks outside discarded")

        # Save results
        _save_fewshot_results(generated_masks, axis, current_ww, current_wl, apply_threshold_refinement,
                              threshold_lower, threshold_upper, 's1sfq')

        message = f"S1SFQ segmentation completed. {len(generated_masks)} masks generated."
        if warning:
            message += f" | Warning: {warning}"
        return jsonify({'success': True, 'message': message, 'warning': warning})

    except Exception as e:
        logger.error(f"Error during S1SFQ segmentation: {e}")
        logger.debug(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cleanup_temp_slices_folder(slices_dir)


def _extract_support_indices(support_mask_files, axis):
    """Extracts support indices from mask filenames."""
    support_indices_map = {}

    for mask_filename in support_mask_files:
        slice_idx = -1
        if state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
            parts = os.path.splitext(mask_filename)[0].replace('_mask', '').split('_')
            if len(parts) > 1 and parts[-2] == axis and parts[-1].isdigit():
                slice_idx = int(parts[-1])
        else:
            base_name = os.path.splitext(mask_filename)[0].replace('_mask', '')
            for i, dicom_path in enumerate(state.DICOM_IMAGE_FILES):
                if os.path.splitext(os.path.basename(dicom_path))[0] == base_name:
                    slice_idx = i
                    break

        if slice_idx != -1:
            support_indices_map[slice_idx] = mask_filename

    return support_indices_map


def _extract_support_indices_list(support_mask_files, axis):
    """Extracts support indices as a list."""
    indices = []
    for mask_filename in support_mask_files:
        slice_idx = -1
        if state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
            parts = mask_filename.split('_')
            for i, part in enumerate(parts):
                if part == axis and i + 1 < len(parts):
                    try:
                        slice_idx = int(parts[i + 1])
                        break
                    except ValueError:
                        continue
        else:
            base_name = os.path.splitext(mask_filename)[0].replace('_mask', '')
            match = re.search(r"['\"](.*?)['\"]", base_name)
            if match:
                base_name = match.group(1)
            for i, dicom_path in enumerate(state.DICOM_IMAGE_FILES):
                if os.path.splitext(os.path.basename(dicom_path))[0] == base_name:
                    slice_idx = i
                    break

        if slice_idx != -1:
            indices.append(slice_idx)

    return sorted(list(set(indices)))


def _save_fewshot_results(masks_dict, axis, ww, wl, apply_threshold, thresh_lower, thresh_upper, method_name):
    """Saves few-shot results."""
    archive_folder = create_archive_folder()
    results_folder = os.path.join(archive_folder, "segmentation_results")

    if method_name == 's1sfq':
        masks_folder = os.path.join(results_folder, "masks_s1sfq")
        overlays_folder = os.path.join(results_folder, "overlays_s1sfq")
        cutouts_folder = os.path.join(results_folder, "cutouts_s1sfq")
    elif method_name == 'memory':
        masks_folder = os.path.join(results_folder, "masks_memory")
        overlays_folder = os.path.join(results_folder, "overlays_memory")
        cutouts_folder = os.path.join(results_folder, "cutouts_memory")
    else:
        masks_folder = os.path.join(results_folder, "masks_few_shot")
        overlays_folder = os.path.join(results_folder, "overlays_few_shot")
        cutouts_folder = os.path.join(results_folder, "cutouts_few_shot")

    os.makedirs(masks_folder, exist_ok=True)
    os.makedirs(overlays_folder, exist_ok=True)
    os.makedirs(cutouts_folder, exist_ok=True)

    # Save windowing parameters
    with open(os.path.join(masks_folder, 'windowing.json'), 'w') as f:
        json.dump({'ww': ww, 'wl': wl}, f)

    threshold_active = apply_threshold and thresh_lower is not None and thresh_upper is not None
    overlay_color = (128, 0, 255) if threshold_active else (255, 0, 0)

    for slice_idx, mask_data in sorted(masks_dict.items()):
        mask_to_save = mask_data

        # Apply threshold refinement
        if threshold_active:
            refined_mask = apply_threshold_refinement_to_mask(mask_data, slice_idx, axis, thresh_lower, thresh_upper)
            if refined_mask is not None:
                mask_to_save = refined_mask

        # Filenames
        if state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
            base_name = f"{state.CURRENT_ORIGINAL_FILENAME}_{axis}_{slice_idx:03d}"
        else:
            base_name = os.path.splitext(os.path.basename(state.DICOM_IMAGE_FILES[slice_idx]))[0]

        # Save mask
        mask_filename = f"{base_name}_mask.png"
        Image.fromarray(mask_to_save * 255, mode='L').save(os.path.join(masks_folder, mask_filename))

        # Create overlay
        try:
            if state.CURRENT_FILE_TYPE != 'dicom_series':
                raw_slice = get_slice_from_volume(state.CURRENT_VOLUME_DATA, slice_idx, axis)
                ds_meta = None
            else:
                ds_meta = pydicom.dcmread(state.DICOM_IMAGE_FILES[slice_idx])
                raw_slice = ds_meta.pixel_array.astype(np.float32)
                if hasattr(ds_meta, 'RescaleSlope'):
                    raw_slice = raw_slice * float(ds_meta.RescaleSlope) + float(ds_meta.RescaleIntercept)

            slice_rgb, _, _, _, _ = apply_windowing(raw_slice, ww, wl, ds_meta)
            overlay_img = Image.fromarray(slice_rgb)
            mask_pil = Image.fromarray(mask_to_save * 255, 'L').resize(overlay_img.size, Image.Resampling.NEAREST)

            overlay_rgb = blend_mask_overlay(np.array(overlay_img), np.array(mask_pil) > 0, color=overlay_color)

            overlay_filename = f"{base_name}_overlay.png"
            Image.fromarray(overlay_rgb, 'RGB').save(os.path.join(overlays_folder, overlay_filename))

            # Save cutout
            cutout_filename = f"{base_name}_cutout.png"
            save_cutout_image(slice_rgb, (np.array(mask_pil) > 0).astype(np.uint8), os.path.join(cutouts_folder, cutout_filename))

        except Exception as e:
            logger.warning(f"Could not create overlay for slice {slice_idx}: {e}")


@fewshot_bp.route('/few_shot_segment_memory', methods=['POST'])
def few_shot_segment_memory_route():
    """
    Runs few-shot segmentation using SAM2's natural memory-attention mechanism.
    Uses ALL support masks in a shared inference_state and lets SAM2 itself
    decide via cross-attention how the information is combined.

    Advantages over interpolation:
    - Uses SAM2's visually based attention instead of linear weighting
    - More robust with gaps (object disappears and reappears)
    - Simpler and closer to the original SAM2 design
    """
    if not is_sam_available():
        return jsonify({'success': False, 'error': 'SAM2 is not available'}), 503

    slices_dir = None
    try:
        data = request.json
        support_mask_files = data.get('support_masks', [])
        axis = data.get('axis', 'axial')
        current_ww = data.get('ww')
        current_wl = data.get('wl')
        apply_threshold_refinement = data.get('apply_threshold_refinement', False)
        threshold_lower = data.get('threshold_lower')
        threshold_upper = data.get('threshold_upper')

        # Optional parameters for memory control
        confidence_threshold = data.get('confidence_threshold', 0.0)
        reinject_high_confidence = data.get('reinject_high_confidence', False)
        min_confidence_for_reinject = data.get('min_confidence_for_reinject', 0.75)

        if not support_mask_files or len(support_mask_files) < 1:
            return jsonify({'success': False, 'error': 'At least one support mask required'}), 400

        logger.info(f"[MEMORY] Starting memory-based few-shot segmentation with {len(support_mask_files)} support masks")
        logger.info(f"[MEMORY] Confidence threshold: {confidence_threshold}, re-inject: {reinject_high_confidence}")

        # Preparation
        archive_folder = create_archive_folder()
        manual_masks_folder = os.path.join(archive_folder, "segmentation_results", "masks")

        if state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
            total_slices = state.SLICE_COUNTS.get(axis, 0)
        else:
            total_slices = len(state.DICOM_IMAGE_FILES)

        lo, hi = _resolve_slice_range(data, total_slices)

        # Extract support indices
        support_indices_map = _extract_support_indices(support_mask_files, axis)
        if not support_indices_map:
            return jsonify({'success': False, 'error': 'No valid support masks found'}), 400

        support_indices = sorted(support_indices_map.keys())
        logger.info(f"[MEMORY] Support slices: {support_indices}")

        out_of_range_support = [s for s in support_indices if s < lo or s > hi]
        warning = None
        if out_of_range_support:
            warning = f"Support slice(s) {out_of_range_support} are outside the range [{lo}, {hi}] and will be set to zero"
            logger.warning(warning)

        # Create temporary slices folder
        if state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
            slices_dir = create_temp_slices_folder_for_axis(state.CURRENT_VOLUME_DATA, axis, ww=current_ww, wl=current_wl)
        else:
            slices_dir = create_temp_slices_folder_dicom(state.DICOM_IMAGE_FILES, ww=current_ww, wl=current_wl)

        predictor = get_predictor()
        device = get_device()
        final_masks = {}

        with torch.cuda.amp.autocast(dtype=torch.bfloat16), torch.inference_mode():
            # Initialize ONE shared inference_state
            inference_state = predictor.init_state(video_path=slices_dir)

            # Add ALL support masks to the inference_state
            logger.info(f"[MEMORY] Adding {len(support_indices)} support masks to the shared memory...")
            for support_idx, mask_file in support_indices_map.items():
                mask_path = os.path.join(manual_masks_folder, mask_file)
                mask_np = (np.array(Image.open(mask_path).convert('L')) > 128).astype(np.uint8)
                mask_tensor = torch.from_numpy(mask_np).to(device=device, dtype=torch.float32)

                predictor.add_new_mask(
                    inference_state=inference_state,
                    frame_idx=support_idx,
                    obj_id="segment_1",
                    mask=mask_tensor
                )

                # Save support masks directly
                final_masks[support_idx] = mask_np
                logger.info(f"[MEMORY]   ✓ Support mask added at slice {support_idx}")

            # Determine query slices (all except support slices)
            all_query_indices = [i for i in range(total_slices) if i not in support_indices_map]

            # Sort query slices: start in the middle and work outward
            middle_idx = total_slices // 2
            all_query_indices.sort(key=lambda x: abs(x - middle_idx))

            logger.info(f"[MEMORY] Starting segmentation of {len(all_query_indices)} slices with memory attention...")

            # Propagate all query slices
            for idx, query_idx in enumerate(all_query_indices):
                # SAM2 automatically uses cross-attention over ALL frames in the inference_state
                for _, _, masks_batch in predictor.propagate_in_video(
                    inference_state,
                    start_frame_idx=query_idx,
                    max_frame_num_to_track=1
                ):
                    mask_logit = masks_batch[0].cpu().numpy()[0]

                    # Compute the average confidence of the mask
                    binary_mask_preview = (mask_logit > 0.5).astype(np.uint8)
                    if binary_mask_preview.sum() > 0:
                        avg_confidence = mask_logit[binary_mask_preview > 0].mean()
                    else:
                        avg_confidence = 0.0

                    # Apply confidence-based threshold
                    binary_mask = (mask_logit > confidence_threshold).astype(np.uint8)
                    final_masks[query_idx] = binary_mask

                    # Optional: re-inject high-confidence masks as new memory anchors
                    if reinject_high_confidence and avg_confidence > min_confidence_for_reinject:
                        new_mask_tensor = torch.from_numpy(binary_mask).to(device=device, dtype=torch.float32)
                        predictor.add_new_mask(
                            inference_state=inference_state,
                            frame_idx=query_idx,
                            obj_id="segment_1",
                            mask=new_mask_tensor
                        )

                    break  # process only one frame

        logger.info(f"[MEMORY] ✓ Segmentation completed: {len(final_masks)} masks generated")

        # Apply slice range
        dropped = _filter_masks_by_range(final_masks, lo, hi)
        if dropped:
            logger.info(f"Slice range [{lo}, {hi}]: {dropped} masks outside discarded")

        # Save results
        _save_fewshot_results(final_masks, axis, current_ww, current_wl, apply_threshold_refinement,
                              threshold_lower, threshold_upper, 'memory')

        message = f'Memory-based few-shot segmentation completed. {len(final_masks)} masks generated.'
        if warning:
            message += f" | Warning: {warning}"

        return jsonify({
            'success': True,
            'message': message,
            'warning': warning,
            'num_masks': len(final_masks),
            'support_slices': support_indices,
            'method': 'few_shot_memory_attention'
        })

    except Exception as e:
        logger.error(f"Error during memory-based segmentation: {e}")
        logger.debug(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        cleanup_temp_slices_folder(slices_dir)
