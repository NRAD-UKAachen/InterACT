# -*- coding: utf-8 -*-
"""
Archiving service for RADSAM.
Contains functions for archiving segmentation results.
"""

import os
import shutil
import datetime
import json
import logging
import traceback

logger = logging.getLogger(__name__)


def create_archive_folder():
    """
    Creates an archive folder with the current date and timestamp, or returns the existing one.

    Returns:
        str: path to the archive folder
    """
    import state

    # If an archive folder already exists for this session, use it
    if state.CURRENT_ARCHIVE_FOLDER and os.path.exists(state.CURRENT_ARCHIVE_FOLDER):
        return state.CURRENT_ARCHIVE_FOLDER

    now = datetime.datetime.now()
    date_folder = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H-%M-%S")

    base_archive_folder = state.ARCHIVE_FOLDER

    # Create the date folder if it does not exist
    date_path = os.path.join(base_archive_folder, date_folder)
    os.makedirs(date_path, exist_ok=True)

    # Create the timestamp folder for this session
    if state.CURRENT_UPLOAD_FILENAME:
        # Use the filename without extension
        base_name = os.path.splitext(state.CURRENT_UPLOAD_FILENAME)[0]
        archive_folder = os.path.join(date_path, f"{time_str}_{base_name}")
    else:
        # Fallback if no filename is available
        archive_folder = os.path.join(date_path, f"{time_str}_segmentation")

    os.makedirs(archive_folder, exist_ok=True)

    # Store the current archive folder
    state.CURRENT_ARCHIVE_FOLDER = archive_folder

    return archive_folder


def create_new_archive_session(preserve_previous=True):
    """
    Creates a new timestamped archive folder for a new session.

    Args:
        preserve_previous: if True, the previous archive folder is not deleted

    Returns:
        str: path to the new archive folder
    """
    import state

    previous_archive = state.CURRENT_ARCHIVE_FOLDER

    # Reset the global variable so create_archive_folder() creates a new folder
    state.CURRENT_ARCHIVE_FOLDER = None

    # Create a new archive folder with a new timestamp
    new_archive_folder = create_archive_folder()

    logger.info(f"New archive session created: {new_archive_folder}")
    if preserve_previous and previous_archive:
        logger.info(f"Previous session is preserved: {previous_archive}")

    return new_archive_folder


def reset_archive_session():
    """Resets the archive session for a new segmentation."""
    import state

    # If a current archive folder exists, delete it entirely.
    if state.CURRENT_ARCHIVE_FOLDER and os.path.exists(state.CURRENT_ARCHIVE_FOLDER):
        try:
            shutil.rmtree(state.CURRENT_ARCHIVE_FOLDER)
            logger.info(f"Deleted the session's complete archive folder: {state.CURRENT_ARCHIVE_FOLDER}")
        except Exception as e:
            logger.error(f"Error deleting archive folder {state.CURRENT_ARCHIVE_FOLDER}: {e}")
            logger.debug(traceback.format_exc())

    # Reset the global variable
    state.CURRENT_ARCHIVE_FOLDER = None


def release_archive_session():
    """
    Releases the current archive session without deleting the folder from disk.
    The previous archive folder is preserved; the next call to
    ``create_archive_folder()`` creates a new folder with its own timestamp.
    """
    import state

    if state.CURRENT_ARCHIVE_FOLDER:
        logger.info(f"Previous archive folder is preserved: {state.CURRENT_ARCHIVE_FOLDER}")
    state.CURRENT_ARCHIVE_FOLDER = None


