# -*- coding: utf-8 -*-
"""
Helper functions for RADSAM.
Contains general utility functions.
"""

import os
import shutil
import logging
import numpy as np

from config import Config

logger = logging.getLogger(__name__)

# Allowed file types for upload
ALLOWED_EXTENSIONS = {'dcm', 'dicom', 'nii', 'nii.gz', 'nrrd'}


def clear_temp_folder(temp_folder=None):
    """
    Clears the temporary folder for uploads.

    Args:
        temp_folder: Path to the temporary folder (optional, uses Config if not specified)
    """
    if temp_folder is None:
        temp_folder = Config.TEMP_UPLOAD_FOLDER

    for file in os.listdir(temp_folder):
        file_path = os.path.join(temp_folder, file)
        try:
            if os.path.isfile(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            logger.error(f"Error deleting {file_path}: {e}")


def allowed_file(filename):
    """
    Checks whether the file extension is allowed.

    Args:
        filename: Name of the file to check

    Returns:
        bool: True if allowed, otherwise False
    """
    if '.' not in filename:
        return False

    # Check for .nii.gz (special case with double extension)
    if filename.lower().endswith('.nii.gz'):
        return True

    return filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_file_extension(filename):
    """
    Returns the file extension.

    Args:
        filename: Name of the file

    Returns:
        str: File extension in lowercase
    """
    if filename.lower().endswith('.nii.gz'):
        return 'nii.gz'
    return filename.rsplit('.', 1)[1].lower() if '.' in filename else ""


def is_safe_filename(filename):
    """
    Checks whether a file name is safe.

    Args:
        filename: File name to check

    Returns:
        bool: True if the file name is safe, otherwise False
    """
    # Check for path traversal attacks
    if '..' in filename or filename.startswith('/') or '\\' in filename:
        return False

    # Check for invalid characters
    if not all(c.isprintable() and c not in '<>:"|?*' for c in filename):
        return False

    # Check for hidden files
    if filename.startswith('.'):
        return False

    return True


def log_mask_details(mask_data_np, context_message="Mask voxel inspection"):
    """
    Outputs detailed information about a NumPy array (mask).
    Uses the logger system instead of print statements.

    Args:
        mask_data_np: NumPy array of the mask
        context_message: Descriptive text for the context
    """
    logger.debug(f"---- {context_message} ----")

    if mask_data_np is None:
        logger.warning("Mask data is None.")
        return

    logger.debug(f"Mask shape: {mask_data_np.shape}")
    logger.debug(f"Mask dtype: {mask_data_np.dtype}")

    if mask_data_np.size == 0:
        logger.warning("Mask is empty (size 0).")
        return

    try:
        # np.min/max can be slow for large arrays, so only run at debug level
        if logger.level <= logging.DEBUG:
            min_val = np.min(mask_data_np)
            max_val = np.max(mask_data_np)
            logger.debug(f"Minimum voxel value: {min_val}")
            logger.debug(f"Maximum voxel value: {max_val}")

            unique_values, counts = np.unique(mask_data_np, return_counts=True)
            logger.debug("Unique voxel values and their frequencies:")
            for value, count in zip(unique_values, counts):
                logger.debug(f"  Value: {str(value):<5}  Frequency: {count}")

        # This information is important, so always compute it
        num_nonzero_voxels = np.count_nonzero(mask_data_np)
        logger.info(f"Number of non-zero voxels: {num_nonzero_voxels}")

        if num_nonzero_voxels == 0:
            logger.warning("WARNING: According to the NumPy analysis, the mask contains ONLY zero values!")
        else:
            logger.info("Mask contains non-zero voxels.")
    except Exception as e:
        logger.error(f"Error analyzing mask data: {e}")
