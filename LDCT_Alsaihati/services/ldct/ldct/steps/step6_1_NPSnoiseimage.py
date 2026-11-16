# ldct/steps/step6_1_NPSnoiseimage.py
from __future__ import annotations
from pathlib import Path
import numpy as np
from scipy.ndimage import uniform_filter
from scipy.fft import fft2, fftshift
from tqdm import tqdm
import json


# ------------------------ helpers ------------------------

def _local_std_2d(img: np.ndarray, k: int = 33) -> np.ndarray:
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
        raise ValueError("Data has no variability (IQR=0).")
    data_min, data_max = data.min(), data.max()
    bins = int(np.ceil((data_max - data_min) / bin_width))
    hist, edges = np.histogram(data, bins=bins)
    idx = np.argmax(hist)
    return float((edges[idx] + edges[idx + 1]) / 2)

def estimate_gni_per_slice(ct_array: np.ndarray,
                           hu_min: int = -300,
                           hu_max: int = 300,
                           ksize: int = 33):
    gni_list = []
    std_volume = np.zeros_like(ct_array, dtype=np.float32)
    for i, slice_img in enumerate(tqdm(ct_array, desc="GNI per slice (step6_1)")):
        local_std = _local_std_2d(slice_img, k=ksize)
        std_volume[i] = local_std
        mask = (slice_img >= hu_min) & (slice_img <= hu_max)
        vals = local_std[mask]
        gni = compute_histogram_mode(vals) if vals.size else 0.0
        gni_list.append(gni)
    return np.array(gni_list, dtype=np.float32), std_volume

def compute_uniform_candidate_mask(local_std_array: np.ndarray,
                                   valid_std_mask: np.ndarray,
                                   gni_per_slice: np.ndarray,
                                   tolerance: float = 1.10) -> np.ndarray:
    Z, H, W = local_std_array.shape
    mask = np.zeros((Z, H, W), dtype=bool)
    for i in range(Z):
        thr = gni_per_slice[i] * tolerance
        mask[i] = (local_std_array[i] <= thr) & valid_std_mask[i]
    return mask

def select_nonoverlapping_rois(ct_array: np.ndarray,
                               candidate_mask: np.ndarray,
                               roi_size: int = 33,
                               max_rois: int = 1000,
                               overlap_threshold: float = 0.5,
                               seed: int | None = 42):
    if seed is not None:
        np.random.seed(seed)
    half = roi_size // 2
    Z, H, W = ct_array.shape
    candidates = np.argwhere(candidate_mask)
    np.random.shuffle(candidates)

    final_mask = np.zeros_like(candidate_mask, dtype=bool)
    rois = []
    for z, y, x in tqdm(candidates, desc="Selecting ROIs (step6_1)"):
        if len(rois) >= max_rois:
            break
        if y - half < 0 or y + half >= H or x - half < 0 or x + half >= W:
            continue
        new_mask = np.zeros_like(candidate_mask, dtype=bool)
        new_mask[z, y-half:y+half+1, x-half:x+half+1] = True
        if np.logical_and(final_mask, new_mask).sum() > roi_size * roi_size * overlap_threshold:
            continue
        roi = ct_array[z, y-half:y+half+1, x-half:x+half+1]
        rois.append(roi)
        final_mask |= new_mask
    return np.asarray(rois, dtype=np.float32)

def compute_average_nps_from_rois(rois: np.ndarray,
                                  pixel_spacing: float,
                                  target_shape: tuple[int, int] = (33, 33)) -> np.ndarray:
    N, H0, W0 = rois.shape
    tH, tW = target_shape
    if H0 > tH or W0 > tW:
        raise ValueError(f"ROI ({H0}x{W0}) > target_shape={target_shape}")
    nps_accum = []
    for roi in tqdm(rois, desc="Computing NPS (step6_1)"):
        r = roi.astype(np.float32)
        r -= r.mean()
        F = fft2(r, s=(tH, tW))
        P = np.abs(fftshift(F)) ** 2
        scale = (pixel_spacing * pixel_spacing) / (H0 * W0)
        nps_accum.append(P * scale)
    return np.mean(nps_accum, axis=0)

# ------------------------ pipeline entrypoint ------------------------

