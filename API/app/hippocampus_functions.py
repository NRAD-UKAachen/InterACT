"""
Runs FSL FIRST on the study's T1 series and uploads the left/right
hippocampus segmentation as a DICOM SEG.
"""
import math
import os
import shutil

import highdicom
import nibabel as nib
import numpy as np
import pandas as pd
from PIL import Image
from highdicom.seg import Segmentation, SegmentDescription
from highdicom.sr.coding import Code
from nibabel.processing import resample_from_to

from .backend_helpers import (
    client, log, find_required_seriesUIDs, sanitize_dicom_dataset,
    store_required_series_as_nifti, upload_dataframe_as_sc, upload_images_as_sc,
    upload_segmentation,
)
from .job_logging import run_logged
from .nifti_to_dicom import build_and_store_dicom_series_from_reoriented_volume
from .storage_paths import API_STORAGE_DIR

FSLDIR = os.environ.get("FSLDIR", "/usr/local/fsl")
FSL_BIN = os.path.join(FSLDIR, "bin")

# FSL FIRST's FreeSurfer-convention labels for the two structures we upload.
HIPPOCAMPUS_LABELS = {"Left Hippocampus": 17, "Right Hippocampus": 53}

_STATS_SERIES_NUMBER = 5001
_CORONAL_QC_SERIES_NUMBER = 5002
_LINKS_SERIES_NUMBER = 5003

# Base URL the "open in 3D Slicer" links in the report point at. The API
# itself is now bound to loopback (it has no auth of its own), so this must
# name an authenticated front door that proxies /hippocampus_files/... --
# not the API's own host:port, which is no longer reachable off-box.
FILES_BASE_URL = os.environ.get("API_PUBLIC_BASE_URL", "http://ohif_api:9200")

# Filenames hippocampus_wrapper() leaves behind in each study's storage_dir
# that are safe to expose for direct download (e.g. to open in 3D Slicer) --
# see routes.py's hippocampus_files_route().
DOWNLOADABLE_FILES = ('t1n_std.nii.gz', 'hippocampus_segmentation_std.nii.gz')

# Healthy-adult per-side reference range (mL), as already cited in this
# project's own nnunet_test/PIPELINES.md. This is a general adult reference,
# not adjusted for age/sex/scanner, and FSL FIRST has no boundary-correction
# pass for hippocampus in this install (see hippocampus_wrapper's docstring),
# so it can genuinely mis-fit on a given scan -- this flag exists to make
# that visible in the report rather than to serve as a clinical read.
_NORMAL_VOLUME_RANGE_ML = (2.5, 4.5)


def _flag_volume(vol_ml):
    low, high = _NORMAL_VOLUME_RANGE_ML
    if vol_ml < low:
        return 'LOW'
    if vol_ml > high:
        return 'ABOVE NORMAL'
    return 'Normal'


def _reorient_to_std(input_image, output_image):
    """Losslessly permute/flip input_image onto FSL's standard axis order
    (fslreorient2std) -- only ever 0/90/180/270-degree rotations, no
    interpolation, just a header/array rewrite.

    run_first_all does its own internal FLIRT-style registration to a
    standard/model space as the first step before fitting each structure's
    shape model, and is just as vulnerable as a bare flirt call to a
    sagittally-acquired T1's non-axis-aligned orientation (the t1n role
    matcher on this study picks "t1_mprage_sag..." series, whose NIfTI ends
    up with x=Anterior-Posterior, y=Right-Left instead of the standard x=
    Left-Right, y=Posterior-Anterior) -- see docker-context-gui's
    fsl_clinical.py reorient_to_std() for the full writeup of this failure
    mode, first found via the FSL Register+QC button producing a visibly
    torn/misaligned QC overlay for exactly this kind of input.
    """
    env = {**os.environ, "FSLDIR": FSLDIR, "FSLOUTPUTTYPE": "NIFTI_GZ"}
    cmd = [os.path.join(FSL_BIN, "fslreorient2std"), input_image, output_image]
    result = run_logged(cmd, timeout=60, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"fslreorient2std failed:\n{result.stdout}\n{result.stderr}")


