# ldct/steps/step5_bp.py
from __future__ import annotations
from pathlib import Path
import numpy as np
import astra
from tqdm import tqdm
import os

def _select_backend(use_gpu_flag: bool, cfg) -> tuple[bool, int, str]:
    gpu_index = int(getattr(cfg, "gpu_index", 0))

    # Is the CUDA algorithm compiled in?
    try:
        _ = astra.astra_dict("BP_CUDA")
        cuda_algo_present = True
    except Exception:
        cuda_algo_present = False

    # Is a CUDA runtime actually available?
    try:
        has_runtime = bool(getattr(astra, "use_cuda", lambda: False)())
    except Exception:
        has_runtime = False

    actual_use_gpu = bool(use_gpu_flag and cuda_algo_present and has_runtime)

    dbg = {
        "use_gpu_flag": use_gpu_flag,
        "cuda_algo_present": cuda_algo_present,
        "cuda_runtime": has_runtime,
        "gpu_index": gpu_index,
        "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "<unset>"),
    }

    if actual_use_gpu:
        try:
            astra.set_gpu_index(gpu_index)
        except Exception as e:
            dbg["set_gpu_index_error"] = str(e)
        msg = f"Using GPU (BP_CUDA) on device index {gpu_index}. Diagnostics: {dbg}"
    else:
        reasons = []
        if use_gpu_flag and not cuda_algo_present:
            reasons.append("BP_CUDA not present in this ASTRA build")
        if use_gpu_flag and cuda_algo_present and not has_runtime:
            reasons.append("CUDA runtime not available (driver/toolkit)")
        because = f" (fallback: {', '.join(reasons)})" if reasons else ""
        msg = f"Using CPU (BP){because}. Diagnostics: {dbg}"

    return actual_use_gpu, gpu_index, msg


def backproject_noise_slice(noisy_sino: np.ndarray,
                            proj_geom,
                            vol_geom,
                            use_gpu: bool,
                            projector_id: int | None = None) -> np.ndarray:
    """
    Unfiltered backprojection of a single sinogram (angles × detectors) -> image (H × W).
    Uses BP_CUDA if use_gpu=True, else BP with line_fanflat projector.
    """
    sino_id = astra.data2d.create("-sino", proj_geom, noisy_sino.astype(np.float32))
    vol_id  = astra.data2d.create("-vol",  vol_geom)

    algo_name = "BP_CUDA" if use_gpu else "BP"
    cfg = astra.astra_dict(algo_name)
    cfg["ProjectionDataId"]     = sino_id
    cfg["ReconstructionDataId"] = vol_id
    if not use_gpu:
        # Only CPU BP requires a projector
        if projector_id is None:
            raise ValueError("CPU BP requires a valid projector_id.")
        cfg["ProjectorId"] = projector_id

    alg_id = astra.algorithm.create(cfg)
    astra.algorithm.run(alg_id)

    img = astra.data2d.get(vol_id).astype(np.float32)

    # cleanup
    astra.algorithm.delete(alg_id)
    astra.data2d.delete(sino_id)
    astra.data2d.delete(vol_id)

    return img


def run(cfg) -> None:
    """
    Step 5 — Backproject noise sinograms into a 3D noise image volume.

    Inputs (expected in cfg.output_dir):
      - sino_geometry.npz    (from step3)
      - noise_sinograms.npy  (from step4)

    Output:
      - noise_image_volume.npy

    Optional cfg:
      - use_gpu (bool, default False)
      - gpu_index (int, default 0)
    """
    output_dir = Path(cfg.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Get logger if available
    logger = getattr(cfg, 'logger', None)

    use_gpu_flag = bool(getattr(cfg, "use_gpu", False))
    actual_use_gpu, gpu_index, backend_msg = _select_backend(use_gpu_flag, cfg)
    if logger:
        logger.info(f"[step5] {backend_msg}")
    else:
        print(f"[step5] {backend_msg}")

    geo_path  = output_dir / "sino_geometry.npz"
    sino_path = output_dir / "noise_sinograms.npy"
    out_path  = output_dir / "noise_image_volume.npy"

    if not geo_path.exists():
        raise FileNotFoundError(f"[step5] Missing geometry: {geo_path}")
    if not sino_path.exists():
        raise FileNotFoundError(f"[step5] Missing sinograms: {sino_path}")

    # ---- load sinograms ----
    noise_sinos = np.load(sino_path)  # (S, A, D)
    num_slices, num_angles_sino, num_detectors_sino = noise_sinos.shape
    if logger:
        logger.info(f"[step5] Loaded noise sinograms: {noise_sinos.shape}")
    else:
        print(f"[step5] Loaded noise sinograms: {noise_sinos.shape}")

    # ---- load geometry ----
    geo = np.load(geo_path)
    angles           = geo["angles"]                # (A,)
    det_spacing_px   = float(geo["det_spacing_px"])
    src_to_iso_px    = float(geo["src_to_iso_px"])
    origin_to_det_px = float(geo["origin_to_det_px"])
    num_detectors    = int(geo["num_detectors"])
    img_height       = int(geo["img_height"])
    img_width        = int(geo["img_width"])

    # sanity checks
    assert num_detectors == num_detectors_sino, \
        f"Detector bins mismatch: sino={num_detectors_sino}, geo={num_detectors}"
    assert angles.shape[0] == num_angles_sino, \
        f"Angles count mismatch: sino={num_angles_sino}, geo={angles.shape[0]}"

    # ---- ASTRA geometries ----
    proj_geom = astra.create_proj_geom(
        "fanflat",
        det_spacing_px,
        num_detectors,
        angles,
        src_to_iso_px,
        origin_to_det_px,
    )
    vol_geom = astra.create_vol_geom(img_height, img_width)  # ASTRA expects (rows, cols)

    # Projector only needed for CPU
    projector_id = None
    if not actual_use_gpu:
        projector_id = astra.create_projector("line_fanflat", proj_geom, vol_geom)

    # ---- backproject all slices ----
    noise_volume = np.empty((num_slices, img_height, img_width), dtype=np.float32)
    desc = f"Back-projecting noise sinograms (step5) [{'GPU' if actual_use_gpu else 'CPU'}]"
    for idx, noisy_sino in enumerate(tqdm(noise_sinos, desc=desc)):
        noise_volume[idx] = backproject_noise_slice(
            noisy_sino,
            proj_geom,
            vol_geom,
            use_gpu=actual_use_gpu,
            projector_id=projector_id,
        )

    if projector_id is not None:
        astra.projector.delete(projector_id)

    # save result
    np.save(out_path, noise_volume)
    if logger:
        logger.info(f"[step5] Noise image volume saved: {noise_volume.shape}")
        logger.info(f"[step5] Saved to: {out_path}")
    else:
        print(f"[step5] Noise image volume saved: {noise_volume.shape}")
        print(f"[step5] Saved to: {out_path}")
