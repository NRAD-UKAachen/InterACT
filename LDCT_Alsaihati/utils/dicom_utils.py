"""Shared DICOM utilities for extracting patient information"""
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional
import SimpleITK as sitk


def extract_patient_name(dicom_folder: Path) -> str:
    """
    Extract series description from DICOM files for folder naming.

    Args:
        dicom_folder: Path to folder containing DICOM files

    Returns:
        Sanitized series description suitable for filesystem use
    """
    try:
        # Get DICOM series file names
        series_file_names = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(str(dicom_folder))
        if not series_file_names:
            raise FileNotFoundError(f"No DICOM files found in {dicom_folder}")

        # Read the first DICOM file to get series description
        reader = sitk.ImageFileReader()
        reader.SetFileName(series_file_names[0])
        reader.LoadPrivateTagsOn()
        reader.ReadImageInformation()

        # Try to get series description (DICOM tag 0008,103E)
        series_description = reader.GetMetaData("0008|103e") if reader.HasMetaDataKey("0008|103e") else ""

        # Clean up series description for use in file path
        if series_description:
            # Replace special characters and spaces
            series_description = series_description.replace("^", "_").replace(" ", "_").replace("/", "_")
            series_description = "".join(c for c in series_description if c.isalnum() or c == "_")

        # If no series description found, use a default
        if not series_description:
            series_description = f"unknown_series_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        return series_description
    except Exception as e:
        # Fallback to timestamp-based name if extraction fails
        print(f"Warning: Could not extract series description: {e}")
        return f"unknown_series_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def extract_patient_id(dicom_folder: Path) -> Optional[str]:
    """
    Extract patient ID from DICOM files.

    Args:
        dicom_folder: Path to folder containing DICOM files

    Returns:
        Patient ID or None if not found
    """
    try:
        series_file_names = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(str(dicom_folder))
        if not series_file_names:
            return None

        reader = sitk.ImageFileReader()
        reader.SetFileName(series_file_names[0])
        reader.LoadPrivateTagsOn()
        reader.ReadImageInformation()

        # DICOM tag 0010,0020
        return reader.GetMetaData("0010|0020") if reader.HasMetaDataKey("0010|0020") else None
    except Exception as e:
        print(f"Warning: Could not extract patient ID: {e}")
        return None


def extract_study_info(dicom_folder: Path) -> Dict[str, str]:
    """
    Extract comprehensive study information from DICOM files.

    Args:
        dicom_folder: Path to folder containing DICOM files

    Returns:
        Dictionary with study information (StudyInstanceUID, StudyDate, etc.)
    """
    try:
        series_file_names = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(str(dicom_folder))
        if not series_file_names:
            return {}

        reader = sitk.ImageFileReader()
        reader.SetFileName(series_file_names[0])
        reader.LoadPrivateTagsOn()
        reader.ReadImageInformation()

        study_info = {}

        # Common DICOM tags
        tags = {
            "patient_name": "0010|0010",
            "patient_id": "0010|0020",
            "patient_birth_date": "0010|0030",
            "patient_sex": "0010|0040",
            "study_instance_uid": "0020|000d",
            "study_date": "0008|0020",
            "study_time": "0008|0030",
            "study_description": "0008|1030",
            "series_instance_uid": "0020|000e",
            "series_description": "0008|103e",
            "modality": "0008|0060",
        }

        for key, tag in tags.items():
            if reader.HasMetaDataKey(tag):
                study_info[key] = reader.GetMetaData(tag)

        return study_info
    except Exception as e:
        print(f"Warning: Could not extract study info: {e}")
        return {}
