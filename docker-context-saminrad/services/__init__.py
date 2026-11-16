# -*- coding: utf-8 -*-
"""
Services module for RADSAM.
Contains the core logic for file handling, image processing and segmentation.
"""

from .file_loader import (
    load_nifti_volume,
    load_nrrd_volume,
    initialize_data_source,
    setup_volume_data
)

from .image_processing import (
    get_slice_from_volume,
    apply_windowing,
    image_to_base64,
    save_cutout_image
)

from .archive_service import (
    create_archive_folder,
    reset_archive_session,
    release_archive_session,
    save_segmentation_parameters,
    archive_results
)

from .mask_processing import (
    create_nifti_mask_from_arrays,
    post_process_nifti_mask,
    apply_threshold_refinement_to_mask,
    extract_surface
)

from .sam_service import (
    get_predictor,
    is_sam_available,
    collect_sam_annotations_from_request
)

__all__ = [
    # file_loader
    'load_nifti_volume',
    'load_nrrd_volume',
    'initialize_data_source',
    'setup_volume_data',
    # image_processing
    'get_slice_from_volume',
    'apply_windowing',
    'image_to_base64',
    'save_cutout_image',
    # archive_service
    'create_archive_folder',
    'reset_archive_session',
    'release_archive_session',
    'save_segmentation_parameters',
    'archive_results',
    # mask_processing
    'create_nifti_mask_from_arrays',
    'post_process_nifti_mask',
    'apply_threshold_refinement_to_mask',
    'extract_surface',
    # sam_service
    'get_predictor',
    'is_sam_available',
    'collect_sam_annotations_from_request'
]
