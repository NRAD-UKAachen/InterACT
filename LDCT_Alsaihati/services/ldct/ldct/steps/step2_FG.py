# ldct/steps/step2_FG.py
from __future__ import annotations
from pathlib import Path
import numpy as np
from scipy.ndimage import uniform_filter
from scipy.fft import fft2, fftshift, fftfreq
from scipy.optimize import curve_fit
from tqdm import tqdm
import matplotlib.pyplot as plt
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
        raise ValueError("Data has no variability (IQR=0); unable to compute histogram-based mode.")

    data_max = data.max()
    data_min = data.min()
    bins = int(np.ceil((data_max - data_min) / bin_width))
    hist, bin_edges = np.histogram(data, bins=bins)
    max_bin_index = np.argmax(hist)
    return float((bin_edges[max_bin_index] + bin_edges[max_bin_index + 1]) / 2)

def estimate_gni_per_slice(ct_array: np.ndarray, hu_min=-300, hu_max=300, ksize=33):
    """Returns: (gni_per_slice, local_std_volume)."""
    gni_list = []
    std_volume = np.zeros_like(ct_array, dtype=np.float32)
    for i, slice_img in enumerate(tqdm(ct_array, desc="GNI (step2)")):
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

    for z, y, x in tqdm(candidates, desc="Selecting ROIs (step2)"):
        if len(rois) >= max_rois:
            break

        if y - half < 0 or y + half >= H or x - half < 0 or x + half >= W:
            continue

        new_mask = np.zeros_like(candidate_mask, dtype=bool)
        new_mask[z, y - half:y + half + 1, x - half:x + half + 1] = True

        # overlap check
        intersection = np.logical_and(final_mask, new_mask).sum()
        if intersection > roi_size * roi_size * overlap_threshold:
            continue

        roi = ct_array[z, y - half:y + half + 1, x - half:x + half + 1]
        rois.append(roi)
        final_mask |= new_mask

    return np.asarray(rois, dtype=np.float32)

def compute_average_nps_from_rois(rois: np.ndarray,
                                  pixel_spacing: float,
                                  target_size: int = 33) -> np.ndarray:
    """Return avg 2D NPS over ROIs (cycles/mm)."""
    N, H0, W0 = rois.shape
    if H0 > target_size or W0 > target_size:
        raise ValueError(f"ROI ({H0}x{W0}) larger than target_size={target_size}.")

    nps_accum = []
    for roi in tqdm(rois, desc="NPS per ROI (step2)"):
        r = roi.astype(np.float32)
        r -= r.mean()
        F = fft2(r, s=(target_size, target_size))
        P = np.abs(fftshift(F)) ** 2
        scale = (pixel_spacing * pixel_spacing) / (H0 * W0)
        nps_accum.append(P * scale)
    return np.mean(nps_accum, axis=0)

def nps_thresholds_from_2d(nps_2d: np.ndarray,
                           pixel_spacing_mm: float,
                           lo: float = 0.03,
                           hi: float = 0.98,
                           exclude_dc: bool = True):
    H, W = nps_2d.shape
    fx = fftshift(fftfreq(W, d=pixel_spacing_mm))
    fy = fftshift(fftfreq(H, d=pixel_spacing_mm))
    FX, FY = np.meshgrid(fx, fy, indexing="xy")
    fr = np.sqrt(FX**2 + FY**2)
    f_nyq = 1.0 / (2.0 * pixel_spacing_mm)

    dfx = float(abs(fx[1] - fx[0]))
    dfy = float(abs(fy[1] - fy[0]))
    dA = dfx * dfy

    keep = fr <= f_nyq
    if exclude_dc:
        keep &= fr > 0

    fr_flat = fr[keep].ravel()
    nps_flat = nps_2d[keep].ravel()
    order = np.argsort(fr_flat)
    fr_sorted = fr_flat[order]
    nps_sorted = nps_flat[order]

    cum_power = np.cumsum(nps_sorted) * dA
    total_power = cum_power[-1]

    t_lo, t_hi = lo * total_power, hi * total_power
    i_lo = np.searchsorted(cum_power, t_lo, side="left")
    i_hi = np.searchsorted(cum_power, t_hi, side="left")
    f_lo = fr_sorted[min(i_lo, len(fr_sorted) - 1)]
    f_hi = fr_sorted[min(i_hi, len(fr_sorted) - 1)]
    return float(f_lo), float(f_hi)

