# ldct/steps/step4_addnoise.py
from __future__ import annotations
from pathlib import Path
import numpy as np
from tqdm import tqdm

REQ_GEO_KEYS = (
    "num_detectors",
    "det_spacing_px",
    "px_mm",
    "src_to_det_mm",
    "src_to_iso_mm",
)

def _load_bowtie_weights(geo_path: Path, bowtie_strength: float, R_mm: float) -> np.ndarray:
    if not geo_path.exists():
        raise FileNotFoundError(
            f"[step4] Required geometry missing: {geo_path}\n"
            "Place sino_geometry.npz (from step3/geo export) in the output_dir."
        )

    geo = np.load(geo_path)
    missing = [k for k in REQ_GEO_KEYS if k not in geo]
    if missing:
        raise KeyError(f"[step4] Geometry file is missing keys: {missing}")

    det_count = int(geo["num_detectors"])
    det_spacing_px = float(geo["det_spacing_px"])
    px_mm = float(geo["px_mm"])
    src_to_det_mm = float(geo["src_to_det_mm"])
    src_to_iso_mm = float(geo["src_to_iso_mm"])

    # detector lateral coordinate (centered), in mm
    u = np.arange(det_count) - (det_count - 1) / 2.0
    u_mm = u * det_spacing_px * px_mm

    # fan angle and lateral coordinate at isocenter
    alpha = np.arctan2(u_mm, src_to_det_mm)
    x_iso_mm = src_to_iso_mm * np.tan(alpha)

    # simple bowtie thickness proxy: t(x) ~ 1 - cos(|x|/R)
    R_mm = float(R_mm)
    if R_mm <= 0:
        raise ValueError("[step4] bowtie_R_mm must be > 0")

    t_raw = 1.0 - np.cos(np.abs(x_iso_mm) / R_mm)
    t_raw = np.clip(t_raw, 0.0, None)
    t_norm = t_raw / (t_raw.max() + 1e-12)

    bowtie_strength = float(bowtie_strength)
    bowtie_w_det = 1.0 + bowtie_strength * t_norm   # (detectors,)
    return bowtie_w_det.astype(np.float32)

def run(cfg) -> None:
    """
    Step 4 — Generate noise sinograms proportional to the signal,
    **REQUIRING** bowtie geometry. Fails if geometry is absent/invalid.

    cfg:
      output_dir: str
      noise_std_fraction: float (default 0.20)
      noise_seed: int | None (default None)
      save_noisy_sinos: bool (default False)
      bowtie_strength: float (default 1.0)
      bowtie_R_mm: float (default 400.0)
      geometry_filename: str (default "sino_geometry.npz")
    """
    output_dir = Path(cfg.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # inputs
    synthetic_path = output_dir / "synthetic_sinograms.npy"
    if not synthetic_path.exists():
        raise FileNotFoundError(f"[step4] Missing input: {synthetic_path}. Did step3 run?")

    sinos = np.load(synthetic_path)
    print(f"[step4] Loaded synthetic sinograms: {sinos.shape}")  # (Z, A, D)
    Z, A, D = sinos.shape

    # params
    frac = float(getattr(cfg, "noise_std_fraction", 0.20))
    seed = getattr(cfg, "noise_seed", None)
    save_noisy = bool(getattr(cfg, "save_noisy_sinos", False))
    bowtie_strength = float(getattr(cfg, "bowtie_strength", 1.0))
    R_mm = float(getattr(cfg, "bowtie_R_mm", 400.0))
    geo_file = str(getattr(cfg, "geometry_filename", "sino_geometry.npz"))

    rng = np.random.default_rng(int(seed)) if seed is not None else np.random.default_rng()
    eps = 1e-6

    # geometry is REQUIRED
    geo_path = output_dir / geo_file
    bowtie_w_det = _load_bowtie_weights(geo_path, bowtie_strength, R_mm)  # (D,)
    if bowtie_w_det.shape[0] != D:
        raise ValueError(
            f"[step4] Detector count mismatch: sinos D={D} vs geometry {bowtie_w_det.shape[0]}"
        )
    print(f"[step4] Bowtie loaded: shape={bowtie_w_det.shape}, strength={bowtie_strength}, R={R_mm} mm")

    # generate noise
    noise_sinos = np.empty_like(sinos, dtype=np.float32)
    for z in tqdm(range(Z), desc="Generating noise sinograms (step4)"):
        sino = sinos[z].astype(np.float32, copy=False)  # (A, D)
        std_map = np.abs(sino) * frac + eps
        std_map *= bowtie_w_det[np.newaxis, :]          # per-detector shaping
        noise = rng.normal(0.0, 1.0, size=sino.shape).astype(np.float32)
        noise_sinos[z] = noise * std_map

    # save outputs
    noise_path = output_dir / "noise_sinograms.npy"
    np.save(noise_path, noise_sinos)
    print(f"[step4] Saved noise sinograms to: {noise_path}")

    if save_noisy:
        noisy = (sinos.astype(np.float32) + noise_sinos).astype(np.float32)
        out_noisy = output_dir / "synthetic_sinograms_noisy.npy"
        np.save(out_noisy, noisy)
        print(f"[step4] Saved noisy sinograms to: {out_noisy}")