def save_segmentation_parameters(folder_path, parameters):
    """
    Saves the segmentation parameters to a JSON file.

    Args:
        folder_path: path to the folder where the file should be saved
        parameters: dictionary with the segmentation parameters

    Returns:
        bool: True on success, False on error
    """
    try:
        # Add a timestamp
        parameters['timestamp'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        params_file = os.path.join(folder_path, "segmentation_parameters.json")

        # Preserve already existing threshold refinement parameters (e.g. from
        # /refine_sam_with_threshold) that would otherwise be overwritten by the final write.
        if os.path.exists(params_file):
            try:
                with open(params_file, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
                if 'hybrid_refinement' in existing and 'hybrid_refinement' not in parameters:
                    parameters['hybrid_refinement'] = existing['hybrid_refinement']
            except Exception as e:
                logger.warning(f"Could not merge existing segmentation_parameters.json: {e}")

        with open(params_file, 'w', encoding='utf-8') as f:
            json.dump(parameters, f, indent=4, ensure_ascii=False)

        logger.info(f"Segmentation parameters saved to: {params_file}")
        return True
    except Exception as e:
        logger.error(f"Error saving segmentation parameters: {e}")
        logger.debug(traceback.format_exc())
        return False


def archive_results(mask_path=None, overlay_path=None, nifti_path=None, nifti_paths=None, segmentation_params=None):
    """
    Archives the segmentation results.

    Args:
        mask_path: path to the mask file (optional)
        overlay_path: path to the overlay file (optional)
        nifti_path: path to the NIfTI file (optional, deprecated)
        nifti_paths: list of NIfTI file paths (optional)
        segmentation_params: dictionary with segmentation parameters (optional)

    Returns:
        bool: True on success, False on error
    """
    import state

    try:
        # Create the archive folder
        archive_folder = create_archive_folder()
        logger.info(f"Archiving results to: {archive_folder}")

        # Create a subfolder for the original data
        original_folder = os.path.join(archive_folder, "original_data")
        os.makedirs(original_folder, exist_ok=True)

        # Copy the original file or folder
        if state.USING_UPLOADED_FILE and state.CURRENT_UPLOAD_FILENAME:
            # For uploaded files
            src_path = os.path.join(state.TEMP_UPLOAD_FOLDER, state.CURRENT_UPLOAD_FILENAME)
            if os.path.exists(src_path):
                if os.path.isdir(src_path):
                    # If it's a folder (e.g. a DICOM series), copy the entire folder
                    dst_folder = os.path.join(original_folder, state.CURRENT_UPLOAD_FILENAME)
                    shutil.copytree(src_path, dst_folder)
                    logger.info(f"Original folder archived: {dst_folder}")
                else:
                    # If it's a single file
                    dst_file = os.path.join(original_folder, state.CURRENT_UPLOAD_FILENAME)
                    shutil.copy2(src_path, dst_file)
                    logger.info(f"Original file archived: {dst_file}")
        else:
            # For standard files (not uploaded)
            input_folder_name = os.path.basename(state.IMAGE_FOLDER)
            dst_folder = os.path.join(original_folder, input_folder_name)

            # Create a metadata file with information about the original data
            metadata_info = {
                "original_data_location": state.IMAGE_FOLDER,
                "original_filename": state.CURRENT_ORIGINAL_FILENAME or "standard_data",
                "file_type": state.CURRENT_FILE_TYPE,
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

            # Save the metadata to a JSON file
            metadata_file = os.path.join(original_folder, "original_data_metadata.json")
            with open(metadata_file, 'w', encoding='utf-8') as f:
                json.dump(metadata_info, f, indent=4, ensure_ascii=False)

            # Copy the entire input folder
            try:
                if os.path.exists(state.IMAGE_FOLDER):
                    if os.path.exists(dst_folder):
                        shutil.rmtree(dst_folder)
                    shutil.copytree(state.IMAGE_FOLDER, dst_folder)
                    logger.info(f"Original folder (input folder) archived: {dst_folder}")
                else:
                    logger.warning(f"Input folder does not exist: {state.IMAGE_FOLDER}")
            except Exception as e:
                logger.error(f"Error copying the input folder: {e}")
                logger.debug(traceback.format_exc())

        # Create a subfolder for the segmentation results
        results_folder = os.path.join(archive_folder, "segmentation_results")
        os.makedirs(results_folder, exist_ok=True)

        # Copy the mask file, if present
        if mask_path and os.path.exists(mask_path):
            mask_filename = os.path.basename(mask_path)
            dst_file = os.path.join(results_folder, "mask_" + mask_filename)
            shutil.copy2(mask_path, dst_file)
            logger.info(f"Mask file archived: {dst_file}")

        # Copy the overlay file, if present
        if overlay_path and os.path.exists(overlay_path):
            overlay_filename = os.path.basename(overlay_path)
            dst_file = os.path.join(results_folder, "overlay_" + overlay_filename)
            shutil.copy2(overlay_path, dst_file)
            logger.info(f"Overlay file archived: {dst_file}")

        # Save segmentation parameters, if present
        if segmentation_params:
            # Add general information
            original_name = state.CURRENT_UPLOAD_FILENAME if state.USING_UPLOADED_FILE else state.CURRENT_ORIGINAL_FILENAME

            segmentation_params['file_info'] = {
                'original_filename': original_name,
                'using_uploaded_file': state.USING_UPLOADED_FILE,
                'image_folder': state.IMAGE_FOLDER,
                'file_type': state.CURRENT_FILE_TYPE,
                'slice_counts': state.SLICE_COUNTS
            }

            # Save the parameters to a JSON file
            save_segmentation_parameters(results_folder, segmentation_params)

        return True
    except Exception as e:
        logger.error(f"Error archiving the results: {e}")
        logger.debug(traceback.format_exc())
        return False
