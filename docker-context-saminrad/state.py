# -*- coding: utf-8 -*-
"""
Global application state for SAM2RAD.
Contains all global variables shared between the various modules.
"""

import numpy as np
from config import Config

# ---------------- Global variables for loaded data ----------------
CURRENT_VOLUME_DATA = None
CURRENT_VOLUME_AFFINE = np.eye(4)
CURRENT_VOLUME_VOXEL_SPACING = (1.0, 1.0, 1.0)
CURRENT_VOLUME_HEADER = None
CURRENT_FILE_TYPE = None
CURRENT_ORIGINAL_FILENAME = None
# Detected, likely erroneous anisotropic voxel spacing (or None).
# Dict: {axis_index, current, suggested, message} - for the upload prompt.
SPACING_ANOMALY = None
DICOM_IMAGE_FILES = []
# The DICOM files the current volume was stacked from - in exactly the order
# of the slice axis (ascending along the slice normal). DICOM_IMAGE_FILES
# stays empty in volume mode (there the file type is 'nifti'), but the
# series' source headers are still needed.
DICOM_SOURCE_FILES = []
USING_UPLOADED_FILE = False
CURRENT_UPLOAD_FILENAME = None

# Origin of the loaded data: 'local' | 'irods' | 'dcm4chee' | 'default'.
# Decides at the end of the segmentation which save paths are offered: only a
# series that also came from the dcm4chee database can be written back as a
# DICOM-SEG. Only the origin is remembered - study and series UID live in the
# headers of the stored files and are read from there when needed (see
# services/file_loader.current_series_uids), so a series switch via
# /select_series doesn't let them go stale.
SOURCE_ORIGIN = None

# DICOMDIR series selection
AVAILABLE_SERIES = []          # List of dicts: {number, description, modality, study, count}
SELECTED_SERIES_NUMBER = None
DICOMDIR_PATH = None           # Path to the detected DICOMDIR file (if present)

# Slice counts per axis
SLICE_COUNTS = {'axial': 0, 'sagittal': 0, 'coronal': 0}

# Axis index mapping for slicing
AXIS_INDEX_FOR_SLICING = {'axial': 2, 'sagittal': 0, 'coronal': 1}

# Global auto-windowing (percentile-based, computed when the volume is loaded)
DEFAULT_WL = None
DEFAULT_WW = None

# Global HU data range of the entire volume (computed when loading).
# Used for the threshold refinement sliders, so that bright objects on other
# slices (higher HU) also fall within the slider range.
GLOBAL_DATA_MIN = None
GLOBAL_DATA_MAX = None

# Global variable for the current archive folder
CURRENT_ARCHIVE_FOLDER = None

# Active image folder (set dynamically)
DEFAULT_IMAGE_FOLDER = Config.IMAGE_FOLDER
IMAGE_FOLDER = DEFAULT_IMAGE_FOLDER

# Temporary upload folder
TEMP_UPLOAD_FOLDER = Config.TEMP_UPLOAD_FOLDER

# Archive folder
ARCHIVE_FOLDER = Config.ARCHIVE_FOLDER

# Allowed file types for upload
ALLOWED_EXTENSIONS = {'dcm', 'dicom', 'nii', 'nii.gz', 'nrrd'}

# SAM2 configuration
SAM2_CHECKPOINT = Config.SAM2_CONFIG['checkpoint']

# Flask configuration
FLASK_PORT = Config.FLASK_CONFIG['port']
FLASK_HOST = Config.FLASK_CONFIG['host']
FLASK_DEBUG = Config.FLASK_CONFIG['debug']

# Downsampling factors
ANALYSIS_MASK_DOWNSAMPLE_FACTOR = Config.PROCESSING['downsample_factors']['analysis']
VIEWER_SPECIFIC_DOWNSAMPLE_FACTOR = Config.PROCESSING['downsample_factors']['viewer']


def reset_global_state():
    """Resets all global variables to their default values."""
    global CURRENT_VOLUME_DATA, CURRENT_VOLUME_AFFINE, CURRENT_VOLUME_VOXEL_SPACING
    global CURRENT_VOLUME_HEADER, CURRENT_FILE_TYPE, CURRENT_ORIGINAL_FILENAME
    global DICOM_IMAGE_FILES, USING_UPLOADED_FILE, CURRENT_UPLOAD_FILENAME, SPACING_ANOMALY
    global SLICE_COUNTS, CURRENT_ARCHIVE_FOLDER, IMAGE_FOLDER
    global AVAILABLE_SERIES, SELECTED_SERIES_NUMBER, DICOMDIR_PATH
    global DICOM_SOURCE_FILES, SOURCE_ORIGIN

    CURRENT_VOLUME_DATA = None
    CURRENT_VOLUME_AFFINE = np.eye(4)
    CURRENT_VOLUME_VOXEL_SPACING = (1.0, 1.0, 1.0)
    CURRENT_VOLUME_HEADER = None
    CURRENT_FILE_TYPE = None
    CURRENT_ORIGINAL_FILENAME = None
    SPACING_ANOMALY = None
    DICOM_IMAGE_FILES = []
    DICOM_SOURCE_FILES = []
    USING_UPLOADED_FILE = False
    CURRENT_UPLOAD_FILENAME = None
    SOURCE_ORIGIN = None
    SLICE_COUNTS = {'axial': 0, 'sagittal': 0, 'coronal': 0}
    CURRENT_ARCHIVE_FOLDER = None
    IMAGE_FOLDER = DEFAULT_IMAGE_FOLDER
    DEFAULT_WL = None
    DEFAULT_WW = None
    AVAILABLE_SERIES = []
    SELECTED_SERIES_NUMBER = None
    DICOMDIR_PATH = None


def get_state_summary():
    """Returns a summary of the current state."""
    return {
        'file_type': CURRENT_FILE_TYPE,
        'original_filename': CURRENT_ORIGINAL_FILENAME,
        'using_uploaded_file': USING_UPLOADED_FILE,
        'upload_filename': CURRENT_UPLOAD_FILENAME,
        'slice_counts': SLICE_COUNTS.copy(),
        'volume_shape': CURRENT_VOLUME_DATA.shape if CURRENT_VOLUME_DATA is not None else None,
        'voxel_spacing': CURRENT_VOLUME_VOXEL_SPACING,
        'image_folder': IMAGE_FOLDER,
        'archive_folder': CURRENT_ARCHIVE_FOLDER,
        'available_series': AVAILABLE_SERIES,
        'selected_series_number': SELECTED_SERIES_NUMBER,
        'dicomdir_path': DICOMDIR_PATH,
        'source_origin': SOURCE_ORIGIN
    }
