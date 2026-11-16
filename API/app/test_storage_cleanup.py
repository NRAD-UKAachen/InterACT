"""Smoke test for storage_cleanup's selection rules, against a fake store.

Run it inside the API container:

    docker compose exec ohif_api python3 -m app.test_storage_cleanup

Covers the decisions that make the sweep safe, since getting one of them wrong
deletes data that is not coming back:

  * an expired child of a swept root is selected, a fresh one is not
  * age comes from the newest file ANYWHERE in the tree, not the dir's own mtime
  * nothing outside SWEPT_ROOTS is reachable -- weights dirs, the old API's
    leftovers and the by-request exclusions are all invisible to the sweep
  * in a 'uid' root, a child that is not named like a DICOM UID is skipped even
    when it is ancient -- this is what keeps a template subject or config
    file sitting beside the studies out of the sweep
  * a batch job with its <id>.json beside it is never selected however old it
    is; an orphan work dir past the cutoff is
  * _remove refuses a path that is not a direct child of its root, and deletes
    one that is

Does NOT cover the daily thread, or a real pass against the real API_storage --
for that, run `python3 -m app.storage_cleanup` and read the dry-run report.

Everything happens under ${API_STORAGE_DIR}/storage_cleanup_test/, which is
itself not a swept root; the module's API_STORAGE_DIR is monkeypatched at it.
"""

import os
import shutil
import time

from . import storage_cleanup as sc
from .storage_paths import API_STORAGE_DIR

TEST_ROOT = f'{API_STORAGE_DIR}/storage_cleanup_test'

DAY = 86400
OLD = time.time() - 90 * DAY
FRESH = time.time() - 2 * DAY


def _touch_tree(path, mtime, depth=2):
    """A directory with a file `depth` levels down, both aged to `mtime`."""
    leaf = os.path.join(path, *[f'level{i}' for i in range(depth)])
    os.makedirs(leaf, exist_ok=True)
    file_path = os.path.join(leaf, 'data.nii.gz')
    with open(file_path, 'w') as f:
        f.write('x')
    for current in (file_path, leaf, path):
        os.utime(current, (mtime, mtime))
    return path


def _build():
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    # Swept roots: one expired study, one fresh one.
    _touch_tree(f'{TEST_ROOT}/fastsurfer/1.2.840.113654.2.70.1.111', OLD)
    _touch_tree(f'{TEST_ROOT}/fastsurfer/1.2.840.113654.2.70.1.222', FRESH)
    _touch_tree(f'{TEST_ROOT}/dti/1.2.840.113654.2.70.1.333', OLD)
    _touch_tree(f'{TEST_ROOT}/screw_inference/2.25.111222333', OLD)

    # Shared assets living right beside the studies in a per-study root.
    # Ancient, but not UID-named, so a 'uid' root must skip them.
    _touch_tree(f'{TEST_ROOT}/fastsurfer/fsaverage', OLD)
    _touch_tree(f'{TEST_ROOT}/fastsurfer/fsaverage_sym', OLD)
    _touch_tree(f'{TEST_ROOT}/fastsurfer/sub-test001', OLD)
    with open(f'{TEST_ROOT}/fastsurfer/global-expert-options.txt', 'w') as f:
        f.write('x')
    os.utime(f'{TEST_ROOT}/fastsurfer/global-expert-options.txt', (OLD, OLD))
    _touch_tree(f'{TEST_ROOT}/dti/dataset_description.json', OLD)

    # An 'any' root sweeps whatever it finds, UID-named or not.
    _touch_tree(f'{TEST_ROOT}/tmp/fsl_trq_9rnj', OLD)

    # A stale dir whose own mtime is recent but whose contents are ancient --
    # newest_mtime must still call it expired, and vice versa.
    mixed = _touch_tree(f'{TEST_ROOT}/synthseg/1.2.840.113654.2.70.1.444', OLD)
    with open(os.path.join(mixed, 'level0', 'late.txt'), 'w') as f:
        f.write('x')

    # Must never be reachable.
    for rel in ('totalsegmentator_weights/nnunet', 'brainles_atlases/mni',
                'federated/keys',
                'hippocampus/study_expired', 'metal_artifact_roi/scan',
                'spleen/study_expired', 'logs/jobs/2020-01-01'):
        _touch_tree(f'{TEST_ROOT}/{rel}', OLD)

    # Batch jobs: a live one (has its .json) and residue of a deleted one.
    _touch_tree(f'{TEST_ROOT}/batch_jobs/live_job', OLD)
    with open(f'{TEST_ROOT}/batch_jobs/live_job.json', 'w') as f:
        f.write('{"id": "live_job", "status": "done"}')
    os.utime(f'{TEST_ROOT}/batch_jobs/live_job.json', (OLD, OLD))
    _touch_tree(f'{TEST_ROOT}/batch_jobs/orphan_job', OLD)
    _touch_tree(f'{TEST_ROOT}/batch_jobs/fresh_orphan_job', FRESH)
    with open(f'{TEST_ROOT}/batch_jobs/half_written.json.tmp', 'w') as f:
        f.write('{}')
    os.utime(f'{TEST_ROOT}/batch_jobs/half_written.json.tmp', (OLD, OLD))


