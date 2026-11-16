"""Global configuration settings for OHIF API"""
from pathlib import Path
import os

# Base project directory
BASE_DIR = Path(__file__).resolve().parent.parent

# Flask configuration
DEBUG = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
HOST = os.getenv('FLASK_HOST', '0.0.0.0')
PORT = int(os.getenv('FLASK_PORT', '8000'))

# CORS settings
CORS_ORIGINS = os.getenv('CORS_ORIGINS', '*')

# GPU configuration
DEFAULT_GPU_INDEX = int(os.getenv('DEFAULT_GPU_INDEX', '0'))
USE_GPU = os.getenv('USE_GPU', 'True').lower() == 'true'

# Job configuration
JOB_CLEANUP_HOURS = int(os.getenv('JOB_CLEANUP_HOURS', '24'))
MAX_CONCURRENT_JOBS = int(os.getenv('MAX_CONCURRENT_JOBS', '10'))
