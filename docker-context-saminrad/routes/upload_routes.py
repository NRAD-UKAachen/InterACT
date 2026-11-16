# -*- coding: utf-8 -*-
"""
Upload routes for RADSAM.
Contains routes for file uploads and default file selection.
"""

import os
import shutil
import logging
import zipfile
import py7zr
from flask import request, jsonify, redirect, url_for
from werkzeug.utils import secure_filename

from . import upload_bp
from config import Config
from services.file_loader import initialize_data_source, read_dicomdir_records, summarize_series, setup_dicom_series_volume
from services.archive_service import release_archive_session
from utils.helpers import allowed_file, clear_temp_folder
import state

logger = logging.getLogger(__name__)


# Sources reported by the upload page. Anything else (or nothing at all, e.g.
# a form POST outside the GUI) is treated as a local upload.
_KNOWN_SOURCES = ('local', 'irods', 'dcm4chee')


def _record_source_origin():
    """Remembers which source the currently loaded data came from.

    Only the browser knows this: iRODS staging and the dcm4chee import both
    run through the GUI endpoints and arrive here as a perfectly normal ZIP
    (see stageArchive() in templates/upload.html). At the end of the
    segmentation, the origin decides whether the result can also be written
    back to the database as DICOM-SEG.
    """
    source = (request.form.get('source') or '').strip().lower()
    state.SOURCE_ORIGIN = source if source in _KNOWN_SOURCES else 'local'
    logger.info(f"Data source: {state.SOURCE_ORIGIN}")


@upload_bp.route('/upload', methods=['POST'])
def handle_file_upload():
    """
    Handles the file upload.
    Supports single files (NIfTI, NRRD) and ZIP archives with DICOM series.
    """
    logger.info("File upload started...")

    # Folder upload (webkitdirectory): multiple files with relative paths
    folder_files = request.files.getlist('files[]')
    if folder_files and any(f.filename for f in folder_files):
        release_archive_session()
        clear_temp_folder()
        try:
            return _handle_folder_upload(folder_files)
        except Exception as e:
            logger.error(f"Error during folder upload: {e}")
            return jsonify({'error': str(e)}), 500

    # Check whether a file is present in the request
    if 'file' not in request.files:
        return jsonify({'error': 'No file in request'}), 400

    file = request.files['file']

    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    # Reset archive session
    release_archive_session()

    # Clear temporary folder
    clear_temp_folder()

    filename = secure_filename(file.filename)
    logger.info(f"Processing uploaded file: {filename}")

    try:
        # ZIP archives for DICOM series
        if filename.lower().endswith('.zip'):
            return _handle_zip_upload(file, filename)

        # 7z archives for DICOM series
        elif filename.lower().endswith('.7z'):
            return _handle_7z_upload(file, filename)

        # Single files (NIfTI, NRRD)
        elif allowed_file(filename):
            return _handle_single_file_upload(file, filename)

        else:
            return jsonify({'error': f'Unsupported file format: {filename}'}), 400

    except Exception as e:
        logger.error(f"Error during file upload: {e}")
        return jsonify({'error': str(e)}), 500


def _flatten_nested_files(root):
    """
    Moves all files from subfolders up to the top level of `root`.

    Apart from the DICOMDIR search, initialize_data_source() only looks at
    the top level of IMAGE_FOLDER. Staged archives, however, are nested:
    dcm4chee delivers a ZIP of the form DICOM/<hash>/<hash>/<hash>.dcm via
    WADO-RS, and an iRODS folder brings its own tree structure. Only called
    as a fallback when the normal initialization found nothing - a valid
    DICOMDIR structure had already succeeded by that point.

    Returns:
        bool: True if files were moved (a second attempt is worthwhile).
    """
    moved = 0
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        if os.path.abspath(dirpath) == os.path.abspath(root):
            continue
        for fname in filenames:
            target = os.path.join(root, fname)
            if os.path.exists(target):
                # Files with the same name from different subfolders
                stem, ext = os.path.splitext(fname)
                target = os.path.join(root, f"{stem}__{moved}{ext}")
            shutil.move(os.path.join(dirpath, fname), target)
            moved += 1

    if moved:
        for entry in os.listdir(root):
            entry_path = os.path.join(root, entry)
            if os.path.isdir(entry_path):
                shutil.rmtree(entry_path, ignore_errors=True)

    return moved > 0


def _finish_extracted_archive(extract_path, name, source_label):
    """
    Common completion step for all extracted archives (ZIP, 7z).

    Uses initialize_data_source() to check whether the archive contains any
    supported image data at all - previously only DICOM was searched for
    here, which meant a ZIP with NIfTI or NRRD (as delivered by iRODS) was
    rejected for no reason.
    """
    state.USING_UPLOADED_FILE = True
    state.CURRENT_UPLOAD_FILENAME = name
    state.IMAGE_FOLDER = extract_path

    initialize_data_source()

    if state.CURRENT_FILE_TYPE is None and _flatten_nested_files(extract_path):
        logger.info("No data at the top level - archive was flattened.")
        initialize_data_source()

    if state.CURRENT_FILE_TYPE is None:
        shutil.rmtree(extract_path, ignore_errors=True)
        return jsonify({
            'error': f'No supported image data found in {source_label}'
        }), 400

    logger.info(f"{source_label} loaded successfully: {state.CURRENT_FILE_TYPE} ({name})")
    _record_source_origin()

    # Direct redirect to the viewer
    return redirect(url_for('main.dicom_viewer'))