def _selected():
    """Paths a dry-run sweep would delete, relative to TEST_ROOT."""
    cutoff = sc._cutoff()
    found = []
    for rel_root in sc.SWEPT_ROOTS:
        for path, _age, _size in sc._expired_children(rel_root, cutoff):
            found.append(os.path.relpath(path, TEST_ROOT))
    for path, _age, _size in sc._sweep_batch_job_residue(cutoff):
        found.append(os.path.relpath(path, TEST_ROOT))
    return sorted(found)


def main():
    sc._self_check()
    print('self-check: SWEPT_ROOTS and NEVER_TOUCH do not overlap  OK')

    _build()
    # Point the module at the fake store. BATCH_JOBS_ROOT is derived at import,
    # so it needs redirecting too.
    sc.API_STORAGE_DIR = TEST_ROOT
    sc.BATCH_JOBS_ROOT = f'{TEST_ROOT}/batch_jobs'
    sc.RETENTION_DAYS = 30

    selected = _selected()
    print('\nwould delete:')
    for path in selected:
        print(f'  {path}')

    expected = sorted([
        'fastsurfer/1.2.840.113654.2.70.1.111',
        'dti/1.2.840.113654.2.70.1.333',
        'screw_inference/2.25.111222333',
        'tmp/fsl_trq_9rnj',
        'batch_jobs/orphan_job',
        'batch_jobs/half_written.json.tmp',
    ])
    assert selected == expected, f'\nexpected {expected}\ngot      {selected}'
    print('\nselection matches expected                              OK')

    # Spelled out individually too, so a failure says which rule broke.
    assert 'fastsurfer/1.2.840.113654.2.70.1.222' not in selected, 'fresh study selected'
    assert 'synthseg/1.2.840.113654.2.70.1.444' not in selected, \
        'newest_mtime ignored a recent file'
    for rel in ('fastsurfer/fsaverage',
                'fastsurfer/fsaverage_sym',
                'fastsurfer/sub-test001',
                'fastsurfer/global-expert-options.txt',
                'dti/dataset_description.json'):
        assert rel not in selected, f'non-UID child of a uid root selected: {rel}'
    assert 'batch_jobs/live_job' not in selected, 'a live batch job was selected'
    assert 'batch_jobs/fresh_orphan_job' not in selected, 'orphan selected before the cutoff'
    for rel in ('totalsegmentator_weights/nnunet', 'brainles_atlases/mni',
                'federated/keys',
                'hippocampus/study_expired', 'metal_artifact_roi/scan',
                'spleen/study_expired', 'logs/jobs/2020-01-01'):
        assert rel not in selected, f'excluded path selected: {rel}'
    print('excluded paths, fresh entries and live batch jobs kept   OK')
    print('non-UID children of study-keyed roots skipped            OK')

    # _remove's containment guard. These refusals must not delete anything, so
    # the paths they name are checked to still be there afterwards.
    assert not sc._remove(f'{TEST_ROOT}/fastsurfer/1.2.840.113654.2.70.1.111/level0', 'fastsurfer'), \
        '_remove accepted a grandchild'
    assert not sc._remove(f'{TEST_ROOT}/fastsurfer', 'fastsurfer'), \
        '_remove accepted the root itself'
    assert not sc._remove(f'{TEST_ROOT}/spleen/study_expired', 'fastsurfer'), \
        '_remove accepted a path outside its root'
    for rel in ('fastsurfer/1.2.840.113654.2.70.1.111/level0', 'fastsurfer',
                'spleen/study_expired'):
        assert os.path.exists(f'{TEST_ROOT}/{rel}'), f'a refused _remove still deleted {rel}'

    # The positive case deletes for real, so it gets a throwaway of its own --
    # fresh, so that even if it survived it could not alter the sweep below.
    victim = _touch_tree(f'{TEST_ROOT}/fastsurfer/1.2.840.113654.2.70.1.999', FRESH)
    assert sc._remove(victim, 'fastsurfer'), '_remove rejected a valid direct child'
    assert not os.path.exists(victim), '_remove returned True without deleting'
    print('_remove containment guard                                OK')

    # And that a pass removes exactly the selection.
    before = set(os.listdir(f'{TEST_ROOT}/fastsurfer'))
    sc.sweep()
    after = set(os.listdir(f'{TEST_ROOT}/fastsurfer'))
    assert after == {'1.2.840.113654.2.70.1.222', 'fsaverage', 'fsaverage_sym',
                     'sub-test001', 'global-expert-options.txt'}, \
        f'after apply, fastsurfer held {after} (was {before})'
    assert os.path.isdir(f'{TEST_ROOT}/fastsurfer/fsaverage_sym'), \
        'apply deleted a FreeSurfer template subject'
    assert os.path.isdir(f'{TEST_ROOT}/batch_jobs/live_job'), 'apply deleted a live batch job'
    assert os.path.isdir(f'{TEST_ROOT}/totalsegmentator_weights/nnunet'), 'apply deleted weights'
    assert not os.path.exists(f'{TEST_ROOT}/batch_jobs/orphan_job'), 'apply kept the orphan'
    print('apply deletes exactly the selection                      OK')

    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    print('\nPASS')


if __name__ == '__main__':
    main()