def _run_first_hippocampus(t1_nifti, output_dir):
    """Run FSL FIRST restricted to L/R hippocampus; returns the combined label map path."""
    os.makedirs(output_dir, exist_ok=True)
    output_base = os.path.join(output_dir, "first")

    env = {**os.environ, "FSLDIR": FSLDIR, "FSLOUTPUTTYPE": "NIFTI_GZ"}
    cmd = [
        os.path.join(FSL_BIN, "run_first_all"),
        "-i", t1_nifti,
        "-o", output_base,
        "-s", "L_Hipp,R_Hipp",
    ]
    log("Running FSL FIRST (L/R hippocampus)")
    result = run_logged(cmd, timeout=7200, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"FSL FIRST failed:\n{result.stdout}\n{result.stderr}")

    # The boundary-corrected "_firstseg" variant only gets produced when FIRST
    # runs its full structure set; restricted to L_Hipp,R_Hipp it stops after
    # the vertex-fitting stage, so "_origsegs" is what's actually available --
    # this is also the exact file fsl_clinical.py's parse_first_volumes()
    # already reads for its validated clinical volumetry.
    seg_path = f"{output_base}_all_fast_origsegs.nii.gz"
    if not os.path.exists(seg_path):
        raise RuntimeError(f"FSL FIRST did not produce the expected output: {seg_path}")
    return seg_path


def _asymmetry_index(left_ml, right_ml):
    """Same formula as docker-context-gui/app/utils/fsl_clinical.py's
    compute_asymmetry_index(), kept in sync manually since that Flask app
    runs in a separate container with no shared Python path."""
    total = left_ml + right_ml
    return round(2.0 * (left_ml - right_ml) / total, 3) if total else 0.0


def _volume_table(volumes_ml):
    """Left/Right hippocampus volume plus asymmetry index, with the
    reference range and flag each volume falls under -- the one table both
    the per-study route (rendered and uploaded as a Secondary Capture, see
    _upload_volume_table) and the batch pipeline (written as CSV, see
    save_hippocampus_core_outputs) report."""
    left_ml, right_ml = volumes_ml
    low, high = _NORMAL_VOLUME_RANGE_ML
    return pd.DataFrame({
        'Metric': ['Left Hippocampus Volume (mL)', 'Right Hippocampus Volume (mL)',
                   'Asymmetry Index (2(L-R)/(L+R))'],
        'Value': [left_ml, right_ml, _asymmetry_index(left_ml, right_ml)],
        'Reference Range': [f'{low}-{high}', f'{low}-{high}', 'no established cutoff'],
        'Flag': [_flag_volume(left_ml), _flag_volume(right_ml), ''],
    })


def _upload_volume_table(studyInstanceUID, t1_series_uid, table):
    """
    Render the Left/Right hippocampus volume table (_volume_table) as an
    image and upload it as a DICOM Secondary Capture -- a persistent,
    viewable-in-OHIF companion to the SEG overlay. (A proper DICOM SR was
    tried first, but OHIF's dicom-sr extension only renders measurements
    anchored to a spatial coordinate on an image; a segment-linked, purely
    numeric measurement like this one crashes it instead of degrading
    gracefully, so a rendered table is the pragmatic choice here.)
    """
    reference_instance = client.retrieve_series(studyInstanceUID, t1_series_uid)[0]
    upload_dataframe_as_sc(
        table, title='FSL FIRST: Hippocampus Volumes',
        study_uid=studyInstanceUID, reference_instance=reference_instance,
        series_description='FSL FIRST Hippocampus Volumes', series_number=_STATS_SERIES_NUMBER,
    )


