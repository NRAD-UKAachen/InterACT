# ldct/steps/step3_fp.py
from __future__ import annotations
from pathlib import Path
import numpy as np
import SimpleITK as sitk
import pydicom
from tqdm import tqdm
import astra
import os

# μ (mm^-1)
MU_WATER = 0.0160031664
MU_AIR   = 0.0000218796

def convert_hu_to_mu(ct_array: np.ndarray,
                     mu_water: float = MU_WATER,
                     mu_air: float = MU_AIR) -> np.ndarray:
    return mu_air + (mu_water - mu_air) * ((ct_array / 1000.0) + 1.0)

def _read_first_dicom(dicom_folder: Path):
    # sitk to list + sanity check the series
    reader = sitk.ImageSeriesReader()
    file_names = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(str(dicom_folder))
    if not file_names:
        raise FileNotFoundError(f"No DICOM files found under: {dicom_folder}")
    reader.SetFileNames(file_names)
    _ = reader.Execute()  # verifies readability
    return pydicom.dcmread(file_names[0])

def _build_astra_geometry(ds,
                          img_height: int,
                          img_width: int,
                          det_count: int,
                          num_projections: int):
    # Geometry from DICOM (mm)
    src_to_iso = float(ds[0x0018, 0x1111].value)  # Distance Source to Patient
    src_to_det = float(ds[0x0018, 0x1110].value)  # Distance Source to Detector
    det_diam   = float(ds[0x0018, 0x0090].value)  # Data Collection Diameter
    px_y, px_x = map(float, ds.PixelSpacing)

    if abs(px_x - px_y) >= 1e-6:
        raise ValueError(f"Non-square pixels detected ({px_x}×{px_y} mm).")

    px = px_x  # mm/pixel

    # Scale to "pixel units" for ASTRA
    src_to_iso_px   = src_to_iso / px
    src_to_det_px   = src_to_det / px
    origin_to_det_px= src_to_det_px - src_to_iso_px
    det_spacing_px  = (det_diam / det_count) / px

    # Projection angles (paper doesn't specify if 180° or 360° - currently using 180°)
    angle_vals = np.linspace(0.0, np.pi, int(num_projections), endpoint=False).astype(np.float32)

    # Package all needed values
    geom = {
        "img_height": int(img_height),
        "img_width":  int(img_width),
        "det_count":  int(det_count),
        "det_spacing_px": float(det_spacing_px),
        "angles": angle_vals,
        "src_to_iso_px": float(src_to_iso_px),
        "src_to_det_px": float(src_to_det_px),
        "origin_to_det_px": float(origin_to_det_px),
        # helpful meta
        "px_mm": float(px),
        "src_to_iso_mm": float(src_to_iso),
        "src_to_det_mm": float(src_to_det),
        "det_diameter_mm": float(det_diam),
        "num_projections": int(num_projections),
    }
    return geom

def _forward_project_slice(mu_slice: np.ndarray, geom: dict, use_gpu: bool) -> np.ndarray:
    H, W = mu_slice.shape
    vol_geom = astra.create_vol_geom(H, W)  # ASTRA expects (rows, cols)
    proj_geom = astra.create_proj_geom(
        "fanflat",
        geom["det_spacing_px"],
        geom["det_count"],
        geom["angles"],
        geom["src_to_iso_px"],
        geom["origin_to_det_px"],
    )

    sino_id = astra.data2d.create("-sino", proj_geom)
    vol_id  = astra.data2d.create("-vol", vol_geom, mu_slice.astype(np.float32))

    algo_name = "FP_CUDA" if use_gpu else "FP"
    cfg = astra.astra_dict(algo_name)
    cfg["ProjectionDataId"] = sino_id
    cfg["VolumeDataId"] = vol_id

    # only create projector for CPU mode
    if not use_gpu:
        projector_id = astra.create_projector("line_fanflat", proj_geom, vol_geom)
        cfg["ProjectorId"] = projector_id

    alg_id = astra.algorithm.create(cfg)
    astra.algorithm.run(alg_id)

    sinogram = astra.data2d.get(sino_id)

    astra.algorithm.delete(alg_id)
    astra.data2d.delete(sino_id)
    astra.data2d.delete(vol_id)
    if not use_gpu:
        astra.projector.delete(projector_id)

    return sinogram

def _select_backend(use_gpu_flag: bool, cfg) -> tuple[bool, int, str]:
    gpu_index = int(getattr(cfg, "gpu_index", 0))

    # Is the CUDA algorithm compiled in?
    try:
        _ = astra.astra_dict("FP_CUDA")
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
        msg = f"Using GPU (FP_CUDA) on device index {gpu_index}. Diagnostics: {dbg}"
    else:
        reasons = []
        if use_gpu_flag and not cuda_algo_present:
            reasons.append("FP_CUDA not present in this ASTRA build")
        if use_gpu_flag and cuda_algo_present and not has_runtime:
            reasons.append("CUDA runtime not available (driver/toolkit)")
        because = f" (fallback: {', '.join(reasons)})" if reasons else ""
        msg = f"Using CPU (FP){because}. Diagnostics: {dbg}"

    return actual_use_gpu, gpu_index, msg



