# ldct/steps/step6_2_filternoise.py
from __future__ import annotations
from pathlib import Path
import numpy as np
from tqdm import tqdm
from scipy.fft import fft2, ifft2, fftshift, ifftshift, fftfreq
import matplotlib.pyplot as plt
import json
# Optional plotting (enabled via cfg.make_figures)



# ---------- Helpers ----------
def freq_radius(shape: tuple[int, int], pixel_spacing_mm: float) -> np.ndarray:
    """Radial spatial frequency (cycles/mm) grid."""
    H, W = shape
    fy = fftshift(fftfreq(H, d=pixel_spacing_mm))
    fx = fftshift(fftfreq(W, d=pixel_spacing_mm))
    Fx, Fy = np.meshgrid(fx, fy, indexing="xy")
    return np.sqrt(Fx**2 + Fy**2)

def weibull_shifted_pdf(f: np.ndarray, s: float, c: float, l: float, eps: float = 1e-12) -> np.ndarray:
    """Shifted Weibull PDF used to model target NPS."""
    s = max(float(s), eps)
    fpos = np.maximum(f - l, 0.0)
    t = fpos / s
    return (c / s) * (t ** (c - 1.0)) * np.exp(-(t ** c))

def build_target_nps(shape: tuple[int, int], A: float, c: float, l: float, s: float, b: float,
                     pixel_spacing_mm: float) -> np.ndarray:
    """Construct target NPS on the same (H, W) grid, in cycles/mm."""
    f = freq_radius(shape, pixel_spacing_mm)
    return A * weibull_shifted_pdf(f, s, c, l) + b

def build_shaping_filter(nps_target: np.ndarray, nps_current: np.ndarray,
                         eps: float = 1e-12, hmax: float = 10.0) -> np.ndarray:
    """
    Frequency-domain shaping filter:
        H = sqrt((target+eps)/(current+eps))
    With clipping (hmax) and variance normalization.
    """
    H = np.sqrt((nps_target + eps) / (nps_current + eps))
    # H = np.clip(H, 0.0, 10)

    # Normalize variance so filtered noise preserves overall power
    # num = float(np.sum(nps_current))
    # den = float(np.sum((H ** 2) * nps_current) + eps)
    # H *= np.sqrt(num / den)
    return H.astype(np.float32, copy=False)

def apply_filter_slice(x: np.ndarray, Hf: np.ndarray) -> np.ndarray:
    """Filter a single 2D slice in the frequency domain (zero-mean before/after)."""
    x = x.astype(np.float32, copy=False)
    x -= x.mean()
    X = fftshift(fft2(x))
    Y = X * Hf
    y = np.real(ifft2(ifftshift(Y)))
    y -= y.mean()
    return y.astype(np.float32, copy=False)


