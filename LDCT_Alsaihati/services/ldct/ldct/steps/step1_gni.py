# ldct/steps/step1_gni.py
from __future__ import annotations
from pathlib import Path
import numpy as np
from scipy.ndimage import uniform_filter, uniform_filter1d
from tqdm import tqdm

# ---------- helpers ----------

def _local_std_2d(img: np.ndarray, k: int = 9) -> np.ndarray:
    img = img.astype(np.float32, copy=False)
    mean = uniform_filter(img, size=k, mode="reflect")
    mean_sq = uniform_filter(img * img, size=k, mode="reflect")
    var = mean_sq - mean * mean
    np.maximum(var, 0.0, out=var)   # clamp tiny negatives due to FP error
    np.sqrt(var, out=var)           # keep float32
    return var

def compute_histogram_mode(data: np.ndarray) -> float:
    n = len(data)
    if n < 2:
        return np.nan

    iqr = np.percentile(data, 75) - np.percentile(data, 25)
    bin_width = 2 * iqr / np.cbrt(n)
    if bin_width == 0:
        raise ValueError("Data has no variability (IQR=0); unable to compute histogram-based mode.")

    data_max = data.max()
    data_min = data.min()
    bins = int(np.ceil((data_max - data_min) / bin_width))
    hist, bin_edges = np.histogram(data, bins=bins)
    max_bin_index = np.argmax(hist)
    mode_est = (bin_edges[max_bin_index] + bin_edges[max_bin_index + 1]) / 2
    return float(mode_est)

def estimate_gni_per_slice(
    ct_array: np.ndarray,
    hu_min: int = -300,
    hu_max: int = 300,
    ksize: int = 9,
    smooth: int = 5,
) -> np.ndarray:
    """
    Compute smoothed GNI per slice from a 3D CT array (z, y, x).
    Returns a 1D float32 array of length = num_slices.
    """
    ct_array = np.asarray(ct_array)
    assert ct_array.ndim == 3, "ct_array must be (slices, height, width)"
    gnies = []
    for slice_img in tqdm(ct_array, desc="GNI per slice"):
        local_std = _local_std_2d(slice_img, k=ksize)
        mask = (slice_img >= hu_min) & (slice_img <= hu_max)
        vals = local_std[mask]
        if vals.size == 0:
            gnies.append(0.0)
        else:
            try:
                gni = compute_histogram_mode(vals)
            except ValueError:
                gni = 0.0
            gnies.append(gni)
    gnies = np.asarray(gnies, dtype=np.float32)
    smoothed = uniform_filter1d(gnies, size=smooth, mode="reflect")
    return smoothed.astype(np.float32, copy=False)

# ---------- pipeline entrypoint ----------

def run(cfg) -> None:
    """
    Loads original_ct_array.npy from cfg.output_dir, computes smoothed GNI per slice,
    and saves smoothed_gni.npy back to cfg.output_dir.

    Optional cfg attributes (with defaults):
      - hu_min (-300), hu_max (300), ksize (9), smooth (5)
    """
    output_dir: Path = Path(cfg.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ct_path = output_dir / "original_ct_array.npy"
    if not ct_path.exists():
        raise FileNotFoundError(f"Input not found: {ct_path}. Did step0 run successfully?")

    ct_array = np.load(ct_path)
    print(f"[step1] CT array shape (slices, height, width): {ct_array.shape}")

    hu_min = -300
    hu_max = 300
    ksize  = 9
    smooth = 5
    smoothed_gni = estimate_gni_per_slice(
        ct_array, hu_min=hu_min, hu_max=hu_max, ksize=ksize, smooth=smooth
    )

    out_path = output_dir / "smoothed_gni.npy"
    np.save(out_path, smoothed_gni)
    print(f"[step1] Smoothed GNI shape: {smoothed_gni.shape}")
    print(f"[step1] Saved to: {out_path}")