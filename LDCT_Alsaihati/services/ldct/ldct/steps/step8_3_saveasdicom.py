# ldct/steps/step8_3_saveasdicom.py
from __future__ import annotations
from pathlib import Path
import numpy as np
import SimpleITK as sitk
from datetime import datetime
import pydicom
from pydicom.uid import generate_uid


def run(cfg) -> None:
    """
    Step 8.3 — Save the simulated LDCT volume as a new DICOM series in the same study.

    Inputs
      - cfg.output_dir        : directory containing the LDCT .npy file
      - cfg.dicom_folder      : original DICOM series folder (source for metadata)

    Defaults / Tunables (optional in cfg)
      - ldct_numpy_filename   : "ldct_sim.npy"
      - output_series_description : "LDCT Simulation" (user-specified series description)
      - dicom_output_subdir   : "dicom_output" (subdirectory for DICOM files)
    """
    out = Path(cfg.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    dicom_folder = Path(cfg.dicom_folder)

    # Get configuration
    npy_name = getattr(cfg, "ldct_numpy_filename", "ldct_sim.npy")
    output_series_desc = getattr(cfg, "output_series_description", "LDCT Simulation")
    dicom_subdir = getattr(cfg, "dicom_output_subdir", "dicom_output")

    npy_path = out / npy_name
    dicom_out_dir = out / dicom_subdir
    dicom_out_dir.mkdir(parents=True, exist_ok=True)

    if not npy_path.exists():
        raise FileNotFoundError(f"[step8_3] Missing LDCT numpy volume: {npy_path}")
    if not dicom_folder.exists():
        raise FileNotFoundError(f"[step8_3] DICOM folder not found: {dicom_folder}")

    # ---- Load simulated volume (Z, H, W) ----
    vol = np.load(npy_path).astype(np.float32, copy=False)

    # ---- Read reference DICOM series ----
    series_files = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(str(dicom_folder))
    if not series_files:
        raise FileNotFoundError(f"[step8_3] No DICOM files found in: {dicom_folder}")

    # Read first DICOM to get metadata template
    ref_dcm = pydicom.dcmread(series_files[0])

    # Generate new Series Instance UID (new series in same study)
    new_series_uid = generate_uid()

    print(f"[step8_3] Creating new DICOM series with {vol.shape[0]} slices")
    print(f"[step8_3] Original Study UID: {ref_dcm.StudyInstanceUID}")
    print(f"[step8_3] Original Series UID: {ref_dcm.SeriesInstanceUID}")
    print(f"[step8_3] New Series Description: {output_series_desc}")
    print(f"[step8_3] New Series UID: {new_series_uid}")
    print(f"[step8_3] NOTE: Study UID is preserved - new series will appear in same study")

    # ---- Create DICOM files for each slice ----
    for slice_idx in range(vol.shape[0]):
        # Read corresponding reference slice if available
        if slice_idx < len(series_files):
            ref_slice_dcm = pydicom.dcmread(series_files[slice_idx])
        else:
            ref_slice_dcm = ref_dcm  # Use first as template

        # Create new dataset by copying reference
        # IMPORTANT: This preserves the Study Instance UID, Patient info, and all study-level metadata
        # The new series will appear under the same study in PACS/OHIF
        new_dcm = ref_slice_dcm.copy()

        # Update Series Instance UID (create new series in same study)
        new_dcm.SeriesInstanceUID = new_series_uid

        # Generate new SOP Instance UID for this instance
        new_dcm.SOPInstanceUID = generate_uid()

        # Update Series Description (user-specified)
        new_dcm.SeriesDescription = output_series_desc

        # Update Series Number (increment from original)
        try:
            original_series_number = int(ref_dcm.SeriesNumber)
            new_dcm.SeriesNumber = str(original_series_number + 1000)  # Offset to avoid conflicts
        except:
            new_dcm.SeriesNumber = "9999"

        # Update Instance Number
        new_dcm.InstanceNumber = str(slice_idx + 1)

        # Update dates and times
        now = datetime.now()
        new_dcm.SeriesDate = now.strftime("%Y%m%d")
        new_dcm.SeriesTime = now.strftime("%H%M%S")
        new_dcm.ContentDate = now.strftime("%Y%m%d")
        new_dcm.ContentTime = now.strftime("%H%M%S")

        # Add series description to image comments
        if hasattr(new_dcm, 'ImageComments'):
            new_dcm.ImageComments = f"LDCT Simulation (f={cfg.f}, beta={cfg.beta})"
        else:
            new_dcm.add_new([0x0020, 0x4000], 'LT', f"LDCT Simulation (f={cfg.f}, beta={cfg.beta})")

        # Get pixel data from numpy array (convert HU to stored values)
        slice_data = vol[slice_idx, :, :]

        # Convert HU to stored pixel values using rescale slope and intercept
        rescale_slope = float(getattr(ref_slice_dcm, 'RescaleSlope', 1.0))
        rescale_intercept = float(getattr(ref_slice_dcm, 'RescaleIntercept', 0.0))

        # stored_value = (HU - intercept) / slope
        stored_values = (slice_data - rescale_intercept) / rescale_slope

        # Clip and convert to int16
        stored_values = np.clip(stored_values, -32768, 32767).astype(np.int16)

        # Update pixel data
        new_dcm.PixelData = stored_values.tobytes()

        # Ensure proper attributes
        new_dcm.Rows = slice_data.shape[0]
        new_dcm.Columns = slice_data.shape[1]
        new_dcm.BitsAllocated = 16
        new_dcm.BitsStored = 16
        new_dcm.HighBit = 15
        new_dcm.PixelRepresentation = 1  # signed
        new_dcm.SamplesPerPixel = 1
        new_dcm.PhotometricInterpretation = "MONOCHROME2"

        # Save DICOM file
        output_filename = dicom_out_dir / f"ldct_{slice_idx+1:04d}.dcm"
        new_dcm.save_as(str(output_filename), write_like_original=False)

    print(f"[step8_3] ✅ Saved {vol.shape[0]} DICOM slices to: {dicom_out_dir}")
    print(f"[step8_3] Study Instance UID: {ref_dcm.StudyInstanceUID}")
    print(f"[step8_3] Series Instance UID: {new_series_uid}")
