# -*- coding: utf-8 -*-
"""
Configuration file for the SAM2RAD medical image segmentation application.
Contains all important parameters and paths.

This version is portable and uses environment variables.
Configuration is done via a .env file or environment variables.
"""

import os
from pathlib import Path

# Try to load dotenv (optional)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed - no problem


class Config:
    # Base paths
    BASE_DIR = Path(__file__).parent.absolute()

    # Input folder - uses environment variable or default
    IMAGE_FOLDER = os.getenv(
        'IMAGE_FOLDER',
        str(BASE_DIR / 'data' / 'input')
    )

    # Temporary upload folder
    TEMP_UPLOAD_FOLDER = os.getenv(
        'TEMP_UPLOAD_FOLDER',
        str(BASE_DIR / 'data' / 'temp_upload')
    )

    # Archive folder for finished segmentations
    ARCHIVE_FOLDER = os.getenv(
        'ARCHIVE_FOLDER',
        str(BASE_DIR / 'data' / 'archive')
    )

    # SAM2 configuration
    SAM2_CONFIG = {
        'checkpoint': os.getenv(
            'SAM2_CHECKPOINT',
            str(BASE_DIR / 'checkpoints' / 'sam2.1_hiera_large.pt')
        ),
        'model_cfg': os.getenv(
            'SAM2_MODEL_CONFIG',
            'configs/sam2.1/sam2.1_hiera_l.yaml'
        ),
        'device': os.getenv('SAM2_DEVICE', 'cuda')
    }

    # Flask configuration
    FLASK_CONFIG = {
        'port': int(os.getenv('FLASK_PORT', '8003')),
        'debug': os.getenv('FLASK_DEBUG', 'False').lower() == 'true',
        'host': os.getenv('FLASK_HOST', '0.0.0.0')
    }

    # Processing parameters
    PROCESSING = {
        'downsample_factors': {
            'analysis': float(os.getenv('DOWNSAMPLE_ANALYSIS', '1.0')) if os.getenv('DOWNSAMPLE_ANALYSIS') != 'None' else None,
            'viewer': float(os.getenv('DOWNSAMPLE_VIEWER', '0.7'))
        },
        'min_area': int(os.getenv('MIN_AREA', '100')),
        'min_score': float(os.getenv('MIN_SCORE', '0.60'))
    }

    # Supported file formats
    SUPPORTED_FORMATS = {
        'dicom': ('.dcm', '.dicom'),
        'nifti': ('.nii', '.nii.gz'),
        'nrrd': ('.nrrd',)
    }

    # Axis configuration
    AXIS_CONFIG = {
        'index_mapping': {
            'axial': 2,
            'sagittal': 0,
            'coronal': 1
        }
    }

    @classmethod
    def create_directories(cls):
        """Creates all required directories."""
        directories = [
            cls.IMAGE_FOLDER,
            cls.TEMP_UPLOAD_FOLDER,
            cls.ARCHIVE_FOLDER,
            Path(cls.SAM2_CONFIG['checkpoint']).parent,
        ]
        for directory in directories:
            Path(directory).mkdir(parents=True, exist_ok=True)
        print(f"✓ All required directories have been created/verified")

    @classmethod
    def validate_paths(cls):
        """Checks whether all necessary paths and files exist."""
        errors = []
        warnings = []
        if not Path(cls.IMAGE_FOLDER).exists():
            warnings.append(f"⚠ Input folder does not exist yet: {cls.IMAGE_FOLDER}")
        if not Path(cls.SAM2_CONFIG['checkpoint']).exists():
            errors.append(f"✗ SAM2 checkpoint missing: {cls.SAM2_CONFIG['checkpoint']}")
        config_path = Path(cls.SAM2_CONFIG['model_cfg'])
        if not config_path.is_absolute():
            config_path = cls.BASE_DIR / config_path
        if not config_path.exists():
            errors.append(f"✗ SAM2 configuration file missing: {config_path}")
        if warnings:
            print("\n" + "\n".join(warnings))
        if errors:
            print("\n" + "\n".join(errors))
            raise FileNotFoundError("Critical files missing!")
        print("✓ All critical paths are present")

    @classmethod
    def print_config(cls):
        """Prints the current configuration."""
        print("\n" + "="*60)
        print("   SAM2RAD - Medical Image Segmentation - CONFIGURATION")
        print("="*60)
        print(f"\n📁 Directories:")
        print(f"   Base Directory:    {cls.BASE_DIR}")
        print(f"   Input Folder:      {cls.IMAGE_FOLDER}")
        print(f"   Temp Upload:       {cls.TEMP_UPLOAD_FOLDER}")
        print(f"   Archive Folder:    {cls.ARCHIVE_FOLDER}")
        print(f"\n🤖 SAM2 Configuration:")
        print(f"   Checkpoint:        {cls.SAM2_CONFIG['checkpoint']}")
        print(f"   Model Config:      {cls.SAM2_CONFIG['model_cfg']}")
        print(f"   Device:            {cls.SAM2_CONFIG['device']}")
        print(f"\n🌐 Flask Server:")
        print(f"   Host:              {cls.FLASK_CONFIG['host']}")
        print(f"   Port:              {cls.FLASK_CONFIG['port']}")
        print(f"   Debug Mode:        {cls.FLASK_CONFIG['debug']}")
        print("="*60 + "\n")
