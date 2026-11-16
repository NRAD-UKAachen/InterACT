# -*- coding: utf-8 -*-
"""
SAM2 service for RADSAM.
Contains functions for SAM2 model initialization and management.
"""

import logging
import traceback
import torch
import numpy as np

from config import Config

logger = logging.getLogger(__name__)

# SAM2 imports (with fallback)
try:
    from sam2.build_sam import build_sam2_video_predictor
    from sam2.sam2_video_predictor import SAM2VideoPredictor
    SAM2_AVAILABLE = True
except ImportError:
    logger.warning("sam2 module not found. Using fallback.")
    SAM2_AVAILABLE = False

    def build_sam2_video_predictor(cfg, chkpt, dev):
        """Dummy function for SAM2 video build when SAM2 is not available."""
        logger.warning("SAM2 not available - using dummy model")
        return None

    class SAM2VideoPredictor:
        """Dummy class for SAM2 video predictor when SAM2 is not available."""

        def __init__(self, model):
            logger.warning("SAM2 dummy video predictor initialized - no segmentation possible")
            self.model = model

        def init_state(self, video_path, **kwargs):
            """Dummy method for init_state."""
            logger.debug("SAM2 dummy init_state called")
            return {"images": [], "num_frames": 0}

        def add_new_points_or_box(self, inference_state, frame_idx, obj_id, **kwargs):
            """Dummy method for add_new_points_or_box."""
            logger.debug("SAM2 dummy add_new_points_or_box called")

        def propagate_in_video(self, inference_state, **kwargs):
            """Dummy method for propagate_in_video, returns empty results."""
            logger.warning("SAM2 dummy propagate_in_video called - no segmentation performed")
            yield 0, [], np.array([])

        def add_new_mask(self, inference_state, frame_idx, obj_id, **kwargs):
            """Dummy method for add_new_mask."""
            logger.debug("SAM2 dummy add_new_mask called")


# Global variables for SAM2
_predictor = None
_device = None


def initialize_sam2():
    """
    Initializes the SAM2 model.

    Returns:
        tuple: (predictor, device) or (None, None) on error
    """
    global _predictor, _device

    logger.info("Initializing SAM2 model...")
    _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    if SAM2_AVAILABLE:
        try:
            _predictor = build_sam2_video_predictor(
                Config.SAM2_CONFIG['model_cfg'],
                Config.SAM2_CONFIG['checkpoint'],
                device=_device
            )
            logger.info(f"SAM2 VideoPredictor successfully loaded on {_device}.")
        except Exception as e:
            logger.error(f"ERROR loading the SAM2 VideoPredictor: {e}\n{traceback.format_exc()}")
            _predictor = SAM2VideoPredictor(None)
            logger.warning("Falling back to dummy SAM2 video predictor.")
    else:
        _predictor = SAM2VideoPredictor(None)

    return _predictor, _device


def get_predictor():
    """
    Returns the SAM2 predictor. Initializes it if this hasn't happened yet.

    Returns:
        SAM2VideoPredictor: The SAM2 predictor
    """
    global _predictor
    if _predictor is None:
        initialize_sam2()
    return _predictor


def get_device():
    """
    Returns the PyTorch device.

    Returns:
        torch.device: The device (cuda or cpu)
    """
    global _device
    if _device is None:
        _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return _device


def is_sam_available():
    """
    Checks whether SAM2 is available.

    Returns:
        bool: True if SAM2 is available
    """
    return SAM2_AVAILABLE and get_predictor() is not None