def _combine_first_labels(seg_path):
    """Collapse FIRST's per-structure "_origsegs" sub-volumes (X, Y, Z, 2 --
    one per requested structure, in HIPPOCAMPUS_LABELS order, each carrying
    two label tiers from the un-corrected voxel classifier, e.g. 17 and 117
    for L_Hipp) into the single 3D label map both the SEG upload and the
    coronal QC rendering need."""
    raw = nib.load(seg_path).get_fdata()
    segmentation_std = np.zeros(raw.shape[:3], dtype=np.uint8)
    for i, value in enumerate(HIPPOCAMPUS_LABELS.values()):
        segmentation_std[np.isin(raw[..., i], [value, value + 100])] = value
    return segmentation_std


def _build_coronal_qc_images(t1_std_img, segmentation_std):
    """Render coronal slices spanning the segmentation's full extent, T1
    grayscale with Left/Right hippocampus overlaid in green/purple.

    Coronal, not axial: the hippocampus is a thin structure whose long axis
    runs mostly antero-posterior, so an axial slice cuts across its *short*
    axis, producing a thin, irregular-looking cross-section at any given
    slice -- easy to mistake for a segmentation defect even when the 3D
    result is a single, correctly-shaped, fully-connected blob (as
    confirmed via connected-component analysis while chasing exactly this
    "looks scattered" report). Coronal is the plane radiologists actually
    use to judge hippocampal segmentation quality.

    Returns a list of RGB uint8 arrays ready for upload_images_as_sc().
    """
    t1_std = t1_std_img.get_fdata()

    idx = np.argwhere(segmentation_std > 0)
    y_lo, y_hi = idx[:, 1].min(), idx[:, 1].max()
    pad = 8
    y_positions = sorted(set(np.linspace(
        max(y_lo - pad, 0), min(y_hi + pad, t1_std.shape[1] - 1), 16
    ).astype(int)))

    lo, hi = np.percentile(t1_std[t1_std > 0], [1, 99])
    images = []
    for y in y_positions:
        coronal_t1 = np.clip((t1_std[:, y, :].T - lo) / (hi - lo), 0, 1)
        coronal_t1 = (coronal_t1 * 255).astype(np.uint8)
        rgb = np.stack([coronal_t1] * 3, axis=-1)
        coronal_seg = segmentation_std[:, y, :].T
        rgb[coronal_seg == HIPPOCAMPUS_LABELS["Left Hippocampus"]] = [80, 220, 80]
        rgb[coronal_seg == HIPPOCAMPUS_LABELS["Right Hippocampus"]] = [200, 100, 255]
        images.append(np.flipud(rgb))  # radiological display convention
    return images


def _coronal_qc_montage(images, columns=4):
    """Tile _build_coronal_qc_images()'s slices into one RGB grid.

    The per-study route uploads them as a multi-frame Secondary Capture
    series, which a batch case has no study to attach to -- and 16 loose
    PNGs per case would bury a cohort's result ZIP (and its manifest's
    output_files column). Every slice comes off the same reoriented volume
    at a fixed x/z extent, so they all share a shape and tile without
    padding; a partly-filled last row is padded with black."""
    rows = math.ceil(len(images) / columns)
    height, width = images[0].shape[:2]
    montage = np.zeros((rows * height, columns * width, 3), dtype=np.uint8)
    for i, image in enumerate(images):
        r, c = divmod(i, columns)
        montage[r * height:(r + 1) * height, c * width:(c + 1) * width] = image
    return montage