def run(cfg) -> None:
    """
    Step 3 — Forward projection (fan-beam) using ASTRA.

    Inputs (expected in cfg):
      - cfg.dicom_folder : path to DICOM series (for geometry)
      - cfg.output_dir   : path where step0 saved original_ct_array.npy

    Optional cfg (defaults shown):
      - det_count = 700           # detector elements (Alsaihati et al. 2024)
      # - num_projections = 984   # old default
      - num_projections = 360     # projection angles (Alsaihati et al. 2024)
      - mu_water = 0.0160031664
      - mu_air   = 0.0000218796
    """
    output_dir = Path(cfg.output_dir)
    dicom_folder = Path(cfg.dicom_folder)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Get logger if available
    logger = getattr(cfg, 'logger', None)

    use_gpu_flag = getattr(cfg, "use_gpu", False)
    actual_use_gpu, gpu_index, backend_msg = _select_backend(use_gpu_flag, cfg)
    if logger:
        logger.info(f"[step3] {backend_msg}")
    else:
        print(f"[step3] {backend_msg}")

    det_count = int(getattr(cfg, "det_count", 700))
    # num_projections = int(getattr(cfg, "num_projections", 984))
    num_projections = int(getattr(cfg, "num_projections", 360))  # Alsaihati et al. 2024
    mu_water = float(getattr(cfg, "mu_water", MU_WATER))
    mu_air   = float(getattr(cfg, "mu_air", MU_AIR))

    # Load CT volume from step0
    ct_path = output_dir / "original_ct_array.npy"
    if not ct_path.exists():
        raise FileNotFoundError(f"[step3] Missing input: {ct_path}. Did step0 run?")
    ct_array = np.load(ct_path)
    img_height, img_width = int(ct_array.shape[1]), int(ct_array.shape[2])

    # Read DICOM & build ASTRA geometry
    ds = _read_first_dicom(dicom_folder)
    geom = _build_astra_geometry(ds, img_height, img_width, det_count, num_projections)

    # Persist geometry (as in your original script)
    geo_path = output_dir / "sino_geometry.npz"
    np.savez(
        geo_path,
        angles=geom["angles"],
        det_spacing_px=np.float32(geom["det_spacing_px"]),
        src_to_iso_px=np.float32(geom["src_to_iso_px"]),
        src_to_det_px=np.float32(geom["src_to_det_px"]),
        origin_to_det_px=np.float32(geom["origin_to_det_px"]),
        num_detectors=np.int32(geom["det_count"]),
        img_height=np.int32(geom["img_height"]),
        img_width=np.int32(geom["img_width"]),
        px_mm=np.float32(geom["px_mm"]),
        src_to_iso_mm=np.float32(geom["src_to_iso_mm"]),
        src_to_det_mm=np.float32(geom["src_to_det_mm"]),
        det_diameter_mm=np.float32(geom["det_diameter_mm"]),
        num_projections=np.int32(geom["num_projections"]),
        mu_water=np.float32(mu_water),
        mu_air=np.float32(mu_air),
    )
    if logger:
        logger.info(f"[step3] Saved projection geometry to: {geo_path}")
    else:
        print(f"[step3] Saved projection geometry to: {geo_path}")

    # Convert HU→μ using (possibly) overridden coefficients
    mu_array = mu_air + (mu_water - mu_air) * ((ct_array / 1000.0) + 1.0)

    # Forward project each slice
    sinos = []
    for mu_slice in tqdm(mu_array, desc=f"Forward projecting slices (step3) [{ 'GPU' if actual_use_gpu else 'CPU' }]"):
        sino = _forward_project_slice(mu_slice, geom, actual_use_gpu)
        sinos.append(sino)

    sinos = np.asarray(sinos, dtype=np.float32)

    # PROPOSED CORRECTION: apply bowtie attenuation to the signal here (physically correct).
    # This replaces the bowtie scaling in step4. In step4, remove the bowtie from std_map.
    #
    # bowtie_strength = float(getattr(cfg, "bowtie_strength", 1.0))
    # bowtie_R_mm     = float(getattr(cfg, "bowtie_R_mm", 400.0))
    #
    # det_count      = geom["det_count"]
    # det_spacing_px = geom["det_spacing_px"]
    # px_mm          = geom["px_mm"]
    # src_to_det_mm  = geom["src_to_det_mm"]
    # src_to_iso_mm  = geom["src_to_iso_mm"]
    #
    # u      = np.arange(det_count) - (det_count - 1) / 2.0
    # u_mm   = u * det_spacing_px * px_mm
    # alpha  = np.arctan2(u_mm, src_to_det_mm)
    # x_iso  = src_to_iso_mm * np.tan(alpha)
    # t_raw  = np.clip(1.0 - np.cos(np.abs(x_iso) / bowtie_R_mm), 0.0, None)
    # t_norm = t_raw / (t_raw.max() + 1e-12)
    # bowtie_attenuation = (1.0 / (1.0 + bowtie_strength * t_norm)).astype(np.float32)  # (D,) <= 1
    #
    # sinos *= bowtie_attenuation[np.newaxis, np.newaxis, :]  # attenuate signal, edges weaker

    out_path = output_dir / "synthetic_sinograms.npy"
    np.save(out_path, sinos)
    if logger:
        logger.info(f"[step3] Saved synthetic sinograms to: {out_path}")
    else:
        print(f"[step3] Saved synthetic sinograms to: {out_path}")

#required:
#output_dir
#dicom_folder

#Optional:
# use_gpu, det_count, num_projections, mu_water, mu_air