def _extract_support_slice_indices(support_mask_files, axis):
    """
    Derives the corresponding slice indices from support mask filenames.
    Supports NRRD/NIfTI names (<base>_<axis>_<idx>_mask.png) and DICOM names
    (<dicom_basename>_mask.png, mapped via state.DICOM_IMAGE_FILES).
    """
    import os
    import state

    indices = set()
    for mask_filename in support_mask_files or []:
        base = os.path.splitext(mask_filename)[0]
        if base.endswith('_mask'):
            base = base[:-5]

        if state.CURRENT_FILE_TYPE in ('nrrd', 'nifti'):
            parts = base.split('_')
            # Attempt 1: direct (_<axis>_<idx> at the end)
            if len(parts) >= 2 and parts[-2] == axis and parts[-1].isdigit():
                indices.add(int(parts[-1]))
                continue
            # Fallback: find '<axis>' anywhere + a following number
            for i, p in enumerate(parts):
                if p == axis and i + 1 < len(parts) and parts[i + 1].isdigit():
                    indices.add(int(parts[i + 1]))
                    break
        else:
            # DICOM: match basename against state.DICOM_IMAGE_FILES
            for i, dicom_path in enumerate(state.DICOM_IMAGE_FILES):
                if os.path.splitext(os.path.basename(dicom_path))[0] == base:
                    indices.add(i)
                    break

    return indices


def collect_sam_annotations_from_request(request_data):
    """
    Collects all SAM annotations from the request data.

    Args:
        request_data: JSON data from the client with all annotations

    Returns:
        dict: Structured annotation data, or None on error
    """
    try:
        all_annotations = request_data.get('all_annotations', {})
        segmentation_axis = request_data.get('axis', 'axial')
        support_mask_files = request_data.get('support_mask_files') or []

        # If a few-shot/S1SFQ/memory run took place before: only include the
        # support slices actually used (derived from the mask filenames).
        support_slice_indices = _extract_support_slice_indices(support_mask_files, segmentation_axis)
        if support_mask_files:
            logger.info(f"Filtering annotations to {len(support_slice_indices)} support slice(s): {sorted(support_slice_indices)}")

        # Structure the annotations
        structured_annotations = {
            'segmentation_axis': segmentation_axis,
            'annotations_by_slice': {},
            'total_annotated_slices': 0,
            'annotation_summary': {
                'points_positive': 0,
                'points_negative': 0,
                'boxes': 0
            }
        }
        if support_mask_files:
            structured_annotations['support_slice_indices'] = sorted(support_slice_indices)

        # Process annotations for the segmentation axis
        if segmentation_axis in all_annotations:
            axis_annotations = all_annotations[segmentation_axis]

            for slice_idx_str, slice_annotations in axis_annotations.items():
                try:
                    slice_idx = int(slice_idx_str)
                    # If support filtering is set, skip non-support slices
                    if support_mask_files and slice_idx not in support_slice_indices:
                        continue
                    structured_annotations['annotations_by_slice'][slice_idx] = []

                    for annotation in slice_annotations:
                        if annotation.get('type') == 'positive':
                            structured_annotations['annotation_summary']['points_positive'] += 1
                        elif annotation.get('type') == 'negative':
                            structured_annotations['annotation_summary']['points_negative'] += 1
                        elif annotation.get('type') == 'box':
                            structured_annotations['annotation_summary']['boxes'] += 1

                        structured_annotations['annotations_by_slice'][slice_idx].append({
                            'type': annotation.get('type'),
                            'coordinates': annotation.get('data') or annotation.get('coordinates'),
                            'slice_index': slice_idx
                        })

                    if slice_annotations:
                        structured_annotations['total_annotated_slices'] += 1

                except (ValueError, TypeError) as e:
                    logger.warning(f"Error processing slice annotations for index {slice_idx_str}: {e}")
                    continue

        logger.info(f"SAM annotations collected: {structured_annotations['total_annotated_slices']} slices, "
                    f"{structured_annotations['annotation_summary']['points_positive']} positive points, "
                    f"{structured_annotations['annotation_summary']['points_negative']} negative points, "
                    f"{structured_annotations['annotation_summary']['boxes']} boxes")

        return structured_annotations

    except Exception as e:
        logger.error(f"Error collecting SAM annotations: {e}")
        logger.debug(traceback.format_exc())
        return None
