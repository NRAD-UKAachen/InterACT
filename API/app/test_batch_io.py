"""Standalone smoke test for batch_io.py (see
Documentation/API_and_API_storage.md). Builds a synthetic 3-patient
`patient_id/study_name/series_name/` tree under API_storage/batch_io_test/
(mixing DICOM and NIfTI series, plus a couple of deliberately non-matching
series), runs prepare_batch_for_tool() against it targeting FastSurfer's
['t1n'] role, and prints the resolved series->role mapping per study.

Then asserts the two pure stage-in helpers behave: plan_series_for_roles()
(which series are worth staging at all, from names only) and
resolve_staged_input_dir() (the path guard on the by-path handoff from
docker-context-gui). Those parts fail loudly rather than printing.

No Flask route involved. Needs dicomweb_client/highdicom/dcm2niix, so run it
inside the ohif_api container, from /app (from the repo root, so Compose can
find the project):

    docker compose exec ohif_api python3 -m app.test_batch_io
"""
import os
import shutil
import threading

import numpy as np
import nibabel as nib
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

from . import batch_io
from .batch_io import (
    TOOL_MODALITY, map_series_to_roles, materialize_tool_input, plan_series_for_roles,
    prepare_batch_for_tool, prepare_batch_for_tools, resolve_staged_input_dir,
)
from .storage_paths import API_STORAGE_DIR

TEST_ROOT = f'{API_STORAGE_DIR}/batch_io_test'
INPUT_ROOT = os.path.join(TEST_ROOT, 'input')
SCRATCH_ROOT = os.path.join(TEST_ROOT, 'scratch')


def _write_dicom_slice(path, study_uid, series_uid, instance_number,
                        series_description, position_z, rng):
    rows, cols = 32, 32

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = MRImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(path, {}, file_meta=file_meta, preamble=b'\x00' * 128)
    ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.Modality = 'MR'
    ds.SeriesDescription = series_description
    ds.PatientID = 'BATCHTEST'
    ds.PatientName = 'Batch^Test'
    ds.InstanceNumber = instance_number
    ds.ImagePositionPatient = [0.0, 0.0, float(position_z)]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.PixelSpacing = [1.0, 1.0]
    ds.SliceThickness = 2.0
    ds.Rows = rows
    ds.Columns = cols
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = 'MONOCHROME2'
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelData = rng.integers(0, 4096, size=(rows, cols), dtype=np.uint16).tobytes()

    ds.save_as(path, enforce_file_format=True)


def make_dicom_series(series_dir, series_description, num_slices=8, seed=0):
    """Write a synthetic num_slices-slice MR DICOM series into series_dir."""
    os.makedirs(series_dir, exist_ok=True)
    rng = np.random.default_rng(seed)
    study_uid = generate_uid()
    series_uid = generate_uid()
    for i in range(num_slices):
        path = os.path.join(series_dir, f'{i + 1:04d}.dcm')
        _write_dicom_slice(path, study_uid, series_uid, i + 1, series_description,
                            position_z=i * 2.0, rng=rng)


def make_nifti_series(series_dir, filename='volume.nii.gz', shape=(16, 16, 8), seed=0):
    """Write a synthetic NIfTI file into series_dir. The role-scoring keyword
    match happens against the *folder* name, so the file's own name inside it
    is arbitrary."""
    os.makedirs(series_dir, exist_ok=True)
    rng = np.random.default_rng(seed)
    volume = rng.integers(0, 4096, size=shape).astype(np.float32)
    nib.save(nib.Nifti1Image(volume, np.eye(4)), os.path.join(series_dir, filename))


def build_sample_cohort():
    if os.path.exists(TEST_ROOT):
        shutil.rmtree(TEST_ROOT)

    # Patient01/Study1: DICOM t1n candidate + a DICOM distractor that
    # shouldn't match t1n at all.
    make_dicom_series(os.path.join(INPUT_ROOT, 'Patient01', 'Study1', 't1_mpr_tra_nativ'),
                       't1_mpr_tra_nativ', seed=1)
    make_dicom_series(os.path.join(INPUT_ROOT, 'Patient01', 'Study1', 't2_tse_tra'),
                       't2_tse_tra', seed=2)

    # Patient01/Study2 (longitudinal): NIfTI t1n candidate only.
    make_nifti_series(os.path.join(INPUT_ROOT, 'Patient01', 'Study2', 't1_mpr_nativ'), seed=3)

    # Patient02/Study1: only a post-contrast T1 (mustnt_contain 'KM' should
    # exclude it from t1n) and an unrelated localizer -- expect t1n missing.
    make_dicom_series(os.path.join(INPUT_ROOT, 'Patient02', 'Study1', 't1_mpr_tra_KM'),
                       't1_mpr_tra_KM', seed=4)
    make_dicom_series(os.path.join(INPUT_ROOT, 'Patient02', 'Study1', 'localizer'),
                       'localizer', seed=5)

    # Patient03/Study1: NIfTI t1n candidate plus an unrelated NIfTI FLAIR
    # (checks the greedy matcher doesn't grab the wrong one). The FLAIR
    # folder has to be named the way a real one is -- SERIES_CRITERIA's
    # 'flair' requires 't2' in the name on top of 'tirm'/'flair', see
    # SERIES_ROLE_INFO's t2_tirm_tra_flair example -- or the role can never
    # match it, which is what check_prepare_batch_for_tools below asserts on.
    make_nifti_series(os.path.join(INPUT_ROOT, 'Patient03', 'Study1', 't1_mpr_tra_nativ'), seed=6)
    make_nifti_series(os.path.join(INPUT_ROOT, 'Patient03', 'Study1', 't2_tirm_tra_flair'), seed=7)