def _handle_zip_upload(file, filename):
    """
    Handles a ZIP archive (DICOM series, NIfTI, NRRD).
    """
    # Save ZIP temporarily
    zip_path = os.path.join(state.TEMP_UPLOAD_FOLDER, filename)
    file.save(zip_path)

    # Create subfolder for the extracted files
    extract_folder_name = os.path.splitext(filename)[0]
    extract_path = os.path.join(state.TEMP_UPLOAD_FOLDER, extract_folder_name)
    os.makedirs(extract_path, exist_ok=True)

    # Extract ZIP
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(extract_path)

    # Delete the ZIP file
    os.remove(zip_path)

    return _finish_extracted_archive(extract_path, extract_folder_name, 'ZIP archive')


def _handle_7z_upload(file, filename):
    """
    Handles a 7z archive (DICOM series, NIfTI, NRRD).
    """
    archive_path = os.path.join(state.TEMP_UPLOAD_FOLDER, filename)
    file.save(archive_path)

    extract_folder_name = os.path.splitext(filename)[0]
    extract_path = os.path.join(state.TEMP_UPLOAD_FOLDER, extract_folder_name)
    os.makedirs(extract_path, exist_ok=True)

    with py7zr.SevenZipFile(archive_path, mode='r') as z:
        z.extractall(path=extract_path)

    os.remove(archive_path)

    return _finish_extracted_archive(extract_path, extract_folder_name, '7z archive')


def _sanitize_relpath(relpath):
    """
    Sanitizes a relative path supplied by the browser, prevents path traversal
    and enforces clean filenames per segment.
    """
    relpath = (relpath or '').replace('\\', '/').lstrip('/')
    parts = [p for p in relpath.split('/') if p and p not in ('.', '..')]
    if not parts:
        return None
    # Filename: secure_filename, but DICOM files without an extension (IMG00001) are kept
    safe_parts = []
    for p in parts[:-1]:
        sp = secure_filename(p)
        if sp:
            safe_parts.append(sp)
    fname = parts[-1]
    safe_fname = secure_filename(fname) or fname  # DICOM files without a '.' may remain
    safe_parts.append(safe_fname)
    return os.path.join(*safe_parts)


def _handle_folder_upload(files):
    """
    Saves a folder uploaded via webkitdirectory to the temp area while
    preserving the directory structure, and initializes the data source.
    """
    # Derive top-level folder name from the first relative path
    first_rel = (files[0].filename or '').replace('\\', '/')
    root_name = first_rel.split('/')[0] if '/' in first_rel else 'uploaded_folder'
    root_name = secure_filename(root_name) or 'uploaded_folder'

    extract_path = os.path.join(state.TEMP_UPLOAD_FOLDER, root_name)
    if os.path.exists(extract_path):
        shutil.rmtree(extract_path)
    os.makedirs(extract_path, exist_ok=True)

    saved_count = 0
    for f in files:
        rel = _sanitize_relpath(f.filename)
        if not rel:
            continue
        # First path component matches root_name -> remove it so we don't nest it twice
        parts = rel.split(os.sep)
        if len(parts) > 1 and secure_filename(parts[0]) == root_name:
            parts = parts[1:]
        if not parts:
            continue
        target = os.path.join(extract_path, *parts)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        f.save(target)
        saved_count += 1

    if saved_count == 0:
        shutil.rmtree(extract_path, ignore_errors=True)
        return jsonify({'error': 'No files found in the folder upload'}), 400

    state.USING_UPLOADED_FILE = True
    state.CURRENT_UPLOAD_FILENAME = root_name
    state.IMAGE_FOLDER = extract_path

    initialize_data_source()

    if state.CURRENT_FILE_TYPE is None:
        return jsonify({
            'error': 'No supported medical image data found in the folder',
            'folder': extract_path
        }), 400

    logger.info(f"Folder upload successful: {saved_count} files saved to {extract_path}.")
    _record_source_origin()
    return redirect(url_for('main.dicom_viewer'))


def _handle_single_file_upload(file, filename):
    """
    Handles a single file (NIfTI, NRRD).
    """
    # Save file
    file_path = os.path.join(state.TEMP_UPLOAD_FOLDER, filename)
    file.save(file_path)

    # Set global variables
    state.USING_UPLOADED_FILE = True
    state.CURRENT_UPLOAD_FILENAME = filename
    state.IMAGE_FOLDER = state.TEMP_UPLOAD_FOLDER

    # Initialize data source
    initialize_data_source()

    logger.info(f"Single file upload successful: {filename}")
    _record_source_origin()

    # Direct redirect to the viewer
    return redirect(url_for('main.dicom_viewer'))