# ---------- Pipeline entrypoint ----------
def run(cfg) -> None:
    """
    Step 6.2 — Shape the noise image volume to match the FG NPS.

    Inputs (in cfg.output_dir):
      - noise_image_volume.npy      (from step5)
      - noise_avg_nps.npy           (from step6_1)
      - fg_nps_weibull_params.npz   (from step2)

    Output (in cfg.output_dir):
      - filtered_noise_volume.npy
      - step6_nps_filtering_comparison.png (optional figure)

    Tunables (optional in cfg):
      - pixel_spacing: from the cfg or json         # mm/pixel
      - hmax: 10.0                   # clip for shaping filter magnitude
      - enforce_zero_mean: True      # zero-mean per-slice after filtering
      - make_figures: True/False     # save comparison figure if matplotlib available
      - fig_vmin: -200, fig_vmax: 200
    """
    out = Path(cfg.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Files
    noise_volume_path = out / "noise_image_volume.npy"
    nps_current_path  = out / "noise_avg_nps.npy"
    params_path       = out / "fg_nps_weibull_params.npz"
    filtered_save_path = out / "filtered_noise_volume.npy"
    fig_path = out / "step6_nps_filtering_comparison.png"

    # Load inputs
    if not noise_volume_path.exists():
        raise FileNotFoundError(f"[step6_2] Missing {noise_volume_path}")
    if not nps_current_path.exists():
        raise FileNotFoundError(f"[step6_2] Missing {nps_current_path} (run step6_1)")
    if not params_path.exists():
        raise FileNotFoundError(f"[step6_2] Missing {params_path} (run step2)")

    noise_volume = np.load(noise_volume_path)
    nps_current  = np.load(nps_current_path).astype(np.float64)
    params = np.load(params_path)
    A, c, l, s, b = (float(params["A"]), float(params["c"]),
                     float(params["l"]), float(params["s"]), float(params["b"]))

    # Ensure 3D (Z,H,W)
    if noise_volume.ndim == 2:
        noise_volume = noise_volume[np.newaxis, ...]
    elif noise_volume.ndim != 3:
        raise ValueError(f"[step6_2] Expected 2D or 3D noise volume, got {noise_volume.shape}")

    Z, H, W = noise_volume.shape
    if nps_current.shape != (H, W):
        raise ValueError(f"[step6_2] NPS_current shape {nps_current.shape} != slice shape {(H, W)}")

    # Params
    hmax = 10.0
    enforce_zero_mean = True
    make_figures = True
    fig_vmin = -200
    fig_vmax = 200
    # ---- pixel spacing: prefer JSON, else cfg ----
    meta_path = out / "original_ct_meta.json"
    spacing_xyz = None

    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            spacing_xyz = meta.get("spacing")  # [sx, sy, sz] in mm
        except json.JSONDecodeError:
            print(f"[step6_2] WARNING: failed to parse {meta_path}; will check cfg.pixel_spacing")

    if spacing_xyz is not None and len(spacing_xyz) >= 2:
        sx, sy = float(spacing_xyz[0]), float(spacing_xyz[1])
        if not np.isclose(sx, sy, rtol=0, atol=1e-6):
            print(f"[step6_2] WARNING: anisotropic in-plane spacing (sx={sx} mm, sy={sy} mm). "
                  "Using mean for scalar pixel_spacing.")
        pixel_spacing = float((sx + sy) / 2.0)
    else:
        if hasattr(cfg, "pixel_spacing"):
            pixel_spacing = float(cfg.pixel_spacing)
            print(f"[step6_2] Using pixel spacing from config: {pixel_spacing} mm")
        else:
            raise ValueError("[step6_2] ERROR: Pixel spacing not found in JSON or cfg. "
                             "Please specify 'pixel_spacing' in your config.yaml.")
    # Target NPS & shaping filter
    nps_target = build_target_nps((H, W), A, c, l, s, b, pixel_spacing_mm=pixel_spacing).astype(np.float64)
    Hf = build_shaping_filter(nps_target, nps_current, hmax=hmax)

    # Filter all slices
    filtered_volume = np.empty_like(noise_volume, dtype=np.float32)
    for i in tqdm(range(Z), desc="Filtering noise slices (step6_2)"):
        filtered_volume[i] = apply_filter_slice(noise_volume[i], Hf)

    if enforce_zero_mean:
        filtered_volume -= filtered_volume.mean(axis=(1, 2), keepdims=True)

    # Save result
    np.save(filtered_save_path, filtered_volume)
    print(f"[step6_2] Saved filtered noise volume: {filtered_save_path}")

    # Optional figure
    if make_figures:
        import matplotlib.pyplot as plt  # safe: guarded above
        mid = Z // 2
        fig, axes = plt.subplots(1, 3, figsize=(15, 5))
        im1 = axes[0].imshow(noise_volume[mid], cmap="gray", vmin=fig_vmin, vmax=fig_vmax)
        axes[0].set_title("Original Noise (Backprojected)"); axes[0].axis("off"); plt.colorbar(im1, ax=axes[0])
        im2 = axes[1].imshow(filtered_volume[mid], cmap="gray", vmin=fig_vmin, vmax=fig_vmax)
        axes[1].set_title("NPS-Filtered Noise"); axes[1].axis("off"); plt.colorbar(im2, ax=axes[1])
        im3 = axes[2].imshow(np.log10(Hf + 1e-10), cmap="hot")
        axes[2].set_title("Shaping Filter H (log10)"); axes[2].axis("off"); plt.colorbar(im3, ax=axes[2])
        plt.tight_layout()
        plt.savefig(fig_path, dpi=150)
        plt.close()
        print(f"[step6_2] Saved comparison figure: {fig_path}")
