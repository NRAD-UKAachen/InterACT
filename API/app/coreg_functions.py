import os

import nibabel as nib
from brainles_preprocessing.modality import CenterModality, Modality
from brainles_preprocessing.normalization.percentile_normalizer import PercentileNormalizer
from brainles_preprocessing.preprocessor import AtlasCentricPreprocessor

from .backend_helpers import client, log, sort_instances_by_slice_normal, store_series_as_nifti
from .brainles_assets import ensure_brainles_assets
from .nifti_to_dicom import build_and_store_dicom_series_from_volume
from .storage_paths import API_STORAGE_DIR

STORAGE_ROOT = f'{API_STORAGE_DIR}/coreg'

# Series-uploaded-by-coregistration are assigned SeriesNumbers starting here,
# incrementing per moving series, to keep them out of the way of the source
# study's own series numbers.
BASE_SERIES_NUMBER = 1234


def coreg_wrapper(centerStudyInstanceUID, centerSeriesUID, movingSeries):
    """Co-register each entry of `movingSeries` onto `centerSeriesUID` (using
    BrainLes, in native/non-atlas space) and upload each result as a new
    DICOM series in `centerStudyInstanceUID`, sharing the center series'
    geometry. This is what the POST /coreg/<studyInstanceUID> route calls.

    `movingSeries` is a list of {"studyInstanceUID": ..., "seriesInstanceUID": ...}
    and may span different studies than the center series (e.g. co-registering
    a prior PET study onto today's CT), as long as it's the same patient --
    the frontend is responsible for only offering series from that patient.
    Results always land in centerStudyInstanceUID, alongside the center series."""
    if not movingSeries:
        raise ValueError("At least one moving series must be selected.")

    for moving in movingSeries:
        if not moving.get('studyInstanceUID') or not moving.get('seriesInstanceUID'):
            raise ValueError("Each moving series needs a studyInstanceUID and seriesInstanceUID.")
        if (
            moving['studyInstanceUID'] == centerStudyInstanceUID
            and moving['seriesInstanceUID'] == centerSeriesUID
        ):
            raise ValueError("The center series cannot also be selected as a moving series.")

    # The upload step below reconstructs each result by copying the center
    # series' own per-slice DICOM headers, one per volume slice -- it can
    # only do that against a classic one-instance-per-slice series. A single
    # multi-frame instance (e.g. some PET reconstructions) can still be used
    # as a *moving* series (store_series_as_nifti handles that), just not as
    # the center.
    center_instances_preview = client.retrieve_series(centerStudyInstanceUID, centerSeriesUID)
    if len(center_instances_preview) == 1 and int(
        getattr(center_instances_preview[0], 'NumberOfFrames', 1) or 1
    ) > 1:
        raise ValueError(
            "The center series is a single multi-frame instance; only classic "
            "(one-instance-per-slice) series can be used as the center series. "
            "Multi-frame series (e.g. some PET reconstructions) can be used as a moving series."
        )

    job_dir = os.path.join(STORAGE_ROOT, centerStudyInstanceUID, centerSeriesUID)
    output_dir = os.path.join(job_dir, 'output')
    os.makedirs(output_dir, exist_ok=True)

    log(f"Downloading center series {centerSeriesUID} for study {centerStudyInstanceUID}")
    center_nifti = os.path.join(job_dir, 'center.nii.gz')
    store_series_as_nifti(centerStudyInstanceUID, centerSeriesUID, filename=center_nifti)

    # BrainLes requires a unique, filesystem-safe modality_name per input;
    # the actual SeriesDescription is only used later, for the upload. Index
    # (not SeriesInstanceUID) is the identity used throughout, since a moving
    # series is really a (study, series) pair once cross-study is allowed.
    moving_names = {i: f'moving{i}' for i in range(len(movingSeries))}
    moving_niftis = {}
    for i, moving in enumerate(movingSeries):
        name = moving_names[i]
        log(
            f"Downloading moving series {moving['seriesInstanceUID']} "
            f"from study {moving['studyInstanceUID']}"
        )
        path = os.path.join(job_dir, f'{name}.nii.gz')
        store_series_as_nifti(moving['studyInstanceUID'], moving['seriesInstanceUID'], filename=path)
        moving_niftis[i] = path

    percentile_normalizer = PercentileNormalizer(
        lower_percentile=0.1,
        upper_percentile=99.9,
        lower_limit=0,
        upper_limit=1,
    )

    raw_bet_dir = os.path.join(output_dir, 'raw_bet')
    raw_skull_dir = os.path.join(output_dir, 'raw_skull')
    norm_skull_dir = os.path.join(output_dir, 'normalized_skull')
    raw_deface_dir = os.path.join(output_dir, 'raw_defaced')
    for directory in (raw_bet_dir, raw_skull_dir, norm_skull_dir, raw_deface_dir):
        os.makedirs(directory, exist_ok=True)

    # atlas_correction=False keeps everything in the center series' native
    # space (matches the original coregistration prototype) rather than
    # resampling onto the SRI24 atlas, since the goal here is just "align
    # these series to each other", not atlas-space normalization.
    center = CenterModality(
        modality_name='center',
        input_path=center_nifti,
        raw_bet_output_path=os.path.join(raw_bet_dir, 'center_bet_raw.nii.gz'),
        raw_skull_output_path=os.path.join(raw_skull_dir, 'center_skull_raw.nii.gz'),
        normalized_skull_output_path=os.path.join(norm_skull_dir, 'center_skull_normalized.nii.gz'),
        raw_defaced_output_path=os.path.join(raw_deface_dir, 'center_defaced_raw.nii.gz'),
        normalizer=percentile_normalizer,
        atlas_correction=False,
    )

    moving_modalities = [
        Modality(
            modality_name=name,
            input_path=moving_niftis[i],
            raw_bet_output_path=os.path.join(raw_bet_dir, f'{name}_bet_raw.nii.gz'),
            raw_skull_output_path=os.path.join(raw_skull_dir, f'{name}_skull_raw.nii.gz'),
            normalized_skull_output_path=os.path.join(norm_skull_dir, f'{name}_skull_normalized.nii.gz'),
            raw_defaced_output_path=os.path.join(raw_deface_dir, f'{name}_defaced_raw.nii.gz'),
            normalizer=percentile_normalizer,
            atlas_correction=False,
        )
        for i, name in moving_names.items()
    ]

    # Before AtlasCentricPreprocessor, which otherwise fetches the atlases
    # from Zenodo itself, with no retry -- see brainles_assets.py.
    ensure_brainles_assets()
    preprocessor = AtlasCentricPreprocessor(
        center_modality=center,
        moving_modalities=moving_modalities,
        limit_cuda_visible_devices="0",
    )

    coregistration_dir = os.path.join(output_dir, 'coregistration')
    log(f"Co-registering {len(moving_modalities)} moving series to center series {centerSeriesUID}")
    preprocessor.run(
        save_dir_coregistration=coregistration_dir,
        save_dir_atlas_registration=os.path.join(output_dir, 'atlas-registration'),
        save_dir_atlas_correction=os.path.join(output_dir, 'atlas-correction'),
        save_dir_n4_bias_correction=os.path.join(output_dir, 'n4_bias_correction'),
        save_dir_brain_extraction=os.path.join(output_dir, 'brain_extraction'),
        save_dir_defacing=os.path.join(output_dir, 'defacing'),
        # Without log_file, BrainLes writes its timestamped
        # brainles_preprocessing_*.log into the process' CWD (API/); keep it
        # with the job's other output under API_storage instead.
        log_file=os.path.join(output_dir, 'brainles_preprocessing.log'),
    )

    # Reference geometry for the upload: must be sorted the same way
    # store_series_as_nifti() ordered the center volume's slices, so
    # reference_instances[i] lines up with the coregistered volume's slice i.
    center_instances = sort_instances_by_slice_normal(
        client.retrieve_series(centerStudyInstanceUID, centerSeriesUID)
    )

    uploaded = []
    for i, moving in enumerate(movingSeries):
        name = moving_names[i]
        coregistered_path = os.path.join(coregistration_dir, f'co__center__{name}.nii.gz')
        if not os.path.exists(coregistered_path):
            raise RuntimeError(
                f"Co-registration did not produce an output for series {moving['seriesInstanceUID']}."
            )

        volume = nib.load(coregistered_path).get_fdata()
        source_instance = client.retrieve_series(
            moving['studyInstanceUID'], moving['seriesInstanceUID']
        )[0]
        source_description = getattr(source_instance, 'SeriesDescription', moving['seriesInstanceUID'])

        log(f"Uploading co-registered series for moving series {moving['seriesInstanceUID']}")
        build_and_store_dicom_series_from_volume(
            volume,
            reference_instances=center_instances,
            series_description=f"Co-registered ({source_description})",
            series_number=BASE_SERIES_NUMBER + i,
            # The pixel content comes from the moving series, warped into the
            # center series' geometry — tag it with the moving series'
            # Modality, not the (copied) center instance's.
            modality=getattr(source_instance, 'Modality', None),
        )
        uploaded.append(moving['seriesInstanceUID'])

    return f"Co-registered {len(uploaded)} series onto {centerSeriesUID} and uploaded the results."
