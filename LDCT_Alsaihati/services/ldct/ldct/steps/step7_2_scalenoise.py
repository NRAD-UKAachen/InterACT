# ldct/steps/step7_2_scalenoise.py
from __future__ import annotations
from pathlib import Path
import numpy as np

def _scale_const(f: float, beta: float) -> float:
    """
    scale_factor_const = sqrt(f^(-2*beta) - 1)
    Valid for 0 < f <= 1 and beta >= 0, and f^(-2*beta) - 1 > 0.
    """
    if not (0.0 < f <= 1.0):
        raise ValueError(f"[step7_2] f must be in (0,1], got {f}")
    if beta < 0.0:
        raise ValueError(f"[step7_2] beta must be >= 0, got {beta}")
    val = (f ** (-2.0 * beta)) - 1.0
    if val <= 0.0:
        raise ValueError(f"[step7_2] f^(-2*beta) - 1 must be > 0, got {val} for f={f}, beta={beta}")
    return float(np.sqrt(val))

def run(cfg) -> None:
    """
    Step 7.2 — Scale filtered noise per slice to match target dose reduction.

    Inputs (in cfg.output_dir):
      - smoothed_gni.npy            # σ_in(k) from step1
      - NoiseImage_gni.npy          # σ_N,raw(k) from step7_1
      - original_ct_array.npy       # to get (S,H,W)
      - filtered_noise_volume.npy   # noise to be scaled (from step6_2)

    Outputs (in cfg.output_dir):
      - scaling_factors.npy         # per-slice scale factors (S,)
      - scaled_noise_volume.npy     # scaled noise volume (S,H,W)

    Tunables (from cfg, defaults shown):
      - f: 0.25                     # dose ratio
      - beta: 0.5                   # noise–dose exponent
      - nan_policy: "zero"          # handling when σ_N,raw(k)=0 -> {"zero","nan","raise"}
    """
    out = Path(cfg.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ---- load inputs ----
    sigma_in_path   = out / "smoothed_gni.npy"
    sigma_raw_path  = out / "NoiseImage_gni.npy"
    ct_path         = out / "original_ct_array.npy"
    noise_path_in   = out / "filtered_noise_volume.npy"

    for p in [sigma_in_path, sigma_raw_path, ct_path, noise_path_in]:
        if not p.exists():
            raise FileNotFoundError(f"[step7_2] Missing input: {p}")

    sigma_in  = np.load(sigma_in_path).astype(np.float32)     # (S,)
    sigma_raw = np.load(sigma_raw_path).astype(np.float32)    # (S,)
    ct        = np.load(ct_path)                               # (S,H,W)
    noise_in  = np.load(noise_path_in).astype(np.float32)      # (S,H,W)

    # shape checks
    S = ct.shape[0]
    if sigma_in.shape[0] != S:
        raise ValueError(f"[step7_2] smoothed_gni length {sigma_in.shape[0]} != slices {S}")
    if sigma_raw.shape[0] != S:
        raise ValueError(f"[step7_2] NoiseImage_gni length {sigma_raw.shape[0]} != slices {S}")
    if noise_in.shape != ct.shape:
        raise ValueError(f"[step7_2] filtered_noise_volume shape {noise_in.shape} != CT shape {ct.shape}")

    # ---- parameters ----
    f = float(getattr(cfg, "f", 0.25))       # your earlier default
    beta = float(getattr(cfg, "beta", 0.5))
    nan_policy = str(getattr(cfg, "nan_policy", "zero")).lower()

    k = _scale_const(f, beta)  # global scale constant

    # ---- per-slice scales ----
    # scales[k] = k * (σ_in(k) / σ_N,raw(k))
    den_zero = (sigma_raw <= 0) | ~np.isfinite(sigma_raw)
    if den_zero.any():
        msg = f"[step7_2] {int(den_zero.sum())}/{S} slices have σ_N,raw<=0 or non-finite."
        if nan_policy == "raise":
            raise ValueError(msg)
        elif nan_policy == "nan":
            print(msg + " Setting scales to NaN for those slices.")
        else:
            print(msg + " Setting scales to 0 for those slices.")
    scales = np.empty(S, dtype=np.float32)
    np.divide(sigma_in, sigma_raw, out=scales, where=~den_zero)
    scales[den_zero] = 0.0 if nan_policy == "zero" else np.nan
    scales *= k

    # ---- apply scaling (broadcast over H,W) ----
    scaled_noise = (scales[:, None, None] * noise_in).astype(np.float32)

    # ---- save outputs ----
    scales_out = out / "scaling_factors.npy"
    noise_out  = out / "scaled_noise_volume.npy"
    np.save(scales_out, scales)
    np.save(noise_out, scaled_noise)

    # ---- logging ----
    preview = np.array2string(scales[:10], precision=4, separator=", ")
    print(f"[step7_2] f={f}, beta={beta}, scale_const={k:.6f}")
    print(f"[step7_2] Saved per-slice scaling factors -> {scales_out} (first 10: {preview})")
    print(f"[step7_2] Saved scaled noise volume -> {noise_out}; shape={scaled_noise.shape}")
