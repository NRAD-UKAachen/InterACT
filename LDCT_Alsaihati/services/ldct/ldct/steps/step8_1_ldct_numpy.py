# ldct/steps/step8_1_ldct_numpy.py
from __future__ import annotations
from pathlib import Path
import numpy as np


def run(cfg) -> None:
    """
    Step 8.1 — Form the simulated low-dose CT by adding the scaled noise.

    Inputs (in cfg.output_dir):
      - original_ct_array.npy        # (S,H,W)
      - scaled_noise_volume.npy      # (S,H,W) from step7_2

    Output (in cfg.output_dir):
      - <ldct_numpy_filename>        # default: ldct_sim_HD_D45.npy

    Tunables (optional in cfg):
      - ldct_numpy_filename: "ldct_sim_HD_D45.npy"
      - ldct_dtype: "float32"        # output dtype ("float32" or "float64")
      - clip_hu_min: None            # if set, clip result to [clip_hu_min, clip_hu_max]
      - clip_hu_max: None
    """
    out = Path(cfg.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    ct_path    = out / "original_ct_array.npy"
    noise_path = out / "scaled_noise_volume.npy"

    if not ct_path.exists():
        raise FileNotFoundError(f"[step8_1] Missing input: {ct_path}")
    if not noise_path.exists():
        raise FileNotFoundError(f"[step8_1] Missing input: {noise_path}")

    # Load arrays
    ct = np.load(ct_path)                       # (S,H,W), HU
    noise = np.load(noise_path).astype(np.float32, copy=False)

    # Shape checks
    if ct.shape != noise.shape:
        raise ValueError(f"[step8_1] Shape mismatch: CT {ct.shape} vs noise {noise.shape}")

    # Output dtype & filename
    out_dtype_str = str(getattr(cfg, "ldct_dtype", "float32")).lower()
    out_dtype = np.float32 if out_dtype_str == "float32" else np.float64
    out_name = getattr(cfg, "ldct_numpy_filename", "ldct_sim_HD_D45.npy")
    ldct_path = out / out_name

    # Compute LDCT = CT + scaled noise
    # Keep precision stable; cast CT to target dtype once.
    ct_cast = ct.astype(out_dtype, copy=False)
    ldct = np.empty_like(ct_cast, dtype=out_dtype)
    np.add(ct_cast, noise.astype(out_dtype, copy=False), out=ldct)

    # Optional clipping in HU (if requested)
    clip_min = None
    clip_max = None
    if clip_min is not None or clip_max is not None:
        np.clip(ldct,
                clip_min if clip_min is not None else ldct.min(),
                clip_max if clip_max is not None else ldct.max(),
                out=ldct)

    # Save
    np.save(ldct_path, ldct)
    print(f"[step8_1] ✅ Simulated low-dose CT saved: {ldct_path}")
    print(f"[step8_1] Shape: {ldct.shape}, dtype: {ldct.dtype}")
