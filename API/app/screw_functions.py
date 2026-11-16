"""
Shared pieces of the screw-segmentation flow: locating a study's Micro-CT
series, and cleaning up a raw model mask.

Used by screw_inference_functions.py, which runs the model and uploads the
DICOM SEG. This module used to also hold screw_wrapper(), which converted a
mask that docker-context-gui's Screw Segmentation page had pre-computed and
recorded in API_storage/screw_seg_registry.json. Both that page and the OHIF
toolbar button that called it are gone -- live inference covers the same case
without a pre-processing step -- so nothing writes or reads that registry any
more.
"""
import numpy as np
from scipy import ndimage

from .backend_helpers import find_series

# The raw model output can include small, isolated false-positive blobs
# elsewhere in the image, alongside the real screw -- which itself often
# fragments into several large disconnected pieces (small gaps in the raw
# per-voxel predictions along a thin, helically-threaded structure). Checked
# against a real case: the ~10 largest pieces (5,000-95,000 voxels) were all
# elongated and mostly slice-spanning -- genuine screw fragments -- while
# everything smaller was either compact/isotropic (stray blobs) or 1-3 voxel
# noise. This threshold sits in the size gap between those two groups.
MIN_COMPONENT_VOXELS = 5000


def _find_ct_series_uid(studyInstanceUID):
    """Pick the study's real Micro-CT series, skipping the raw mask (and any prior SEG)."""
    _, series_uids, modalities, descriptions = find_series(studyInstanceUID)
    for uid, modality, description in zip(series_uids, modalities, descriptions):
        description = (description or '').lower()
        if modality == 'CT' and 'mask' not in description and 'seg' not in description:
            return uid
    raise ValueError(f"No Micro-CT series found in study {studyInstanceUID}.")


def _drop_small_components(mask, min_voxels=MIN_COMPONENT_VOXELS):
    """Zero out connected components smaller than min_voxels (see MIN_COMPONENT_VOXELS)."""
    labeled, n = ndimage.label(mask, structure=np.ones((3, 3, 3)))
    sizes = ndimage.sum(mask, labeled, range(1, n + 1))
    keep_labels = [i + 1 for i, size in enumerate(sizes) if size >= min_voxels]
    return np.isin(labeled, keep_labels).astype(np.uint8)
