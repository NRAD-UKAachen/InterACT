"""Storage configuration for all API services"""
import os
from pathlib import Path

# Base storage directory for all API outputs. Must come from the environment
# (API_STORAGE_DIR, set per-stack in the root .env) rather than a hardcoded
# path -- this module is shared by both the `interact` and `interact-production`
# stacks' ldct_api containers, and a hardcoded path here previously made both
# stacks silently share (and overwrite) the same on-disk output directory.
# See API/app/storage_paths.py for the same convention on the main API side.
_env_value = (os.environ.get("API_STORAGE_DIR") or "").rstrip("/")
if not _env_value:
    raise RuntimeError("API_STORAGE_DIR must be set in the environment")
BASE_API_STORAGE = Path(_env_value)

# Service-specific storage paths
STORAGE_PATHS = {
    "ldct_alsaihati": BASE_API_STORAGE / "ldct_alsaihati",

}

# Ensure base directory exists
BASE_API_STORAGE.mkdir(parents=True, exist_ok=True)