# Series names from one study of the real Tumor-Testkohorte in the iRODS
# zone -- 18 series, of which BraTS opens 4 and FastSurfer 1. The whole point
# of plan_series_for_roles() is that the other 14 never get transferred.
REAL_STUDY_SERIES = [
    '5_t2_tirm_tra_dark-fluid_3mm',
    '7_t2_swi_tra_wave4_2mm_Mag',
    '8_t2_swi_tra_wave4_2mm_Pha',
    '9_t2_swi_tra_wave4_2mm_SWI',
    '10_t2_swi_tra_wave4_2mm_SWI_mIP',
    '11_t1_mprage_sag_iso_nativ',
    '12_t1_mprage_sag_iso_nativ_MPR_MPR_tra_1mm',
    '13_t1_mprage_sag_iso_nativ_MPR_MPR_cor_1mm',
    '20_ep2d_perf_p2_MoCo',
    '25_GBP_ep2d_perf_p2_MoCo',
    '28_ep2d_diff_3scan_trace_p2_TRACEW',
    '29_ep2d_diff_3scan_trace_p2_ADC',
    '30_t2_tse_sag',
    '31_t1_mprage_sag_iso_KM',
    '32_t1_mprage_sag_iso_KM_MPR_MPR_cor_1mm',
    '34_t1_mprage_sag_iso_KM_MPR_MPR_tra_1mm',
    '2000_ep2d_perf_p2_MoCo',
    '41_SAVED_PR',
]


def check_plan_series_for_roles():
    """plan_series_for_roles() decides what a stage-in downloads, so a
    false negative here means a case silently loses a required series and
    fails downstream. Assert both directions: needed series survive, junk
    doesn't, and a study with nothing usable still reports its missing
    roles instead of looking empty."""
    plan = plan_series_for_roles(REAL_STUDY_SERIES, ['t1n'], TOOL_MODALITY['fastsurfer'])
    # t1n = must contain t1 + mpr, must not contain KM.
    assert plan['series_needed'] == [
        '11_t1_mprage_sag_iso_nativ',
        '12_t1_mprage_sag_iso_nativ_MPR_MPR_tra_1mm',
        '13_t1_mprage_sag_iso_nativ_MPR_MPR_cor_1mm',
    ], plan['series_needed']
    assert plan['missing_roles'] == [], plan['missing_roles']

    plan = plan_series_for_roles(REAL_STUDY_SERIES, ['t1n', 't1c', 't2', 'flair'],
                                  TOOL_MODALITY['brats'])
    needed = plan['series_needed']
    assert plan['missing_roles'] == [], plan['missing_roles']
    # Every role has at least one plausible candidate kept. Note t1c is the
    # _MPR_MPR_tra_ reformat, not the sagittal '31_t1_mprage_sag_iso_KM'
    # source series: t1c's criteria require 'tra', so the post-transfer
    # matcher wouldn't pick that one either.
    for expected in ('11_t1_mprage_sag_iso_nativ',
                      '34_t1_mprage_sag_iso_KM_MPR_MPR_tra_1mm',
                      '30_t2_tse_sag', '5_t2_tirm_tra_dark-fluid_3mm'):
        assert expected in needed, f"{expected} missing from {needed}"
    # ...and the series no role can use are gone. This is where the transfer
    # saving comes from, so check it explicitly rather than just counting.
    for dropped in ('20_ep2d_perf_p2_MoCo', '28_ep2d_diff_3scan_trace_p2_TRACEW',
                     '29_ep2d_diff_3scan_trace_p2_ADC', '41_SAVED_PR',
                     '10_t2_swi_tra_wave4_2mm_SWI_mIP'):
        assert dropped not in needed, f"{dropped} should not be staged"
    assert len(needed) < len(REAL_STUDY_SERIES), needed
    # Input order preserved, no duplicates from a series matching two roles.
    assert needed == [s for s in REAL_STUDY_SERIES if s in set(needed)], needed

    # A study with nothing usable: reported, not silently dropped.
    plan = plan_series_for_roles(['localizer', 'dose_report'], ['t1n'],
                                  TOOL_MODALITY['fastsurfer'])
    assert plan['series_needed'] == [], plan['series_needed']
    assert plan['missing_roles'] == ['t1n'], plan['missing_roles']

    # 'ct_screw' (live screw inference) is the one role phrased as an
    # exclusion rather than a keyword match: any CT except a previous screw
    # run's own derived output, mirroring screw_functions._find_ct_series_uid.
    ct_study = ['CT 0.6 Br64', 'Topogramm', 'Screw Mask Raw',
                'MONAI Screw Segmentation (live inference)']
    plan = plan_series_for_roles(ct_study, ['ct_screw'], TOOL_MODALITY['screw_infer'])
    assert plan['series_needed'] == ['CT 0.6 Br64', 'Topogramm'], plan['series_needed']
    assert plan['missing_roles'] == [], plan['missing_roles']

    print(f"plan_series_for_roles: OK "
          f"(BraTS stages {len(needed)}/{len(REAL_STUDY_SERIES)} series of a real study)")