def _upload_download_links_card(studyInstanceUID, reference_instance):
    """Permanent card pointing at the copyable-text links page (routes.py's
    hippocampus_links_route), so the download URLs are discoverable any time
    by opening the study -- unlike the job's completion toast (10s, then
    gone) or /job_status (job expires from memory after an hour).

    This is a rendered image, not selectable text, so it can't carry the
    actual (long, per-study) download URLs in copy-pasteable form. Instead
    it points at a fixed, short, no-UID-needed index page that lists every
    segmented study -- log in, click through, copy from there.
    """
    table = pd.DataFrame({
        '3D Slicer download links for this and every other segmented study': [
            f'{FILES_BASE_URL}/hippocampus_links/',
        ],
    })
    upload_dataframe_as_sc(
        table, title='FSL FIRST: 3D Slicer Download Links',
        study_uid=studyInstanceUID, reference_instance=reference_instance,
        series_description='FSL FIRST Hippocampus 3D Slicer Links', series_number=_LINKS_SERIES_NUMBER,
    )


### ---------------------------------------------------------------------- ###
### pure local-file compute -- no dcm4chee access below this point         ###
### ---------------------------------------------------------------------- ###

def hippocampus_core(t1_nifti_path, output_dir):
    """Run FSL FIRST on an already-staged local T1 NIfTI and parse its
    outputs into local-file products, without touching dcm4chee -- what the
    per-study route (hippocampus_wrapper below) and the batch pipeline
    (routes.py's _run_hippocampus_tool) share.

    `t1_nifti_path` is the T1 to segment; every intermediate and product
    (the reoriented T1, FIRST's own output, the standard-space label map) is
    written under output_dir. No gpu_index parameter, unlike the other
    tools' cores: FSL FIRST is CPU-only, so a batch worker slot's assigned
    GPU has nothing to pin here.

    Returns a dict:
      - 'segmentation': label map resampled back onto the source T1's own
        grid, for upload_segmentation()
      - 'segmentation_labels' / 'segmentation_values': its label names/values
      - 'segmentation_std' / 'segmentation_std_path': the same map in
        FIRST's (reoriented) standard space, as array and as saved NIfTI
      - 't1_std_path' / 't1_std_img': the reoriented T1 FIRST actually ran on
      - 'first_seg_path': FIRST's raw per-structure output
      - 'volumes_ml': [left, right] volumes, from the native-grid map
      - 'volume_table': _volume_table()'s DataFrame
      - 'coronal_images': coronal QC renders (see _build_coronal_qc_images)
    """
    os.makedirs(output_dir, exist_ok=True)

    t1_std_path = os.path.join(output_dir, 't1n_std.nii.gz')
    _reorient_to_std(t1_nifti_path, t1_std_path)

    seg_path = _run_first_hippocampus(t1_std_path, output_dir)

    # Restricted to a structure subset (-s L_Hipp,R_Hipp), FIRST's "_origsegs"
    # is (X, Y, Z, 2): one sub-volume per requested structure, in request
    # order. (A full, unrestricted run produces separate per-structure files
    # instead -- first-L_Hipp_first.nii.gz etc -- with no combined map at
    # all; FSL's boundary-correction pass, which only exists for thalamus/
    # putamen/pallidum/accumbens in this install, doesn't apply to
    # hippocampus either way, so there's no accuracy reason to run it.)
    t1_native_img = nib.load(t1_nifti_path)
    voxel_mm3 = float(np.prod(t1_native_img.header.get_zooms()))

    t1_std_img = nib.load(t1_std_path)
    segmentation_std = _combine_first_labels(seg_path)

    # Save a clean std-space segmentation NIfTI alongside t1n_std.nii.gz --
    # for the per-study route both are exposed for download via
    # hippocampus_files_route() so the study can be inspected directly in 3D
    # Slicer, which (unlike OHIF on this project's typically obliquely- or
    # non-axially-favourable-acquired T1 series) shows true coronal/sagittal/
    # axial MPR with no extra reorientation step needed on the viewer's part.
    seg_std_path = os.path.join(output_dir, 'hippocampus_segmentation_std.nii.gz')
    segmentation_std_img = nib.Nifti1Image(segmentation_std, t1_std_img.affine, t1_std_img.header)
    segmentation_std_img.set_data_dtype(np.uint8)
    nib.save(segmentation_std_img, seg_std_path)

    # FIRST ran on the reoriented T1, so this label map is in that grid --
    # map it back onto the original series' native grid before
    # upload_segmentation() assigns it slice-by-slice to the source DICOM
    # instances. order=0 (nearest-neighbour) keeps it a clean 3-value label
    # map instead of blending labels at the boundary; exact here since
    # fslreorient2std is itself just an axis permutation/flip, not a true
    # resample.
    segmentation_native_img = resample_from_to(segmentation_std_img, t1_native_img, order=0)
    segmentation = np.asanyarray(segmentation_native_img.dataobj).astype(np.uint8)

    volumes_ml = [
        round(np.count_nonzero(segmentation == value) * voxel_mm3 / 1000, 2)
        for value in HIPPOCAMPUS_LABELS.values()
    ]
    log(f"Hippocampus volumes (mL): Left={volumes_ml[0]}, Right={volumes_ml[1]}")

    return {
        'segmentation': segmentation,
        't1_nifti_path': t1_nifti_path,
        'segmentation_labels': list(HIPPOCAMPUS_LABELS.keys()),
        'segmentation_values': list(HIPPOCAMPUS_LABELS.values()),
        'segmentation_std': segmentation_std,
        'segmentation_std_path': seg_std_path,
        't1_std_path': t1_std_path,
        't1_std_img': t1_std_img,
        'first_seg_path': seg_path,
        'volumes_ml': volumes_ml,
        'volume_table': _volume_table(volumes_ml),
        # Coronal QC -- see _build_coronal_qc_images()'s docstring for why
        # this is the plane to actually judge the segmentation by, not the
        # native series' axial slices. Pure numpy slicing (unlike
        # FastSurfer's nilearn surface renders), so it is computed
        # unconditionally and both callers keep it.
        'coronal_images': _build_coronal_qc_images(t1_std_img, segmentation_std),
    }