def run(cfg) -> None:
    """
    Step 6.1 — NPS analysis on noise image volume.

    Inputs
      - cfg.output_dir             : directory with noise_image_volume.npy (from step5)
      - cfg.output_dir_nps_step6   : directory to store NPS stats

    Optional cfg (defaults shown):
      - hu_min=-300, hu_max=300
      - ksize=33
      - tolerance=1.10
      - roi_size=33
      - max_rois=1000
      - overlap_threshold=0.5
      - seed=42
      - pixel_spacing= from json or cfg yaml
      - NPS grid = in-plane slice shape (H, W) of the noise volume
    """
    out = Path(cfg.output_dir)
    nps_dir = Path(getattr(cfg, "output_dir_nps_step6", out / "NPS_NoiseImagestats"))
    out.mkdir(parents=True, exist_ok=True)
    nps_dir.mkdir(parents=True, exist_ok=True)

    # params
    hu_min = -300
    hu_max = 300
    ksize  = 33
    tolerance = 1.10
    roi_size  = 33
    max_rois  = 1000
    overlap_th= 0.5
    seed      = 42
    # ---- read metadata and pixel spacing ----
    meta_path = out / "original_ct_meta.json"
    meta = None
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            print("[step6_1] WARNING: failed to parse original_ct_meta.json")

    # spacing (prefer from JSON)
    spacing_xyz = meta.get("spacing") if meta is not None else None
    if spacing_xyz is not None and len(spacing_xyz) >= 2:
        sx, sy = float(spacing_xyz[0]), float(spacing_xyz[1])
        if not np.isclose(sx, sy, rtol=0, atol=1e-6):
            print(f"[step6_1] WARNING: anisotropic in-plane spacing (sx={sx} mm, sy={sy} mm). "
                  "Using mean for scalar pixel_spacing.")
        pixel_spacing = float((sx + sy) / 2.0)
    else:
        if hasattr(cfg, "pixel_spacing"):
            pixel_spacing = float(cfg.pixel_spacing)
            print(f"[step6_1] Using pixel spacing from config: {pixel_spacing} mm")
        else:
            raise ValueError(
                "[step6_1] ERROR: Pixel spacing not found in JSON or cfg. "
                "Please specify 'pixel_spacing' in your config.yaml."
            )

    # inputs
    noise_vol_path = out / "noise_image_volume.npy"
    if not noise_vol_path.exists():
        raise FileNotFoundError(f"[step6_1] Missing {noise_vol_path}")
    ct_array = np.load(noise_vol_path)

    # ---- target shape ----
    # The NPS grid must match the in-plane slice shape (H, W), since step6_2 applies it
    # as a frequency-domain filter to full slices. Non-square slices are supported.
    _, H, W = ct_array.shape
    target_shape = (int(H), int(W))
    cfg_target_size = getattr(cfg, "target_size", None)
    if cfg_target_size is not None and (int(cfg_target_size), int(cfg_target_size)) != target_shape:
        print(f"[step6_1] WARNING: ignoring cfg.target_size={cfg_target_size}; "
              f"NPS must match slice shape {target_shape}.")
    print(f"[step6_1] Using NPS target_shape={target_shape} from noise volume.")

    # 6.1.1 GNI + std volume
    gni, std_vol = estimate_gni_per_slice(ct_array, hu_min=hu_min, hu_max=hu_max, ksize=ksize)
    np.save(nps_dir / "NPS_gni.npy", gni)
    np.save(nps_dir / "local_std_volume.npy", std_vol)
    print(f"[step6_1] NPS_gni shape={gni.shape}, local_std_volume shape={std_vol.shape}")

    # 6.1.2 valid mask
    valid_std_mask = (ct_array >= hu_min) & (ct_array <= hu_max)
    np.save(nps_dir / "valid_std_mask.npy", valid_std_mask)

    # 6.1.3 uniform ROI candidate mask
    uniform_mask = compute_uniform_candidate_mask(std_vol, valid_std_mask, gni, tolerance)
    np.save(nps_dir / "uniform_candidate_mask.npy", uniform_mask)

    # 6.1.4 select ROIs
    rois = select_nonoverlapping_rois(ct_array, uniform_mask,
                                      roi_size=roi_size,
                                      max_rois=max_rois,
                                      overlap_threshold=overlap_th,
                                      seed=seed)
    np.save(nps_dir / f"nps_rois_{roi_size}x{roi_size}.npy", rois)
    print(f"[step6_1] Selected {len(rois)} ROIs of size {roi_size}x{roi_size}")

    # 6.1.5 average NPS
    avg_nps = compute_average_nps_from_rois(rois, pixel_spacing=pixel_spacing, target_shape=target_shape)
    np.save(out / "noise_avg_nps.npy", avg_nps)
    print(f"[step6_1] Saved noise_avg_nps.npy")