def freq_radius_grid(target_size: int, pixel_spacing_mm: float) -> np.ndarray:
    fx = fftshift(fftfreq(target_size, d=pixel_spacing_mm))
    fy = fftshift(fftfreq(target_size, d=pixel_spacing_mm))
    FX, FY = np.meshgrid(fx, fy, indexing="xy")
    return np.sqrt(FX**2 + FY**2)

def weibull_model(f, A, c, l, s, b):
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (f - l) / s
        z = np.maximum(z, 0)
        W = (c / s) * np.power(z, c - 1) * np.exp(-np.power(z, c))
        W = np.nan_to_num(W, nan=0.0, posinf=0.0, neginf=0.0)
    return A * W + b


# ------------------------ pipeline entrypoint ------------------------

def run(cfg) -> None:
    """
    Full FG pipeline (step 2):
      - Compute per-slice GNI & local std volume
      - Build soft-tissue mask & uniform-ROI candidate mask
      - Sample non-overlapping 33x33 ROIs
      - Compute average 2D NPS
      - Find low/high frequency thresholds and Weibull fit

    Uses:
      cfg.output_dir                : base artifacts dir (reads original_ct_array.npy & smoothed_gni.npy)
      cfg.output_dir_nps_step2      : stats dir for step 2 outputs

    Optional cfg fields (with defaults shown):
      target_size, fg_exclude_dc=True
    """
    out = Path(cfg.output_dir)
    nps_dir = Path(getattr(cfg, "output_dir_nps_step2", out / "NPS2_step2FGstats"))
    nps_dir.mkdir(parents=True, exist_ok=True)

    # ---- params ----
    hu_min = -300
    hu_max = 300
    ksize = 33
    tolerance = 1.10

    roi_size = 33
    max_rois = 1000
    overlap_threshold = 0.5
    seed = 42

    target_size = getattr(cfg, "fg_target_size", roi_size)

    lo = 0.03
    hi = 0.98
    exclude_dc = getattr(cfg, "fg_exclude_dc", True)

    # ---- inputs ----
    ct_path = out / "original_ct_array.npy"
    meta_path = out / "original_ct_meta.json"
    gni_smoothed_path = out / "smoothed_gni.npy"
    if not ct_path.exists():
        raise FileNotFoundError(f"[step2] Missing input: {ct_path}")
    if not gni_smoothed_path.exists():
        raise FileNotFoundError(f"[step2] Missing input: {gni_smoothed_path}")

    ct_array = np.load(ct_path)
    smoothed_gni = np.load(gni_smoothed_path)

    # ---- read spacing from JSON (if present) ----
    spacing_xyz = None
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            spacing_xyz = meta.get("spacing")  # [sx, sy, sz] in mm
        except json.JSONDecodeError:
            print(f"[step2] WARNING: failed to parse {meta_path}; falling back to cfg.pixel_spacing")

    # pixel spacing: prefer JSON, else cfg
    # pixel spacing: must come from JSON or cfg — no default fallback
    if spacing_xyz is not None and len(spacing_xyz) >= 2:
        sx, sy = float(spacing_xyz[0]), float(spacing_xyz[1])
        if not np.isclose(sx, sy, rtol=0, atol=1e-6):
            print(f"[step2] WARNING: anisotropic in-plane spacing (sx={sx} mm, sy={sy} mm). "
                "Using mean for scalar pixel_spacing. Consider upgrading to separate sx/sy.")
        pixel_spacing = float((sx + sy) / 2.0)

    else:
        # Require pixel spacing from cfg (e.g., config.yaml)
        if hasattr(cfg, "pixel_spacing"):
            pixel_spacing = float(cfg.pixel_spacing)
            print(f"[step2] Using pixel spacing from config: {pixel_spacing} mm")
        else:
            raise ValueError(
                "[step2] ERROR: Pixel spacing not found in JSON or cfg. "
                "Please specify 'pixel_spacing' in your config.yaml."
            )


    # ---- 2.1: GNI & local std volume ----
    NPS_gni, std_volume = estimate_gni_per_slice(ct_array, hu_min=hu_min, hu_max=hu_max, ksize=ksize)
    np.save(nps_dir / "NPS_gni.npy", NPS_gni)
    np.save(nps_dir / "local_std_volume.npy", std_volume)
    print(f"[step2] NPS_gni shape: {NPS_gni.shape}; local_std_volume shape: {std_volume.shape}")

    # Save valid std mask for soft tissue
    valid_std_mask = (ct_array >= hu_min) & (ct_array <= hu_max)
    np.save(nps_dir / "valid_std_mask.npy", valid_std_mask)
    print("[step2] Saved valid_std_mask.npy")

    # ---- 2.2: Uniform ROI candidate mask ----
    uniform_candidate_mask = compute_uniform_candidate_mask(
        std_volume, valid_std_mask, smoothed_gni, tolerance=tolerance
    )
    np.save(nps_dir / "uniform_candidate_mask.npy", uniform_candidate_mask)
    print("[step2] Saved uniform_candidate_mask.npy")

    # ---- 2.3: Select ROIs ----
    rois = select_nonoverlapping_rois(
        ct_array=ct_array,
        candidate_mask=uniform_candidate_mask,
        roi_size=roi_size,
        max_rois=max_rois,
        overlap_threshold=overlap_threshold,
        seed=seed,
    )
    np.save(nps_dir / f"nps_rois_{roi_size}x{roi_size}.npy", rois)
    print(f"[step2] Selected {len(rois)} ROIs of size {roi_size}x{roi_size}")

    # ---- 2.4: Average NPS from ROIs ----
    avg_nps = compute_average_nps_from_rois(rois, pixel_spacing=pixel_spacing, target_size=target_size)
    np.save(out / "avg_nps_FG.npy", avg_nps)  # keep original script location
    print("[step2] Saved avg_nps_FG.npy")

    # ---- 2.5: Thresholds (frequency power CDF percentiles) ----
    f_lo, f_hi = nps_thresholds_from_2d(avg_nps, pixel_spacing_mm=pixel_spacing, lo=lo, hi=hi, exclude_dc=exclude_dc)
    np.savez(nps_dir / "fg_nps_thresholds.npz", f_min=f_lo, f_max=f_hi)
    print(f"[step2] Thresholds: f_min={f_lo:.4f}, f_max={f_hi:.4f} cy/mm (saved)")

    # ---- 2.6: Frequency-radius grid for NPS ----
    radii = freq_radius_grid(avg_nps.shape[0], pixel_spacing_mm=pixel_spacing)
    np.save(nps_dir / "fg_radii_per_NPS.npy", radii)

    # ---- 2.7: Weibull fit on band-limited data ----
    fr_flat = radii.ravel()
    nps_flat = avg_nps.ravel()
    mask = (nps_flat > 0) & np.isfinite(nps_flat) & np.isfinite(fr_flat) & (fr_flat > 0)
    fr_flat = fr_flat[mask]
    nps_flat = nps_flat[mask]
    sel = (fr_flat >= f_lo) & (fr_flat <= f_hi)
    fr_flat = fr_flat[sel]
    nps_flat = nps_flat[sel]

    # rmin, rmax = float(np.min(fr_flat)), float(np.max(fr_flat))
    # A0 = float(np.max(nps_flat))
    # c0, l0 = 1.0, max(rmin - 0.1 * (rmax - rmin), rmin - 1.0)
    # s0 = max((rmax - rmin) / 4.0, 1e-3)
    # b0 = max(float(np.min(nps_flat)), 0.0)
    # initial = [A0, c0, l0, s0, b0]
    # lower = [0.0, 0.2, rmin - 5.0, 1e-6, 0.0]
    # upper = [1e12, 5.0, rmax - 1e-6, (rmax - rmin) * 5.0, 1e12]

    # popt, _ = curve_fit(weibull_model, fr_flat, nps_flat,
    #                     p0=initial, bounds=(lower, upper), maxfev=20000)
    
        # --- smarter initial guesses + tiny multi-start ---
    rmin, rmax = float(np.min(fr_flat)), float(np.max(fr_flat))
    f_span = rmax - rmin
    eps = np.median(np.diff(np.unique(np.sort(fr_flat)))) if fr_flat.size > 1 else 1e-3

    # robust baseline from the lowest 15% of y
    y_sorted = np.sort(nps_flat)
    b0 = float(np.median(y_sorted[:max(5, int(0.15 * len(y_sorted)))]))
    b0 = max(b0, 0.0)

    # amplitude from the 95th percentile above the floor
    A0_base = float(np.quantile(nps_flat, 0.95) - b0)
    A0_base = max(A0_base, np.finfo(float).eps)

    # put left shift just left of the band
    l0_base = max(f_lo - 2 * eps, rmin - 0.5 * f_span)

    def s_from_mode(f_mode, l0, c0):
        if c0 <= 1.0:
            return max(0.25 * f_span, 1e-6)
        ratio = ((c0 - 1.0) / c0) ** (1.0 / c0)
        s = (f_mode - l0) / max(ratio, 1e-6)
        return float(max(s, 1e-6))

    # we want a monotonic rise across [f_lo, f_hi] → push the Weibull mode to the right of the band
    c0_list = [1.5, 2.0, 2.5]
    l0_list = [l0_base, f_lo - eps, f_lo]
    fmode_factor = [1.15, 1.30, 1.50]  # f_mode ≈ k * rmax

    initial_sets = []
    for c0 in c0_list:
        for l0 in l0_list:
            for k in fmode_factor:
                f_mode = min(k * rmax, rmax + 0.5 * f_span)
                s0 = s_from_mode(f_mode, l0, c0)
                initial_sets.append([A0_base, c0, l0, s0, b0])

    lower = [0.0, 1.0, rmin - 5.0, 1e-6, 0.0]
    upper = [1e12, 5.0, rmax - 1e-6, 5.0 * f_span, 1e12]

    # try each start; keep the best by residual sum of squares
    best, best_rss = None, np.inf
    for init in initial_sets:
        try:
            p, _ = curve_fit(weibull_model, fr_flat, nps_flat,
                            p0=init, bounds=(lower, upper), maxfev=20000)
            resid = nps_flat - weibull_model(fr_flat, *p)
            rss = float(np.sum(resid ** 2))
            if rss < best_rss:
                best, best_rss = p, rss
        except Exception:
            pass

    if best is None:  # fallback (very unlikely)
        best = initial_sets[0]

    popt = best
    A_fit, c_fit, l_fit, s_fit, b_fit = popt



    #A_fit, c_fit, l_fit, s_fit, b_fit = popt
    print(f"[step2] Weibull params: A={A_fit:.3e}, c={c_fit:.3f}, l={l_fit:.4f}, s={s_fit:.4f}, b={b_fit:.3e}")

    # plot
    f_plot = np.linspace(np.min(fr_flat), np.max(fr_flat), 500)
    nps_fit = weibull_model(f_plot, *popt)
    plt.figure(figsize=(8, 5))
    plt.scatter(fr_flat, nps_flat, s=1, alpha=0.3, label="Measured NPS")
    plt.plot(f_plot, nps_fit, linewidth=2, label="Weibull fit")
    plt.xlabel("Spatial frequency (cycles/mm)")
    plt.ylabel("NPS")
    plt.title("FG NPS vs Spatial Frequency (Weibull fit)")
    plt.grid(True)
    plt.axvline(f_lo, linestyle="--", label="f_min")
    plt.axvline(f_hi, linestyle="--", label="f_max")
    plt.legend()
    plt.tight_layout()
    plt.savefig(out / "fg_NPS_Weibull_Fit.png")

    # save params
    np.savez(out / "fg_nps_weibull_params.npz", A=A_fit, c=c_fit, l=l_fit, s=s_fit, b=b_fit)
    print("[step2] Saved fg_NPS_Weibull_Fit.png and fg_nps_weibull_params.npz")