def hippocampus_batch_stats(result):
    """Flat {metric: value} row for the batch pipeline's
    results_overview.csv (see routes.py's _run_hippocampus_tool) -- reuses
    the volumetry hippocampus_core() already computed, mirroring
    fastsurfer_batch_stats. The per-side flags are the same
    reference-range check the uploaded table carries (see _flag_volume's
    caveats -- a general adult reference, not a clinical read)."""
    left_ml, right_ml = result['volumes_ml']
    return {
        'Left_Hippocampus_Volume_mL': left_ml,
        'Right_Hippocampus_Volume_mL': right_ml,
        'Asymmetry_Index': _asymmetry_index(left_ml, right_ml),
        'Left_Flag': _flag_volume(left_ml),
        'Right_Flag': _flag_volume(right_ml),
    }


### ---------------------------------------------------------------------- ###
### batch pipeline: write core outputs to a plain output tree (no dcm4chee) ###
### ---------------------------------------------------------------------- ###

def save_hippocampus_core_outputs(result, out_dir):
    """Write hippocampus_core()'s outputs to `out_dir` as plain files, for
    the batch pipeline (POST /batch/run with 'hippocampus' selected). No
    DICOM SEG here -- a batch case has no dcm4chee study to attach one to.

    Both grids are kept: the native-grid label map overlays the study's own
    T1 as staged, while the standard-space pair (reoriented T1 + label map)
    is what opens straight in 3D Slicer with true anatomical planes, the
    same reason the per-study route offers those two files for download.
    The coronal QC renders are written as a single montage PNG rather than
    16 separate files, so a cohort's result ZIP stays readable."""
    os.makedirs(out_dir, exist_ok=True)

    native_img = nib.Nifti1Image(
        result['segmentation'], nib.load(result['t1_nifti_path']).affine
    )
    native_img.set_data_dtype(np.uint8)
    nib.save(native_img, os.path.join(out_dir, 'hippocampus_segmentation.nii.gz'))

    shutil.copyfile(
        result['segmentation_std_path'],
        os.path.join(out_dir, 'hippocampus_segmentation_std.nii.gz'),
    )
    shutil.copyfile(result['t1_std_path'], os.path.join(out_dir, 't1n_std.nii.gz'))

    result['volume_table'].to_csv(os.path.join(out_dir, 'volume_table.csv'), index=False)

    Image.fromarray(_coronal_qc_montage(result['coronal_images'])).save(
        os.path.join(out_dir, 'coronal_qc.png')
    )


