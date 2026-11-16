# ldct/steps/step7_1_noisegni.py
from __future__ import annotations
from pathlib import Path
import numpy as np
from scipy.ndimage import uniform_filter
from tqdm import tqdm

# ---------------- helpers ----------------

def _local_std_2d(img: np.ndarray, k: int = 9) -> np.ndarray:
    img = img.astype(np.float32, copy=False)
    mean = uniform_filter(img, size=k, mode="reflect")
    mean_sq = uniform_filter(img * img, size=k, mode="reflect")
    var = mean_sq - mean * mean
    np.maximum(var, 0.0, out=var)
    np.sqrt(var, out=var)
    return var

def compute_histogram_mode(data: np.ndarray) -> float:
    n = len(data)
    if n < 2:
        return np.nan
    iqr = np.percentile(data, 75) - np.percentile(data, 25)
    bin_width = 2 * iqr / np.cbrt(n)
    if np.isclose(bin_width, 0):
        raise ValueError("Data has no variability (IQR=0); unable to compute histogram-based mode.")
    data_min, data_max = data.min(), data.max()
    bins = int(np.ceil((data_max - data_min) / bin_width))
    hist, edges = np.histogram(data, bins=bins)
    idx = np.argmax(hist)
    return float((edges[idx] + edges[idx + 1]) / 2)

def estimate_gni_per_slice(
    vol: np.ndarray, hu_min: int = -300, hu_max: int = 300, ksize: int = 9
) -> np.ndarray:
    """Compute GNI per slice from a 3D noise image volume (Z, H, W)."""
    vol = np.asarray(vol)
    assert vol.ndim == 3, "Input volume must be (slices, height, width)"
    gnies = []
    for slice_img in tqdm(vol, desc="GNI per slice (step7_1)"):
        local_std = _local_std_2d(slice_img, k=ksize)
        mask = (slice_img >= hu_min) & (slice_img <= hu_max)
        vals = local_std[mask]
        gni = compute_histogram_mode(vals) if vals.size else 0.0
        gnies.append(gni)
    return np.asarray(gnies, dtype=np.float32)

# --------------- pipeline entrypoint ---------------

def run(cfg) -> None:
    """
    Step 7.1 — Compute GNI per slice for the filtered noise image.

    Inputs (in cfg.output_dir):
      - filtered_noise_volume.npy   (from step6_2)

    Output:
      - NoiseImage_gni.npy          (per-slice GNI, float32)

    Tunables (optional in cfg):
      - hu_min=-300, hu_max=300
      - noise_gni_ksize=9           # falls back to cfg.ksize or 9
    """
    out = Path(cfg.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    noise_img_path = out / "filtered_noise_volume.npy"
    if not noise_img_path.exists():
        raise FileNotFoundError(f"[step7_1] Missing input: {noise_img_path} (run step6_2 first)")

    vol = np.load(noise_img_path)
    if vol.ndim == 2:
        vol = vol[np.newaxis, ...]  # ensure (Z,H,W)

    print(f"[step7_1] Noise image shape (slices, H, W): {vol.shape}")

    hu_min = -300
    hu_max = 300
    ksize = 9

    gni = estimate_gni_per_slice(vol, hu_min=hu_min, hu_max=hu_max, ksize=int(ksize))

    out_path = out / "NoiseImage_gni.npy"
    np.save(out_path, gni.astype(np.float32))
    print(f"[step7_1] NoiseImage_gni shape: {gni.shape}")
    print(f"[step7_1] Saved per-slice GNI to: {out_path}")
