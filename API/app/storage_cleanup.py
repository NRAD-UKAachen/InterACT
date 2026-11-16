"""Age-based retention for API_storage, the way job_logging.py does it for logs.

Nothing under API_storage has ever aged out except the job logs, so every tool
run's working tree has accumulated since the store was created. This sweeps the
regenerable ones -- per-study output that can be rebuilt from DICOM at any time
-- and leaves everything else alone.

ALLOWLIST, NOT A DENYLIST. SWEPT_ROOTS below is the complete set of directories
this will ever delete from; a child of anything else is never a candidate, and a
directory that shows up in API_storage after this was written is ignored until
somebody adds it here deliberately. That is the whole safety design, because the
same tree also holds:

  * multi-GB downloaded weights, atlases and runtimes (totalsegmentator_weights,
    synthseg_cuda11, brainles_atlases, brainles_synthstrip_weights,
    hd_bet_weights). Deleting any of them means a multi-GB re-download through
    the proxy.
  * state that cannot be regenerated: federated/ (instance registry, CKKS keys,
    the self-call token), screw_seg_registry.json, batch_jobs/<id>.json.
  * ~8 GB of inert leftovers from the API deleted in 8eb6311d7d (spleen*, lung*,
    liver*, downloads, coregistration, statistics, lung_test_00*). Deliberately
    out of scope: clearing those is a manual one-off decision, not something a
    daily timer should be making.
  * federated, fsl_registration, hippocampus*, metal_artifact_roi and statistics,
    held back from this first version by request.
  * ldct_alsaihati, which is active but holds hand-named simulation runs rather
    than per-study output -- see NEVER_TOUCH.

BATCH JOBS ARE NEVER AGED OUT. batch_jobs.delete_job() already removes a job's
work_dir the moment a user deletes it from their batch page, so batch data stays
entirely user-driven: a <job_id>/ tree with a <job_id>.json beside it is kept
however old it is. Only residue of an already-deleted job is collected here --
see _sweep_batch_job_residue.

Run a pass on demand with:

    docker compose exec ohif_api python3 -m app.storage_cleanup

It deletes; STORAGE_RETENTION_DAYS=0 is the off switch.

There is deliberately no HTTP route for this: the API has no authentication of
its own (see API/docker-compose.yml's ports comment), so a destructive endpoint
would be reachable by anything that could reach the host.
"""

import logging
import os
import re
import shutil
import threading
import time

from .job_logging import newest_mtime
from .storage_paths import API_STORAGE_DIR

# 0 or less disables the sweep entirely, the same way LOG_RETENTION_DAYS does.
RETENTION_DAYS = int(os.environ.get('STORAGE_RETENTION_DAYS', '30'))

# A DICOM UID: digits and dots, nothing else. The standard allows no other
# characters, so this cannot match a name somebody chose.
_UID_RE = re.compile(r'^[0-9]+(?:\.[0-9]+)+$')

# Paths relative to API_STORAGE_DIR whose IMMEDIATE CHILDREN are swept, mapped
# to which children count. The roots themselves are never removed -- several
# must keep existing (tmp is TMPDIR and entrypoint.sh asserts it; batch_inputs
# and federated are created by the GUI's entrypoint).
#
#   'uid'  only a child named like a DICOM UID, i.e. one study's output.
#   'any'  every child.
#
# 'uid' is the default for a reason. These roots are keyed by
# StudyInstanceUID (see each *_functions.py's STORAGE_ROOT), but a tool can
# just as well keep a shared asset right alongside the studies -- a FreeSurfer
# template subject, a config file -- and that is not per-study output.
# Matching on the UID shape keeps anything deliberately named out of reach,
# and errs the safe way: an unrecognised name is simply never swept.
SWEPT_ROOTS = {
    'fastsurfer': 'uid',            # fastsurfer_functions.FASTSURFER_STORAGE_ROOT
    'fastsurfer_long': 'uid',       # fastsurfer_long_functions.STORAGE_ROOT
    'synthseg': 'uid',              # synthseg_functions.SYNTHSEG_STORAGE_ROOT
    'tumorseg': 'uid',              # brats_functions.BRATS_STORAGE_ROOT
    'coreg': 'uid',                 # coreg_functions.STORAGE_ROOT
    'dti': 'uid',                   # dti_functions.STORAGE_ROOT
    'siena': 'uid',                 # siena_functions.STORAGE_ROOT
    'totalsegmentator': 'uid',      # total_segmentator_functions -- NOT the weights dir
    'screw_inference': 'uid',       # screw_inference_functions.STORAGE_ROOT
    # Not study-keyed: scratch and handoff dirs, named by the code that made them.
    'batch_inputs': 'any',          # batch_io.BATCH_STAGE_ROOT -- GUI->API cohort handoff
    'tmp': 'any',                   # TMPDIR
}