@upload_bp.route('/use_default')
def use_default():
    """
    Uses the default data from the configured input folder.
    """
    logger.info("Using default data...")

    # Reset archive session
    release_archive_session()

    # Reset to default folder
    state.USING_UPLOADED_FILE = False
    state.CURRENT_UPLOAD_FILENAME = None
    state.IMAGE_FOLDER = state.DEFAULT_IMAGE_FOLDER

    # Initialize data source
    initialize_data_source()

    if state.CURRENT_FILE_TYPE is None:
        return jsonify({
            'error': 'No files found in the default folder',
            'folder': state.DEFAULT_IMAGE_FOLDER
        }), 404

    logger.info(f"Default data loaded: {state.CURRENT_ORIGINAL_FILENAME}")
    state.SOURCE_ORIGIN = 'default'

    return redirect(url_for('main.dicom_viewer'))


@upload_bp.route('/list_series', methods=['GET'])
def list_series():
    """
    Returns the series found in the currently loaded DICOMDIR.
    """
    return jsonify({
        'available_series': state.AVAILABLE_SERIES,
        'selected_series_number': state.SELECTED_SERIES_NUMBER,
        'dicomdir_path': state.DICOMDIR_PATH,
    })


@upload_bp.route('/select_series', methods=['POST'])
def select_series():
    """
    Switches to a different series within the current DICOMDIR.
    Expects JSON: {"series_number": <int>}
    """
    if not state.DICOMDIR_PATH or not os.path.isfile(state.DICOMDIR_PATH):
        return jsonify({'error': 'No DICOMDIR loaded'}), 400

    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        series_number = int(data.get('series_number'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid series_number'}), 400

    try:
        records = read_dicomdir_records(state.DICOMDIR_PATH)
    except Exception as e:
        logger.error(f"Error reading the DICOMDIR: {e}")
        return jsonify({'error': f'DICOMDIR read error: {e}'}), 500

    image_files = [r['path'] for r in records if r['series'] == series_number]
    if not image_files:
        return jsonify({'error': f'Series {series_number} contains no images'}), 404

    # Reset archive session so no old segmentation is left over
    release_archive_session()

    state.AVAILABLE_SERIES = summarize_series(records)
    state.SELECTED_SERIES_NUMBER = series_number
    original_filename = os.path.basename(os.path.dirname(state.DICOMDIR_PATH))
    # Invalidate the volume cache so the new series is read in again
    state.CURRENT_VOLUME_DATA = None
    state.CURRENT_VOLUME_HEADER = None
    state.DICOM_IMAGE_FILES = []
    state.DEFAULT_WL = None
    state.DEFAULT_WW = None

    # Preferred: stack the series into a volume -> all three axes available
    if setup_dicom_series_volume(image_files, original_filename):
        logger.info(f"Switched to series {series_number} (volume {state.CURRENT_VOLUME_DATA.shape}, {len(image_files)} images)")
    else:
        # Fallback: axial file-list mode (axial view only)
        state.CURRENT_FILE_TYPE = 'dicom_series'
        state.CURRENT_ORIGINAL_FILENAME = original_filename
        state.DICOM_IMAGE_FILES = image_files
        state.SLICE_COUNTS['axial'] = len(image_files)
        state.SLICE_COUNTS['sagittal'] = 0
        state.SLICE_COUNTS['coronal'] = 0
        logger.info(f"Switched to series {series_number} (axial fallback, {len(image_files)} images)")
    return jsonify({
        'success': True,
        'selected_series_number': series_number,
        'slice_counts': state.SLICE_COUNTS,
    })


@upload_bp.route('/select_file', methods=['POST'])
def select_file():
    """
    Handles the selection of a file from the input folder.
    """
    try:
        data = request.json
        filename = data.get('filename')

        if not filename:
            return jsonify({'error': 'No filename provided'}), 400

        filepath = os.path.join(state.IMAGE_FOLDER, filename)

        # Check whether the file/folder exists
        if not os.path.exists(filepath):
            return jsonify({'error': f'File not found: {filename}'}), 404

        # Reset archive session
        release_archive_session()

        # Reset global variables
        state.reset_global_state()

        # Initialize based on file type
        if os.path.isdir(filepath):
            # DICOM folder
            state.IMAGE_FOLDER = filepath
            initialize_data_source()

            return jsonify({
                'success': True,
                'file_type': state.CURRENT_FILE_TYPE,
                'slice_counts': state.SLICE_COUNTS,
                'redirect': url_for('main.dicom_viewer')
            })
        else:
            # Single file (NIfTI, NRRD)
            state.IMAGE_FOLDER = os.path.dirname(filepath)
            initialize_data_source()

            return jsonify({
                'success': True,
                'file_type': state.CURRENT_FILE_TYPE,
                'slice_counts': state.SLICE_COUNTS,
                'redirect': url_for('main.dicom_viewer')
            })

    except Exception as e:
        logger.error(f"Error during file selection: {e}")
        return jsonify({'error': str(e)}), 500
