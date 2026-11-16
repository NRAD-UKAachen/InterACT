# ldct/steps/step0_dcm2npy.py
from __future__ import annotations
from pathlib import Path
import json
import numpy as np
import SimpleITK as sitk

def run(cfg) -> None:
    """Read a DICOM series and save a NumPy array (and basic metadata)."""
    dicom_folder: Path = cfg.dicom_folder
    output_dir: Path = cfg.output_dir

    output_dir.mkdir(parents=True, exist_ok=True)

    # Discover series
    series_file_names = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(str(dicom_folder))
    if not series_file_names:
        raise FileNotFoundError(f"No DICOM files found under: {dicom_folder}")

    # Read series
    reader = sitk.ImageSeriesReader()
    reader.SetFileNames(series_file_names)
    image = reader.Execute()

    # Convert to numpy (z, y, x)
    ct_array = sitk.GetArrayFromImage(image)

    # Save image array
    np.save(output_dir / "original_ct_array.npy", ct_array)

    # Save minimal metadata (often useful downstream)
    meta = {
        "spacing": list(image.GetSpacing()),       # (x, y, z)
        "origin":  list(image.GetOrigin()),
        "direction": list(image.GetDirection()),   # length 9
        "shape_zyx": list(ct_array.shape),         # (slices, height, width)
        "dicom_folder": str(dicom_folder),
    }
    (output_dir / "original_ct_meta.json").write_text(json.dumps(meta, indent=2))

    print(f"[step0] CT array saved to {output_dir/'original_ct_array.npy'}")
    print(f"[step0] Metadata saved to {output_dir/'original_ct_meta.json'}")
