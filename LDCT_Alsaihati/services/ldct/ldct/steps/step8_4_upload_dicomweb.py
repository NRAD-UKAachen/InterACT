# ldct/steps/step8_4_upload_dicomweb.py
from __future__ import annotations
import os
from pathlib import Path
import pydicom
from dicomweb_client import DICOMwebClient


def run(cfg) -> None:
    """
    Step 8.4 — Upload the DICOM series to DICOMweb server.

    Inputs
      - cfg.output_dir        : directory containing the DICOM output

    Defaults / Tunables (optional in cfg)
      - dicom_output_subdir   : "dicom_output" (subdirectory with DICOM files)
      - dicomweb_url         : DICOMweb server URL
      - upload_to_dicomweb   : True/False (enable/disable upload)
    """
    # Check if upload is enabled
    upload_enabled = getattr(cfg, "upload_to_dicomweb", True)
    if not upload_enabled:
        print("[step8_4] DICOMweb upload disabled, skipping...")
        return

    out = Path(cfg.output_dir)
    dicom_subdir = getattr(cfg, "dicom_output_subdir", "dicom_output")
    dicom_out_dir = out / dicom_subdir

    if not dicom_out_dir.exists():
        raise FileNotFoundError(f"[step8_4] DICOM output directory not found: {dicom_out_dir}")

    # Get DICOMweb server URL. Must be reachable by container name on the
    # shared network, not by the archive's external hostname -- see the same
    # DCM4CHEE_RS default used for the download side in blueprint.py, and by
    # the rest of the platform (e.g. docker-context-gui/app/utils/screw_seg_utils.py).
    dicomweb_url = getattr(
        cfg,
        "dicomweb_url",
        None,
    ) or os.environ.get(
        'DCM4CHEE_RS', 'http://arc:8080/dcm4chee-arc/aets/DCM4CHEE/rs'
    )

    print(f"[step8_4] Uploading DICOM series to DICOMweb server...")
    print(f"[step8_4] URL: {dicomweb_url}")

    # Initialize DICOMweb client
    try:
        client = DICOMwebClient(url=dicomweb_url)
    except Exception as e:
        print(f"[step8_4] ⚠️  Failed to initialize DICOMweb client: {e}")
        print(f"[step8_4] DICOM files are saved locally in: {dicom_out_dir}")
        return

    # Get all DICOM files
    dicom_files = sorted(dicom_out_dir.glob("*.dcm"))
    if not dicom_files:
        raise FileNotFoundError(f"[step8_4] No DICOM files found in: {dicom_out_dir}")

    print(f"[step8_4] Found {len(dicom_files)} DICOM files to upload")

    # Read all DICOM datasets
    datasets = []
    for dcm_file in dicom_files:
        try:
            ds = pydicom.dcmread(str(dcm_file))
            datasets.append(ds)
        except Exception as e:
            print(f"[step8_4] ⚠️  Failed to read {dcm_file}: {e}")

    if not datasets:
        raise ValueError("[step8_4] No valid DICOM datasets to upload")

    # Upload to DICOMweb server using STOW-RS
    try:
        client.store_instances(datasets=datasets)
        print(f"[step8_4] ✅ Successfully uploaded {len(datasets)} instances to DICOMweb server")
        print(f"[step8_4] Study UID: {datasets[0].StudyInstanceUID}")
        print(f"[step8_4] Series UID: {datasets[0].SeriesInstanceUID}")
        print(f"[step8_4] Series Description: {datasets[0].SeriesDescription}")
        print(f"[step8_4] Patient Name: {getattr(datasets[0], 'PatientName', 'N/A')}")
        print(f"[step8_4] NOTE: New series uploaded to same study - will appear alongside original in OHIF viewer")
    except Exception as e:
        print(f"[step8_4] ⚠️  Failed to upload to DICOMweb server: {e}")
        print(f"[step8_4] DICOM files are saved locally in: {dicom_out_dir}")
        print(f"[step8_4] You can manually upload them later")