# Kept out of SWEPT_ROOTS but named here so the intent is greppable, and so the
# self-check below can prove none of them is reachable as a sweep target.
NEVER_TOUCH = (
    # weights / atlases / runtimes
    'totalsegmentator_weights', 'synthseg_cuda11', 'brainles_atlases',
    'brainles_synthstrip_weights', 'hd_bet_weights',
    # held back by request
    'federated', 'fsl_registration', 'hippocampus', 'hippocampus_ct',
    'metal_artifact_roi', 'statistics',
    # Active (the ldct compose profile writes it) but NOT per-study output: its
    # children are hand-named simulation runs -- ABD_CTDI_12__50__B40f,
    # LDCT_Simulation___Alsaihati_Method, twennyfive -- not study UIDs, so
    # nothing here can be regenerated from DICOM on demand the way the swept
    # roots can. Move it into SWEPT_ROOTS as 'any' if those turn out to be
    # disposable after all; it is a one-line change.
    'ldct_alsaihati',
    # stateful / separate services
    'SAM_data', 'logs',
    # remains of the API deleted in 8eb6311d7d
    'spleen', 'spleen_work', 'lung', 'liver', 'liver_work', 'coregistration',
    'downloads', 'lung_test_001', 'lung_test_002',
    # test fixtures, regenerated by their own modules
    'batch_io_test', 'batch_jobs_test',
)

BATCH_JOBS_ROOT = f'{API_STORAGE_DIR}/batch_jobs'

logger = logging.getLogger('interact.storage_cleanup')


def _cutoff():
    return time.time() - RETENTION_DAYS * 86400


def _tree_size(path):
    """Bytes on disk under `path`, for the report. Metadata-only walk."""
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _human(num_bytes):
    value = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.1f} TB'


def _expired_children(rel_root, cutoff):
    """Yield (path, age_days, size_bytes) for each immediate child of
    API_STORAGE_DIR/rel_root untouched since `cutoff` and matching that root's
    child rule. Age is the newest mtime anywhere in the child's tree
    (job_logging.newest_mtime), not the child's own mtime -- a directory's mtime
    only tracks its immediate entries, so a study whose work happened three
    levels down would otherwise always look stale."""
    root = os.path.join(API_STORAGE_DIR, rel_root)
    if not os.path.isdir(root):
        return
    uid_only = SWEPT_ROOTS.get(rel_root) == 'uid'
    for name in sorted(os.listdir(root)):
        if uid_only and not _UID_RE.match(name):
            continue
        path = os.path.join(root, name)
        try:
            newest = newest_mtime(path) if os.path.isdir(path) else os.path.getmtime(path)
        except OSError:
            continue
        if newest >= cutoff:
            continue
        yield path, (time.time() - newest) / 86400, _tree_size(path)


def _sweep_batch_job_residue(cutoff):
    """Candidates left behind by a job the user already deleted.

    delete_job() unlinks <job_id>.json first and only then rmtree()s the work
    dir with ignore_errors=True, so a partial failure (a busy mount, a
    permission error mid-tree) leaves a <job_id>/ tree that no longer has a
    registry entry -- invisible to the API and to the GUI's batch page, and
    otherwise there forever. Same for a *.json.tmp that _save_locked wrote but
    never got to os.replace().

    A job that still HAS its .json is never a candidate, whatever its age or
    status: that one is still listed on somebody's batch page, and removing it
    is their call, not this timer's.
    """
    if not os.path.isdir(BATCH_JOBS_ROOT):
        return
    for name in sorted(os.listdir(BATCH_JOBS_ROOT)):
        path = os.path.join(BATCH_JOBS_ROOT, name)
        if os.path.isdir(path):
            if os.path.exists(os.path.join(BATCH_JOBS_ROOT, f'{name}.json')):
                continue  # live job -- user-driven deletion only
        elif not name.endswith('.json.tmp'):
            continue
        try:
            newest = newest_mtime(path) if os.path.isdir(path) else os.path.getmtime(path)
        except OSError:
            continue
        # The age gate also covers a job being created right now, whose work dir
        # can exist for a moment before its first _save_locked() writes the JSON.
        if newest >= cutoff:
            continue
        yield path, (time.time() - newest) / 86400, _tree_size(path)