### ---------------------------------------------------------------------- ###
### wrapper: stage-in from dcm4chee -> core -> stage-out to dcm4chee       ###
### ---------------------------------------------------------------------- ###

def hippocampus_wrapper(studyInstanceUID):
    """Segment L/R hippocampus on the study's T1 MRI via FSL FIRST and upload as a DICOM SEG."""
    storage_dir = f"{API_STORAGE_DIR}/hippocampus/{studyInstanceUID}"

    # stage-in
    UID_dic = store_required_series_as_nifti(
        studyInstanceUID, required_series=['t1n'], storage_path=storage_dir
    )
    if not UID_dic or 't1n' not in UID_dic:
        raise ValueError(f"Study {studyInstanceUID} is missing a suitable T1 series.")

    # core -- output_dir is the study's own storage dir, so t1n_std.nii.gz /
    # first_all_fast_origsegs.nii.gz land exactly where hippocampus_files_route()
    # serves them from and where upload_reoriented_hippocampus_series() below
    # looks for its cached FIRST output.
    result = hippocampus_core(os.path.join(storage_dir, 't1n.nii.gz'), storage_dir)

    # stage-out
    upload_segmentation(
        result['segmentation'],
        labels=result['segmentation_labels'],
        values=result['segmentation_values'],
        series_uid=UID_dic['t1n'],
        study_uid=studyInstanceUID,
        seg_description='FSL FIRST Hippocampus Segmentation',
        colors=[(60, 20, -40), (60, -20, 40)],  # distinct left/right colors
    )

    reference_instance = client.retrieve_series(studyInstanceUID, UID_dic['t1n'])[0]
    _upload_volume_table(studyInstanceUID, UID_dic['t1n'], result['volume_table'])

    upload_images_as_sc(
        result['coronal_images'], study_uid=studyInstanceUID, reference_instance=reference_instance,
        series_description='FSL FIRST Hippocampus Coronal QC (green=Left, purple=Right)',
        series_number=_CORONAL_QC_SERIES_NUMBER,
    )

    _upload_download_links_card(studyInstanceUID, reference_instance)

    return (
        f"Hippocampus segmentation completed for study {studyInstanceUID}: "
        f"SEG + volume table + coronal QC + Slicer download-links card uploaded to PACS. "
        f"3D Slicer files -- T1: {FILES_BASE_URL}/hippocampus_files/{studyInstanceUID}/t1n_std.nii.gz "
        f"| segmentation: {FILES_BASE_URL}/hippocampus_files/{studyInstanceUID}/hippocampus_segmentation_std.nii.gz"
    )


_REORIENTED_T1_SERIES_NUMBER = 7000
_REORIENTED_SEG_SERIES_NUMBER = 7001