def check_plan_keeps_what_the_matcher_picks():
    """The invariant the whole optimization rests on: never filter out a
    series that map_series_to_roles() -- the authoritative mapping, run
    API-side once the data has landed -- would have chosen. If this fails,
    a cohort stages "successfully" and then reports missing series.

    Checked against the same names for every batch-eligible role set, with
    the planner and the matcher given the same modality (the planner can
    only assume the tool's; the matcher reads real DICOM headers, and any
    disagreement there can only make the matcher pickier, never less)."""
    for roles, modality in (
        (['t1n'], TOOL_MODALITY['fastsurfer']),
        (['t1n', 't1c', 't2', 'flair'], TOOL_MODALITY['brats']),
        (['tof'], 'MR'),
        (['ct_tissue'], 'CT'),
    ):
        needed = set(plan_series_for_roles(REAL_STUDY_SERIES, roles, modality)['series_needed'])
        candidates = [{'name': name, 'modality': modality} for name in REAL_STUDY_SERIES]
        for role, picked in map_series_to_roles(candidates, roles).items():
            assert picked['name'] in needed, (
                f"{roles}: matcher picks {picked['name']!r} for {role}, "
                f"but the planner would not have staged it")
    print("plan/matcher agreement: OK (planner keeps every series the "
          "post-transfer matcher picks)")


def check_resolve_staged_input_dir():
    """The by-path handoff is the one place an unauthenticated API takes a
    filesystem path from its caller, so its confinement check gets asserted
    from both sides -- including the symlink case, which is why the
    implementation resolves before comparing."""
    stage_root = os.path.join(TEST_ROOT, 'stage_root')
    shutil.rmtree(stage_root, ignore_errors=True)
    real_stage = os.path.join(stage_root, 'abc123')
    os.makedirs(real_stage, exist_ok=True)

    original_root = batch_io.BATCH_STAGE_ROOT
    batch_io.BATCH_STAGE_ROOT = stage_root
    try:
        assert resolve_staged_input_dir(real_stage) == os.path.realpath(real_stage)

        escape = os.path.join(stage_root, 'escape')
        os.symlink('/etc', escape)

        for bad in ('', '/etc', '/etc/passwd', stage_root,
                     os.path.join(stage_root, '..', '..', 'etc'),
                     escape,
                     os.path.join(stage_root, 'does_not_exist'),
                     os.path.join(real_stage, 'nope')):
            try:
                resolved = resolve_staged_input_dir(bad)
            except ValueError:
                continue
            raise AssertionError(f"{bad!r} should have been rejected, got {resolved!r}")
    finally:
        batch_io.BATCH_STAGE_ROOT = original_root
        shutil.rmtree(stage_root, ignore_errors=True)

    print("resolve_staged_input_dir: OK (accepts a staged dir, rejects "
          "root/outside/traversal/symlink/missing)")