def _remove(path, rel_root):
    """The only place this module deletes anything.

    Re-derives the target from its root instead of trusting the caller, in the
    style of batch_io.resolve_staged_input_dir: the path must resolve to a
    direct child of the swept root it came from, and that root must itself be
    inside API_STORAGE_DIR. A symlinked child therefore fails the check rather
    than letting an rmtree follow it somewhere else.
    """
    root = os.path.realpath(os.path.join(API_STORAGE_DIR, rel_root))
    storage = os.path.realpath(API_STORAGE_DIR)
    real = os.path.realpath(path)
    if not root.startswith(storage + os.sep):
        logger.error('Storage retention: refusing %s -- root outside API_STORAGE_DIR', path)
        return False
    if os.path.dirname(real) != root or real == root:
        logger.error('Storage retention: refusing %s -- not a direct child of %s', path, root)
        return False
    try:
        if os.path.isdir(real):
            shutil.rmtree(real)
        else:
            os.remove(real)
    except OSError:
        logger.exception('Storage retention: could not remove %s', path)
        return False
    return True


def sweep():
    """One retention pass. Returns (items, bytes) removed."""
    if RETENTION_DAYS <= 0:
        logger.info('Storage retention: disabled (STORAGE_RETENTION_DAYS=%d)', RETENTION_DAYS)
        return 0, 0
    cutoff = _cutoff()

    candidates = []
    for rel_root in SWEPT_ROOTS:
        for path, age_days, size in _expired_children(rel_root, cutoff):
            candidates.append((rel_root, path, age_days, size))
    for path, age_days, size in _sweep_batch_job_residue(cutoff):
        candidates.append(('batch_jobs', path, age_days, size))

    if not candidates:
        logger.info('Storage retention: nothing older than %d days', RETENTION_DAYS)
        return 0, 0

    removed = freed = 0
    for rel_root, path, age_days, size in candidates:
        if not _remove(path, rel_root):
            continue
        removed += 1
        freed += size
        logger.info('  %s  %dd  %s', os.path.relpath(path, API_STORAGE_DIR),
                    int(age_days), _human(size))

    logger.info('Storage retention: removed %d item(s), freed %s', removed, _human(freed))
    return removed, freed


def _sweep_daily():
    while True:
        try:
            sweep()
        except Exception:  # noqa: BLE001 - keep the retention thread alive
            logger.exception('Storage retention pass failed')
        time.sleep(24 * 3600)


def start_retention_thread():
    """Start the daily sweep. Called once from create_app(), after
    batch_jobs.load_jobs_from_disk() so the registry is on disk before the
    residue pass looks for orphans. The first pass runs inside the thread, not
    inline, so a store this size never delays container startup."""
    if RETENTION_DAYS <= 0:
        logger.info('Storage retention: disabled (STORAGE_RETENTION_DAYS=%d)', RETENTION_DAYS)
        return
    logger.info('Storage retention: every %d day(s), %d root(s)',
                RETENTION_DAYS, len(SWEPT_ROOTS))
    threading.Thread(target=_sweep_daily, name='storage-retention', daemon=True).start()


def _self_check():
    """Nothing in NEVER_TOUCH may be reachable as a sweep target -- neither as a
    swept root itself nor as a child of one."""
    swept = set(SWEPT_ROOTS)
    for rel in NEVER_TOUCH:
        assert rel not in swept, f'{rel} is in both SWEPT_ROOTS and NEVER_TOUCH'
        parent = os.path.dirname(rel)
        assert not (parent and parent in swept), f'{rel} would be swept as a child of {parent}'


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    _self_check()
    print(f'API_STORAGE_DIR = {API_STORAGE_DIR}')
    print(f'retention       = {RETENTION_DAYS} day(s)\n')
    count, size = sweep()
    print(f'\n{count} item(s), {_human(size)}')
