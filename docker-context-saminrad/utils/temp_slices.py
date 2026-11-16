# -*- coding: utf-8 -*-
"""
Temporary slice folder management for RADSAM.
Contains functions for creating and cleaning up temporary slice folders for SAM2.
"""

import os
import shutil
import tempfile
import logging
import numpy as np
from PIL import Image
import pydicom

from config import Config

logger = logging.getLogger(__name__)


def create_temp_slices_folder(volume_data, output_dir=None):
    """
    Creates a temporary folder with slice images from a 3D volume.
    Uses the axial axis by default.

    Args:
        volume_data: 3D NumPy array with the volume data
        output_dir: Optional output folder (default: TEMP_UPLOAD_FOLDER)

    Returns:
        str: Path to the created temporary folder
    """
    return create_temp_slices_folder_for_axis(volume_data, 'axial', output_dir=output_dir)


def create_temp_slices_folder_for_axis(volume_data, axis_name, ww=None, wl=None, output_dir=None):
    """
    Creates a temporary folder with slice images for a given axis.

    Args:
        volume_data: 3D NumPy array with the volume data
        axis_name: Name of the axis ('axial', 'sagittal', 'coronal')
        ww: Window width for windowing (optional)
        wl: Window level for windowing (optional)
        output_dir: Optional output folder

    Returns:
        str: Path to the created temporary folder
    """
    # Import here to avoid circular imports
    from services.image_processing import apply_windowing, get_slice_from_volume

    if output_dir is None:
        output_dir = Config.TEMP_UPLOAD_FOLDER

    # Create temporary folder
    slices_dir = tempfile.mkdtemp(dir=output_dir, prefix=f"slices_{axis_name}_")

    try:
        # Determine axis index
        axis_map = {'axial': 2, 'sagittal': 0, 'coronal': 1}
        axis_idx = axis_map.get(axis_name, 2)
        num_slices = volume_data.shape[axis_idx]

        for i in range(num_slices):
            try:
                # Extract slice
                slice_2d = get_slice_from_volume(volume_data, i, axis_name)
                if slice_2d is None:
                    continue

                # Apply windowing
                slice_np_rgb, _, _, _, _ = apply_windowing(slice_2d, ww, wl)

                # Save as JPEG
                slice_image = Image.fromarray(slice_np_rgb)
                slice_image.save(os.path.join(slices_dir, f"{i:05d}.jpg"), quality=100)

            except Exception as e:
                logger.warning(f"Error processing slice {i} (axis {axis_name}): {e}")
                continue

        logger.info(f"Temporary slices folder created: {slices_dir} with {num_slices} slices (axis: {axis_name})")
        return slices_dir

    except Exception as e:
        logger.error(f"Error creating temporary slices folder: {e}")
        shutil.rmtree(slices_dir, ignore_errors=True)
        raise


def create_temp_slices_folder_dicom(dicom_files, ww=None, wl=None, output_dir=None):
    """
    Creates a temporary folder with slice images from DICOM files.

    Args:
        dicom_files: List of DICOM file paths
        ww: Window width for windowing (optional)
        wl: Window level for windowing (optional)
        output_dir: Optional output folder

    Returns:
        str: Path to the created temporary folder
    """
    # Import here to avoid circular imports
    from services.image_processing import apply_windowing

    if output_dir is None:
        output_dir = Config.TEMP_UPLOAD_FOLDER

    # Create temporary folder
    slices_dir = tempfile.mkdtemp(dir=output_dir, prefix="slices_dicom_")

    try:
        for i, dicom_path in enumerate(dicom_files):
            try:
                # Load DICOM
                ds = pydicom.dcmread(dicom_path, force=True)
                pixel_array_raw = ds.pixel_array.astype(np.float32)

                # Apply rescale values
                slope = float(getattr(ds, 'RescaleSlope', 1))
                intercept = float(getattr(ds, 'RescaleIntercept', 0))

                if slope != 1 or intercept != 0:
                    raw_data = pixel_array_raw * slope + intercept
                else:
                    raw_data = pixel_array_raw

                # Apply windowing
                slice_np_rgb, _, _, _, _ = apply_windowing(raw_data, ww, wl, ds)

                # Save as JPEG
                slice_image = Image.fromarray(slice_np_rgb)
                slice_image.save(os.path.join(slices_dir, f"{i:05d}.jpg"))

            except Exception as e:
                logger.warning(f"Error processing DICOM {i}: {e}")
                continue

        logger.info(f"Temporary DICOM slices folder created: {slices_dir} with {len(dicom_files)} slices")
        return slices_dir

    except Exception as e:
        logger.error(f"Error creating temporary DICOM slices folder: {e}")
        shutil.rmtree(slices_dir, ignore_errors=True)
        raise


def cleanup_temp_slices_folder(slices_dir):
    """
    Cleans up a temporary slices folder.

    Args:
        slices_dir: Path to the temporary folder
    """
    if slices_dir and os.path.exists(slices_dir):
        try:
            shutil.rmtree(slices_dir)
            logger.debug(f"Temporary slices folder deleted: {slices_dir}")
        except Exception as e:
            logger.warning(f"Error deleting temporary folder {slices_dir}: {e}")