def upload_reoriented_hippocampus_series(studyInstanceUID):
    """Upload a properly axis-aligned (fslreorient2std) copy of the study's
    T1 series, plus a matching hippocampus SEG, as their own new DICOM
    series in the study.

    hippocampus_wrapper()'s SEG has to sit in the *original* series'
    (possibly oblique) geometry so it overlays correctly there -- which
    means a viewer just stepping through that series' native slices is
    stepping through oblique cuts, not true axial/coronal/sagittal planes.
    Even OHIF's own MPR reslicing, which is supposed to handle this
    regardless of acquisition angle, rendered a torn/skewed volume for this
    kind of series when tried. This uploads a second, genuinely axis-aligned
    copy instead, so plain viewing (no MPR needed) shows true anatomical
    planes.

    Requires hippocampus_wrapper() to have already run for this study --
    reuses its cached t1n_std.nii.gz / first_all_fast_origsegs.nii.gz rather
    than re-running FSL FIRST (the expensive step).
    """
    storage_dir = f"{API_STORAGE_DIR}/hippocampus/{studyInstanceUID}"
    t1_std_path = os.path.join(storage_dir, 't1n_std.nii.gz')
    seg_path = os.path.join(storage_dir, 'first_all_fast_origsegs.nii.gz')
    if not os.path.exists(t1_std_path) or not os.path.exists(seg_path):
        raise ValueError(
            f"No cached FSL FIRST output for study {studyInstanceUID} -- "
            "run hippocampus segmentation first."
        )

    UID_dic = find_required_seriesUIDs(studyInstanceUID, ['t1n'])
    if not UID_dic or 't1n' not in UID_dic:
        raise ValueError(f"Study {studyInstanceUID} is missing a suitable T1 series.")
    reference_instance = client.retrieve_series(studyInstanceUID, UID_dic['t1n'])[0]

    t1_std_img = nib.load(t1_std_path)
    volume = t1_std_img.get_fdata()

    new_mr_instances = build_and_store_dicom_series_from_reoriented_volume(
        volume, t1_std_img.affine, reference_instance,
        series_description='T1 (Reoriented, Standard Space)',
        series_number=_REORIENTED_T1_SERIES_NUMBER,
    )
    log(f"Uploaded {len(new_mr_instances)}-slice reoriented T1 series")

    raw = nib.load(seg_path).get_fdata()
    segmentation_std = np.zeros(raw.shape[:3], dtype=np.uint8)
    for i, value in enumerate(HIPPOCAMPUS_LABELS.values()):
        segmentation_std[np.isin(raw[..., i], [value, value + 100])] = value

    # (rows, cols, slices) -> (slices, rows, cols): slice i must line up
    # with new_mr_instances[i], same convention upload_segmentation() uses.
    segmentation_for_seg = segmentation_std.transpose(2, 0, 1)

    masks = []
    segment_descriptions = []
    colors = [(60, 20, -40), (60, -20, 40)]
    for i, (label, value) in enumerate(HIPPOCAMPUS_LABELS.items(), start=1):
        mask = np.zeros_like(segmentation_for_seg, dtype=np.uint8)
        mask[segmentation_for_seg == value] = 1
        masks.append(mask)
        segment_descriptions.append(SegmentDescription(
            segment_number=i,
            segment_label=label,
            algorithm_type="MANUAL",
            segmented_property_category=Code("T-D0050", "SRT", "Anatomical Structure"),
            segmented_property_type=Code("T-12770", "SRT", "Bone"),
            display_color=highdicom.color.CIELabColor(*colors[i - 1]),
        ))
    combined_mask = np.stack(masks, axis=3)

    sanitized_instances = [sanitize_dicom_dataset(ds) for ds in new_mr_instances]
    segmentation_obj = Segmentation(
        source_images=sanitized_instances,
        pixel_array=combined_mask,
        segmentation_type='BINARY',
        segment_descriptions=segment_descriptions,
        series_instance_uid=highdicom.UID(),
        series_number=_REORIENTED_SEG_SERIES_NUMBER,
        series_description='FSL FIRST Hippocampus Segmentation (Standard Space)',
        sop_instance_uid=highdicom.UID(),
        instance_number=1,
        manufacturer="TestOrg",
        manufacturer_model_name="ThresholdSegmenter",
        software_versions="1.0.0",
        device_serial_number="SN-TEST",
    )
    client.store_instances([segmentation_obj])
    log("Uploaded reoriented hippocampus SEG")
