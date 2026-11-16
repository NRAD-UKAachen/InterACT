# -*- coding: utf-8 -*-
"""
Main routes for RADSAM.
Contains the basic pages and configuration routes.
"""

import os
import logging
from flask import render_template, jsonify, redirect, url_for, request

from . import main_bp
from config import Config
from services.file_loader import initialize_data_source
from services.archive_service import reset_archive_session, release_archive_session, create_new_archive_session
import state

logger = logging.getLogger(__name__)


@main_bp.route('/')
def index():
    """Shows the upload page.

    iRODS and the dcm4chee database are only reachable when this page runs
    inside the InterACT GUI (docker-context-gui/app/routes/reverse_proxy.py
    then sets X-Forwarded-Prefix). The browser reads both sources through the
    already-authenticated GUI endpoints - this container holds neither an
    iRODS nor a Keycloak credential, which keeps the GUI's PHI audit trail
    complete. When accessed directly on port 8003, both options therefore
    stay disabled.
    """
    return render_template(
        'upload.html',
        embedded_in_gui=bool(request.headers.get('X-Forwarded-Prefix')),
    )


@main_bp.route('/start_new_session', methods=['POST'])
def start_new_session():
    """
    Starts a completely new session:
    - Resets all global variables
    - Releases the current archive folder (the previous one is kept on disk)
    - Redirects to the upload page
    """
    logger.info("Starting new session...")

    # Release archive session (previous folder is kept)
    release_archive_session()

    # Reset global variables
    state.reset_global_state()

    logger.info("New session started successfully.")
    return jsonify({'success': True, 'message': 'New session started'})


@main_bp.route('/get_current_config')
def get_current_config():
    """
    Returns the current configuration for the frontend.
    """
    config_data = {
        'file_type': state.CURRENT_FILE_TYPE,
        'original_filename': state.CURRENT_ORIGINAL_FILENAME,
        'slice_counts': state.SLICE_COUNTS,
        'using_uploaded_file': state.USING_UPLOADED_FILE,
        'voxel_spacing': state.CURRENT_VOLUME_VOXEL_SPACING,
        'spacing_anomaly': state.SPACING_ANOMALY
    }

    # Add filenames for DICOM series
    if state.CURRENT_FILE_TYPE == 'dicom_series' and state.DICOM_IMAGE_FILES:
        config_data['dicom_filenames'] = [os.path.basename(f) for f in state.DICOM_IMAGE_FILES]

    return jsonify(config_data)


@main_bp.route('/fix_spacing', methods=['POST'])
def fix_spacing():
    """
    Corrects a detected, likely erroneous voxel spacing (see state.SPACING_ANOMALY):
    sets the length of the affected affine column to the suggested value (direction is kept),
    updates CURRENT_VOLUME_VOXEL_SPACING and clears the anomaly. This way the later 3D mask
    inherits the corrected spacing (no more stretched model).
    """
    import numpy as np
    anom = state.SPACING_ANOMALY
    if not anom:
        return jsonify({'success': False, 'error': 'No spacing anomaly present'}), 400
    try:
        ax = int(anom['axis_index'])
        sugg = float(anom['suggested'])
        aff = np.array(state.CURRENT_VOLUME_AFFINE, dtype=float).copy()
        col = aff[:3, ax]
        norm = float(np.linalg.norm(col))
        if norm > 1e-9:
            aff[:3, ax] = col * (sugg / norm)   # keep direction, magnitude (spacing) = suggested
        else:
            aff[ax, ax] = sugg
        state.CURRENT_VOLUME_AFFINE = aff
        sp = list(state.CURRENT_VOLUME_VOXEL_SPACING)
        if ax < len(sp):
            sp[ax] = sugg
        state.CURRENT_VOLUME_VOXEL_SPACING = tuple(sp)
        old = anom.get('current')
        state.SPACING_ANOMALY = None
        logger.info(f"Voxel spacing corrected: axis {anom.get('axis_name')} {old} -> {sugg} mm")
        return jsonify({'success': True, 'axis': anom.get('axis_name'), 'from': old, 'to': sugg})
    except Exception as e:
        logger.error(f"Error during spacing correction: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/dicom_viewer')
def dicom_viewer():
    """
    Shows the DICOM/NIfTI/NRRD viewer.
    """
    import json

    # Check whether data is loaded
    if state.CURRENT_FILE_TYPE is None:
        logger.warning("No file type loaded. Initializing data source...")
        initialize_data_source()

    if state.CURRENT_FILE_TYPE is None:
        logger.error("No data available to display.")
        return redirect(url_for('main.index'))

    # Filenames for DICOM series
    image_filenames = []
    if state.CURRENT_FILE_TYPE == 'dicom_series' and state.DICOM_IMAGE_FILES:
        image_filenames = [os.path.basename(f) for f in state.DICOM_IMAGE_FILES]

    return render_template(
        'index_dicom.html',
        image_filenames=json.dumps(image_filenames),
        initial_file_type=state.CURRENT_FILE_TYPE,
        initial_slice_counts=json.dumps(state.SLICE_COUNTS),
        initial_base_filename=json.dumps(state.CURRENT_ORIGINAL_FILENAME or ''),
        available_series=json.dumps(state.AVAILABLE_SERIES or []),
        selected_series_number=json.dumps(state.SELECTED_SERIES_NUMBER)
    )


@main_bp.route('/set_method', methods=['POST'])
def set_method():
    """
    Sets the active segmentation method.
    (Used by the frontend to switch between SAM and threshold)
    """
    from flask import request

    # Support both JSON and form data
    if request.is_json:
        data = request.json
    else:
        data = request.form.to_dict() if request.form else {}

    method = data.get('method', 'sam')

    logger.info(f"Segmentation method switched to: {method}")

    return jsonify({'success': True, 'method': method})


@main_bp.route('/back_to_annotation', methods=['POST'])
def back_to_annotation():
    """
    Creates a new archive session but keeps the original data loaded.
    Allows the user to start with new annotations.
    """
    logger.info("Back to annotation - creating new archive session...")

    try:
        # Create new archive session, previous one is kept
        new_archive = create_new_archive_session(preserve_previous=True)

        logger.info(f"New archive session created: {new_archive}")
        return jsonify({
            'success': True,
            'message': 'New annotation session started. Original data remains loaded.',
            'archive_folder': new_archive
        })

    except Exception as e:
        logger.error(f"Error in back_to_annotation: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@main_bp.route('/retry_from_windowing', methods=['POST'])
def retry_from_windowing():
    """
    Resets the segmentation and allows new windowing settings.
    Keeps the loaded data but deletes the previous segmentation results.
    """
    logger.info("Retry from windowing - resetting segmentation...")

    try:
        # Reset archive session (deletes the folder with results)
        reset_archive_session()

        logger.info("Segmentation reset. Ready for new windowing.")
        return jsonify({
            'success': True,
            'message': 'Segmentation reset. You can now choose new windowing settings.'
        })

    except Exception as e:
        logger.error(f"Error in retry_from_windowing: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
