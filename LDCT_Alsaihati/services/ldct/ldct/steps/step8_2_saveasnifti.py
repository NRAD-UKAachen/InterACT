# ldct/steps/step8_2_saveasnifti.py
from __future__ import annotations
from pathlib import Path
import numpy as np
import SimpleITK as sitk


def _derive_nifti_name(npy_name: str) -> str:
    p = Path(npy_name)
    return (p.with_suffix("").name + ".nii.gz")


def run(cfg) -> None:
    """
    Step 8.2 — Save the simulated LDCT volume as NIfTI, copying geometry from the reference DICOM.

    Inputs
      - cfg.output_dir        : directory containing the LDCT .npy file
      - cfg.dicom_folder      : DICOM series folder to copy geometry (spacing/origin/direction)

    Defaults / Tunables (optional in cfg)
      - ldct_numpy_filename   : "ldct_sim_HD_D45.npy"
      - ldct_nifti_filename   : (derived from numpy filename, e.g. "ldct_sim_HD_D45.nii.gz")
      - nifti_compress        : True
      - nifti_dtype           : "float32"  # or "int16" (simple cast; no rescaling)
    """
    out = Path(cfg.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    dicom_folder = Path(cfg.dicom_folder)

    npy_name = getattr(cfg, "ldct_numpy_filename", "ldct_sim_HD_D45.npy")
    nifti_name = getattr(cfg, "ldct_nifti_filename", _derive_nifti_name(npy_name))
    compress = bool(getattr(cfg, "nifti_compress", True))
    nifti_dtype = str(getattr(cfg, "nifti_dtype", "float32")).lower()

    npy_path = out / npy_name
    nifti_path = out / nifti_name

    if not npy_path.exists():
        raise FileNotFoundError(f"[step8_2] Missing LDCT numpy volume: {npy_path}")
    if not dicom_folder.exists():
        raise FileNotFoundError(f"[step8_2] DICOM folder not found: {dicom_folder}")

    # ---- load simulated volume (Z, H, W) ----
    vol = np.load(npy_path).astype(np.float32, copy=False)

    # ---- read reference DICOM series for geometry ----
    reader = sitk.ImageSeriesReader()
    series_files = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(str(dicom_folder))
    if not series_files:
        raise FileNotFoundError(f"[step8_2] No DICOM files found in: {dicom_folder}")
    reader.SetFileNames(series_files)
    ref_img = reader.Execute()  # provides spacing/origin/direction

    # ---- wrap numpy -> SimpleITK and copy geometry ----
    img = sitk.GetImageFromArray(vol, isVector=False)  # expects (z,y,x)
    img.CopyInformation(ref_img)

    # Optional cast (simple cast—no windowing/rescale)
    if nifti_dtype == "int16":
        img = sitk.Cast(img, sitk.sitkInt16)
    else:
        # ensure float32 on write
        img = sitk.Cast(img, sitk.sitkFloat32)

    # ---- write NIfTI ----
    sitk.WriteImage(img, str(nifti_path), useCompression=compress)

    print(f"[step8_2] Wrote NIfTI: {nifti_path}")
    print(f"[step8_2] From numpy: {npy_path}")
    print(f"[step8_2] Compress={compress}, dtype={'int16' if nifti_dtype=='int16' else 'float32'}")