def check_prepare_batch_for_tools():
    """prepare_batch_for_tools() (several tools run against one
    batch in one job) must resolve each tool's role mapping independently
    -- one tool missing a role for a study must not affect another tool's
    mapping for that same study, and every tool gets its own entry for
    every (patient, study) in the cohort. Uses a made-up second "tool"
    (role 'flair' instead of FastSurfer's 't1n') against the same sample
    cohort build_sample_cohort() already wrote, rather than depending on
    another real tool's role list."""
    # {tool_id: {'required_roles', 'tool_modality', ...}} -- the shape
    # prepare_batch_for_tools() takes since 'input_kind'/'series_selection'
    # were added for LDCT (both optional here, defaulting to nifti/roles).
    tool_specs = {
        'fastsurfer': {'required_roles': ['t1n'], 'tool_modality': TOOL_MODALITY['fastsurfer']},
        'flair_only': {'required_roles': ['flair'], 'tool_modality': TOOL_MODALITY['fastsurfer']},
    }
    results = prepare_batch_for_tools(INPUT_ROOT, tool_specs)

    # Patient01/Study1 has a t1n DICOM series but no flair series at all.
    p1s1 = results[('Patient01', 'Study1')]
    assert set(p1s1.keys()) == {'fastsurfer', 'flair_only'}, p1s1.keys()
    assert p1s1['fastsurfer']['missing_roles'] == [], p1s1['fastsurfer']
    assert p1s1['flair_only']['missing_roles'] == ['flair'], p1s1['flair_only']

    # Patient03/Study1 has both a t1n and a flair NIfTI series -- both tools
    # should resolve cleanly, from the same underlying series folders.
    p3s1 = results[('Patient03', 'Study1')]
    assert p3s1['fastsurfer']['missing_roles'] == [], p3s1['fastsurfer']
    assert p3s1['flair_only']['missing_roles'] == [], p3s1['flair_only']

    print("prepare_batch_for_tools: OK (each tool's role mapping resolved independently)")


def check_conversion_is_shared():
    """Two tools that both pick the same DICOM series (Patient01/Study1's
    t1n) must share one dcm2niix run when given the same nifti_cache_dir --
    each still getting its own <role>.nii.gz copy with identical content --
    and concurrent requests for one series must also convert it only once."""
    tool_specs = {
        'fastsurfer': {'required_roles': ['t1n'], 'tool_modality': TOOL_MODALITY['fastsurfer']},
        'synthseg': {'required_roles': ['t1n'], 'tool_modality': TOOL_MODALITY['synthseg']},
    }
    case = prepare_batch_for_tools(INPUT_ROOT, tool_specs)[('Patient01', 'Study1')]
    shared_root = os.path.join(TEST_ROOT, 'shared_conversion')
    shutil.rmtree(shared_root, ignore_errors=True)
    cache_dir = os.path.join(shared_root, '_nifti_cache')

    calls = []
    original = batch_io.dicom_series_to_nifti

    def counting(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    batch_io.dicom_series_to_nifti = counting
    try:
        paths = []
        for tool_id, tool_input in case.items():
            info = materialize_tool_input(
                tool_input, os.path.join(shared_root, tool_id), nifti_cache_dir=cache_dir
            )
            paths.append(info['role_paths']['t1n'])
        assert len(calls) == 1, calls
        assert paths[0] != paths[1], paths
        a, b = (np.asarray(nib.load(p).dataobj) for p in paths)
        assert np.array_equal(a, b)

        # Concurrent callers on a fresh cache: still exactly one conversion.
        calls.clear()
        concurrent_cache = os.path.join(shared_root, '_nifti_cache_concurrent')
        series_dir = case['fastsurfer']['role_candidates']['t1n']['path']
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(
                batch_io.cached_dicom_series_to_nifti(series_dir, concurrent_cache)))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(calls) == 1, calls
        assert len(set(results)) == 1 and len(results) == 4, results
    finally:
        batch_io.dicom_series_to_nifti = original

    print("conversion sharing: OK (one dcm2niix run per series across tools and threads)")


def main():
    check_plan_series_for_roles()
    check_plan_keeps_what_the_matcher_picks()
    check_resolve_staged_input_dir()
    print()

    build_sample_cohort()
    print(f"Sample cohort built under {INPUT_ROOT}\n")

    required_roles = ['t1n']  # FastSurfer's requirement
    tool_modality = TOOL_MODALITY['fastsurfer']

    planned = prepare_batch_for_tool(INPUT_ROOT, required_roles, tool_modality)

    for (patient_id, study_name), plan in sorted(planned.items()):
        scratch_dir = os.path.join(SCRATCH_ROOT, patient_id, study_name)
        info = materialize_tool_input(plan, scratch_dir)
        print(f"{patient_id}/{study_name}")
        print(f"  series found:  {info['series_found']}")
        print(f"  role mapping:  {info['role_paths']}")
        print(f"  missing roles: {info['missing_roles']}")
        for role, path in info['role_paths'].items():
            img = nib.load(path)
            print(f"    {role}.nii.gz -> shape {img.shape} at {path}")
        print()

    check_prepare_batch_for_tools()
    check_conversion_is_shared()


if __name__ == '__main__':
    main()
