# -*- coding: utf-8 -*-
"""
Image processing service for RADSAM.
Contains functions for slice extraction, windowing and image conversion.
"""

import io
import base64
import logging
import numpy as np
from PIL import Image
import pydicom

logger = logging.getLogger(__name__)

# Axis index mapping for slicing
AXIS_INDEX_FOR_SLICING = {'axial': 2, 'sagittal': 0, 'coronal': 1}

# Opacity of the colored mask overlays (0 = invisible, 1 = fully opaque).
# Single place to tune for all overlays.
OVERLAY_ALPHA = 0.4


def blend_mask_overlay(rgb_np, mask_bool, color=(255, 0, 0), alpha=OVERLAY_ALPHA):
    """Blend a color semi-transparently into the mask pixels of an RGB image (instead of replacing them),
    so the underlying (windowed) image stays visible. Returns uint8 RGB with baked-in transparency."""
    out = rgb_np.astype(np.float32).copy()
    m = np.asarray(mask_bool).astype(bool)
    out[m] = (1.0 - alpha) * out[m] + alpha * np.asarray(color, dtype=np.float32)
    return np.clip(out, 0, 255).astype(np.uint8)


def get_slice_from_volume(volume_data, slice_index, axis_name):
    """
    Extracts a 2D slice from a 3D volume.

    Args:
        volume_data: 3D NumPy array
        slice_index: index of the slice to extract
        axis_name: name of the axis ('axial', 'sagittal', 'coronal')

    Returns:
        np.ndarray: 2D slice as a float32 array, or None on error
    """
    axis_idx = AXIS_INDEX_FOR_SLICING.get(axis_name)
    if axis_idx is None or axis_idx == -1 or axis_idx >= volume_data.ndim:
        logger.warning(f"Invalid axis index {axis_idx} for axis {axis_name} with data shape {volume_data.shape}")
        return None

    try:
        if axis_idx == 0:
            slice_2d = volume_data[slice_index, :, :]    # Sagittal
        elif axis_idx == 1:
            slice_2d = volume_data[:, slice_index, :]    # Coronal
        elif axis_idx == 2:
            slice_2d = volume_data[:, :, slice_index]    # Axial
        else:
            return None
        return np.ascontiguousarray(slice_2d.astype(np.float32))

    except IndexError:
        logger.error(f"IndexError: slice {slice_index} on axis {axis_name} (index {axis_idx}) for shape {volume_data.shape}")
        return None
    except Exception as e:
        logger.error(f"Error in get_slice_from_volume: {e}")
        return None


def apply_windowing(pixel_array, target_ww, target_wl, dicom_ds_for_metadata=None):
    """
    Applies windowing to a pixel array.

    Args:
        pixel_array: 2D NumPy array with pixel values
        target_ww: target window width (can be None)
        target_wl: target window level (can be None)
        dicom_ds_for_metadata: DICOM dataset for metadata (optional)

    Returns:
        tuple: (np_rgb, pil_rgb, original_size, used_ww, used_wl)
    """
    rescaled_array = pixel_array.astype(np.float64)
    ww, wl, source = None, None, "N/A"

    # 1. Try user-defined values
    if target_ww is not None and target_wl is not None:
        try:
            _ww = float(target_ww)
            _wl = float(target_wl)
        except (ValueError, TypeError):
            _ww, _wl = None, None
        if _ww is not None and _ww > 0:
            ww, wl, source = _ww, _wl, "User"

    # 2. Try DICOM metadata
    if (ww is None or wl is None) and dicom_ds_for_metadata is not None:
        try:
            wc = dicom_ds_for_metadata.get('WindowCenter')
            wd = dicom_ds_for_metadata.get('WindowWidth')
            if wc is not None and wd is not None:
                _wl_meta = wc[0] if isinstance(wc, pydicom.multival.MultiValue) else wc
                _ww_meta = wd[0] if isinstance(wd, pydicom.multival.MultiValue) else wd
                _wl = float(_wl_meta)
                _ww = float(_ww_meta)
                if _ww > 0:
                    wl, ww, source = _wl, _ww, "Metadata"
        except Exception:
            pass

    # 3. Compute from slice data (percentile-based, similar to 3D Slicer)
    if ww is None or wl is None:
        try:
            finite_vals = rescaled_array[np.isfinite(rescaled_array)]
            if finite_vals.size > 0:
                p1, p99 = np.percentile(finite_vals, [1, 99])
                wl = (p1 + p99) / 2.0
                ww = max(1.0, p99 - p1)
                source = "PercentileCalc"
            else:
                ww, wl, source = 1.0, 0.0, "FallbackNan"
        except Exception:
            ww, wl, source = 1.0, 0.0, "FallbackError"

    actual_ww = float(ww) if isinstance(ww, (int, float)) and ww > 0 else 1.0
    actual_wl = float(wl) if isinstance(wl, (int, float)) else 0.0

    low = actual_wl - actual_ww / 2.0
    high = actual_wl + actual_ww / 2.0

    img_win = np.clip(rescaled_array, low, high)
    img_norm = (img_win - low) / actual_ww if actual_ww > 1e-5 else np.zeros_like(img_win)
    img_8b = (img_norm * 255.0).astype(np.uint8)

    # Take photometric interpretation into account
    photometric_interpretation = getattr(dicom_ds_for_metadata, 'PhotometricInterpretation', "MONOCHROME2") if dicom_ds_for_metadata else "MONOCHROME2"
    if photometric_interpretation == "MONOCHROME1":
        img_8b = 255 - img_8b

    orig_sz = (img_8b.shape[1], img_8b.shape[0])
    pil_g = Image.fromarray(img_8b, 'L')
    pil_rgb = pil_g.convert('RGB')
    np_rgb = np.array(pil_rgb)

    return np_rgb, pil_rgb, orig_sz, actual_ww, actual_wl


def image_to_base64(img_pil):
    """
    Converts a PIL image to a Base64 string.

    Args:
        img_pil: PIL Image object

    Returns:
        str: Base64-encoded string with data URI prefix, or None on error
    """
    try:
        buf = io.BytesIO()
        img_pil.save(buf, format="PNG")
        return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}"
    except Exception as e:
        logger.error(f"Error during Base64 conversion: {e}")
        return None


def save_cutout_image(original_rgb_np: np.ndarray, mask_uint8: np.ndarray, save_path: str) -> None:
    """
    Saves a cutout (black outside the mask), based on an already
    WL/WW-corrected RGB or grayscale slice.

    Args:
        original_rgb_np: HxW (grayscale) or HxWx3 (RGB) uint8 array
        mask_uint8: HxW binary {0,1} array
        save_path: destination PNG file
    """
    try:
        img_np = original_rgb_np
        if img_np.ndim == 2:
            # Grayscale -> black outside
            cut_np = (img_np * mask_uint8.astype(np.uint8)).astype(np.uint8)
            Image.fromarray(cut_np, mode='L').save(save_path)
        else:
            # RGB -> mask channel-wise
            if mask_uint8.ndim == 2:
                mask3 = np.repeat(mask_uint8[:, :, None], 3, axis=2).astype(np.uint8)
            else:
                mask3 = mask_uint8.astype(np.uint8)
            cut_np = (img_np * mask3).astype(np.uint8)
            Image.fromarray(cut_np, mode='RGB').save(save_path)
    except Exception as e:
        logger.warning(f"Could not save cutout ({save_path}): {e}")
