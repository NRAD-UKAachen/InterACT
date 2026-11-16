import hashlib
import os
import shutil
import subprocess
import threading
import uuid
import zipfile
from datetime import datetime
from tempfile import SpooledTemporaryFile

import pydicom

from .backend_helpers import SERIES_CRITERIA, log, score_series_for_criteria
from .job_logging import run_logged
from .storage_paths import API_STORAGE_DIR

# Modality a NIfTI series folder is assumed to carry, keyed by tool name --
# NIfTI files have no DICOM header to read a real Modality tag from, and
# one batch submission targets one modality-consistent tool list.
#
# TotalSegmentator isn't listed here: unlike fastsurfer/brats, its modality
# varies per *task* (most tasks are CT, but brain_aneurysm's required 'tof'
# series is MR) -- see TASK_MODALITY in total_segmentator_functions.py,
# keyed by task_name instead of tool name.
TOOL_MODALITY = {
    'fastsurfer': 'MR',
    'synthseg': 'MR',
    'brats': 'MR',
    'ldct': 'CT',
    'fsl_register': 'MR',
    'hippocampus': 'MR',
    'screw_infer': 'CT',
    'fastsurfer_long': 'MR',
    'siena': 'MR',
}

# What a tool wants handed to it, per prepare_batch_for_tool's `input_kind`:
#
#   'nifti' (the default, and what every *_core() function takes) -- the
#     matched series is converted with dcm2niix and materialized as
#     <role>.nii.gz in a per-tool work dir.
#   'dicom' -- the tool needs the *original* DICOM series folder, so no
#     conversion happens and the matched series' own directory is passed
#     through instead (see `source_paths` in prepare_batch_for_tool).
#
# LDCT is the only 'dicom' tool: its pipeline reads the series with
# SimpleITK's GDCM reader for geometry (step0) and rebuilds its output series
# by copying the source instances' headers (step8_3), neither of which
# survives a trip through NIfTI. A consequence is that an already-NIfTI series
# folder can never feed it -- the runner reports that as a per-case skip.
TOOL_INPUT_KIND = {
    'ldct': 'dicom',
}

# How a tool picks which of a study's series it runs on, per
# prepare_batch_for_tool's `series_selection`:
#
#   'roles' (the default) -- greedy best-match, one series per required role
#     (map_series_to_roles). What every segmentation tool wants: BraTS needs
#     exactly one T1, one T1c, one T2 and one FLAIR.
#   'every' -- no role matching at all; every series in the study whose
#     modality matches runs separately, and the tool's runner is handed all
#     of them.
#   'all_matching' -- like 'roles', but keeps every series that passes a
#     role's must-criteria filter instead of only the single best-scoring one
#     (map_series_to_roles_all). The batch form's "use every matching series"
#     checkbox opts a job into this per entry (see _start_batch_job in
#     routes.py) for every tool except the ones that genuinely need several
#     roles at once (BraTS) or that pick a series from outside this study
#     entirely (FastSurfer Longitudinal, SIENA/SIENAX) -- those always stay
#     on 'roles' regardless of the checkbox.
#
# LDCT is 'every': simulating a low-dose acquisition is a per-series
# operation with nothing to combine across series, so a study holding four
# CT reconstructions should yield four simulated series, not one chosen by
# keyword. Its required role is therefore the unfiltered 'ct' (see
# SERIES_CRITERIA in backend_helpers.py) -- still declared, because
# /batch/plan's name-only stage-in planner and the "no matching series"
# per-case skip are both phrased in terms of roles. Since 'ct' has no keyword
# filter, 'every' and 'all_matching' would coincide for it anyway, so the
# checkbox never overrides this hardcoded entry.
TOOL_SERIES_SELECTION = {
    'ldct': 'every',
}


# Handoff directory for cohorts staged by docker-context-gui (see its
# routes/batch.py). Both containers bind-mount API_storage at this identical
# host path, so a cohort the GUI has already written there needs no second
# transfer into this API -- the /batch/<tool>/<mode> routes accept an
# `input_path` under this root instead of a multipart ZIP upload, and just
# move the tree into the job's work dir.
BATCH_STAGE_ROOT = os.environ.get(
    'BATCH_STAGE_ROOT',
    f'{API_STORAGE_DIR}/batch_inputs',
)


# Result ZIPs up to this size are built in RAM; past it zip_output_tree rolls
# over to a temp file (which lands under TMPDIR -- pointed at the API_storage
# mount in docker-compose.yml, not the container's small /tmp).
_ZIP_SPOOL_MAX_BYTES = 256 * 1024 * 1024


### ---------------------------------------------------------------------- ###
### ZIP-in for the batch route                                            ###
### ---------------------------------------------------------------------- ###

def unpack_batch_zip(zip_path, dest_dir):
    """Safely extract an uploaded ZIP of a patient_id/study/series tree into
    dest_dir, for the /batch/<tool>/<mode> route. Takes a path rather than
    the request's raw upload stream -- werkzeug's underlying
    SpooledTemporaryFile doesn't expose a `seekable` attribute the way
    zipfile expects, so the caller saves the upload to disk first. Rejects
    any member whose path would resolve outside dest_dir (zip-slip via '../'
    traversal or an absolute path) instead of trusting the archive, since it
    comes straight from an HTTP upload."""
    os.makedirs(dest_dir, exist_ok=True)
    dest_root = os.path.realpath(dest_dir)

    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            member_path = os.path.realpath(os.path.join(dest_dir, member))
            if member_path != dest_root and not member_path.startswith(dest_root + os.sep):
                raise ValueError(f"unsafe path in ZIP: {member}")
        zf.extractall(dest_dir)


def resolve_staged_input_dir(input_path):
    """Validate an `input_path` handed to a /batch route and return the real
    directory to take over, or raise ValueError.

    This API has no auth of its own (see _start_batch_job in routes.py), so
    an unconstrained path parameter here would be an
    arbitrary-directory-read primitive: point a job at any directory on the
    host, then pull it back out through GET /batch/result/<job_id>. Every
    accepted path must therefore resolve to a real subdirectory *inside*
    BATCH_STAGE_ROOT -- realpath() first, so a symlink planted under the
    stage root that points elsewhere resolves to its target and fails the
    prefix check rather than passing it.

    The root itself is rejected too: the caller takes ownership of what it
    is given (it gets moved into the job's work dir), and that must never be
    the shared handoff directory holding other users' pending cohorts."""
    if not input_path:
        raise ValueError("input_path is empty.")

    stage_root = os.path.realpath(BATCH_STAGE_ROOT)
    candidate = os.path.realpath(input_path)

    if candidate == stage_root or not candidate.startswith(stage_root + os.sep):
        raise ValueError(f"input_path must be a directory under {BATCH_STAGE_ROOT}.")
    if not os.path.isdir(candidate):
        raise ValueError("input_path is not a directory (or no longer exists).")

    return candidate


def zip_output_tree(root_dir, only_case_ids=None, extra_files=None):
    """Pack a batch route's output tree into a ZIP (rewound, ready to send).

    Spools through a SpooledTemporaryFile rather than a plain BytesIO: small
    result trees (every segmentation tool's -- masks and CSVs, a few MB per
    case) stay entirely in memory exactly as before, but anything past
    _ZIP_SPOOL_MAX_BYTES rolls over to an unnamed temp file on disk instead
    of being held in RAM. That ceiling exists because LDCT results are of a
    completely different order: a simulated series is ~100 MB of DICOM per
    case, so a cohort-sized LDCT batch would otherwise try to buffer several
    GB in the API process to answer one download.

    Still needs no cleanup scheduling, which is why the original was in
    memory: the temp file is unlinked as soon as it is closed, so it goes
    away when the response finishes with it -- nothing depends on
    response.call_on_close, which does not fire for a send_file/
    direct_passthrough response.

    `only_case_ids`, when given, restricts the ZIP to just those case
    subtrees -- each a "patient_id/study_name" string, i.e. the two
    directory levels directly under a kind='batch' job's result/ (see
    _run_batch_case in routes.py) -- and skips every other case's folder
    (and any file directly at `root_dir`'s top level) entirely, without
    descending into it. This is what lets batch_result_partial_route offer a
    ZIP of "whatever's finished so far" while the job is still 'running':
    a case only gets a terminal status once its runner call has already
    returned (see _run_worker_pool in batch_jobs.py), by which point every
    file that case was ever going to write is already on disk, so filtering
    to just the finished case_ids can never pack a file another worker slot
    is still mid-write on.

    `extra_files`, when given, is {arcname: content} (content str or bytes)
    written into the ZIP alongside the walked tree -- e.g.
    batch_result_partial_route's freshly-built results_overview.csv, which
    has no on-disk counterpart to walk into yet (that only exists once the
    job settles and its completion hook runs, see
    routes.py's _write_results_overview) but is still worth including for a
    "download finished so far" ZIP."""
    buffer = SpooledTemporaryFile(max_size=_ZIP_SPOOL_MAX_BYTES)
    allowed_cases = allowed_patients = None
    if only_case_ids is not None:
        allowed_cases = set(only_case_ids)
        allowed_patients = {cid.split('/', 1)[0] for cid in allowed_cases}

    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, files in os.walk(root_dir):
            if allowed_cases is not None:
                rel_dir = os.path.relpath(dirpath, root_dir)
                depth = 0 if rel_dir == '.' else rel_dir.count(os.sep) + 1
                if depth == 1 and os.path.basename(dirpath) not in allowed_patients:
                    dirnames[:] = []  # prune: don't even descend into this patient's folder
                    continue
                if depth == 2 and rel_dir.replace(os.sep, '/') not in allowed_cases:
                    dirnames[:] = []
                    continue
                if depth == 0:
                    continue  # a top-level file (e.g. manifest.csv) describes every case, not just the finished ones
            for name in files:
                full_path = os.path.join(dirpath, name)
                arcname = os.path.relpath(full_path, root_dir)
                zf.write(full_path, arcname)
        for arcname, content in (extra_files or {}).items():
            zf.writestr(arcname, content)
    buffer.seek(0)
    return buffer


### ---------------------------------------------------------------------- ###
### tree walking                                                            ###
### ---------------------------------------------------------------------- ###

def walk_batch_tree(root_dir):
    """Walk a `patient_id/study_name_or_id/series_name/` tree and return
    {(patient_id, study_name): [series_dir, ...]}, sorted for determinism."""
    studies = {}
    for patient_id in sorted(os.listdir(root_dir)):
        patient_dir = os.path.join(root_dir, patient_id)
        if not os.path.isdir(patient_dir):
            continue
        for study_name in sorted(os.listdir(patient_dir)):
            study_dir = os.path.join(patient_dir, study_name)
            if not os.path.isdir(study_dir):
                continue
            series_dirs = [
                os.path.join(study_dir, series_name)
                for series_name in sorted(os.listdir(study_dir))
                if os.path.isdir(os.path.join(study_dir, series_name))
            ]
            studies[(patient_id, study_name)] = series_dirs
    return studies


### ---------------------------------------------------------------------- ###
### per-series classification and DICOM -> NIfTI conversion                ###
### ---------------------------------------------------------------------- ###

def classify_series_folder(series_dir):
    """Return ('nifti', path_to_nii_gz) if `series_dir` already holds a NIfTI
    file, otherwise ('dicom', [file_path, ...]) for every regular file in it
    (assumed to be DICOM instances of one series)."""
    files = sorted(
        os.path.join(series_dir, f) for f in os.listdir(series_dir)
        if os.path.isfile(os.path.join(series_dir, f))
    )
    nifti_files = [f for f in files if f.endswith('.nii.gz') or f.endswith('.nii')]
    if nifti_files:
        if len(nifti_files) > 1:
            log(f"Warning: multiple NIfTI files in {series_dir}, using {nifti_files[0]}")
        return 'nifti', nifti_files[0]

    if not files:
        raise ValueError(f"Series folder {series_dir} contains no files.")
    return 'dicom', files


def read_dicom_modality_and_description(dicom_files):
    """Read the Modality and SeriesDescription tags off the first DICOM file
    in a series folder, in one header read. SeriesDescription is None if
    absent or empty."""
    ds = pydicom.dcmread(dicom_files[0], stop_before_pixels=True)
    return getattr(ds, 'Modality', None), (str(getattr(ds, 'SeriesDescription', '') or '').strip() or None)


def read_dicom_study_date(dicom_files):
    """DICOM StudyDate off the first file in a series folder, as a date, or
    None if absent/unparseable -- the batch-tree counterpart of
    siena_functions._parse_study_date, so a cohort staged locally and a
    study looked up live in PACS agree on what counts as a usable date."""
    ds = pydicom.dcmread(dicom_files[0], stop_before_pixels=True)
    value = getattr(ds, 'StudyDate', None)
    try:
        return datetime.strptime(value, '%Y%m%d').date()
    except (TypeError, ValueError):
        return None


def dicom_series_to_nifti(series_dir, out_dir, name_hint):
    """Convert a folder of DICOM files already on disk to NIfTI with
    dcm2niix. Mirrors the subprocess pattern in backend_helpers.series_to_nifti,
    but points dcm2niix directly at `series_dir` instead of first re-saving
    pydicom Datasets to a temp dir -- the files are already on disk here."""
    nii_dir = os.path.join(out_dir, name_hint)
    os.makedirs(nii_dir, exist_ok=True)

    result = run_logged(
        ["dcm2niix", "-z", "y", "-f", name_hint, "-o", nii_dir, series_dir],
    )
    if result.returncode != 0:
        raise RuntimeError(f"dcm2niix error on {series_dir}: {result.stderr}")

    nii_files = [f for f in os.listdir(nii_dir) if f.endswith('.nii.gz')]
    if not nii_files:
        raise RuntimeError(f"dcm2niix produced no NIfTI output for {series_dir}")
    return os.path.join(nii_dir, nii_files[0])


# Per-cache-entry locks and remembered conversion failures for
# cached_dicom_series_to_nifti, keyed by the entry's directory path. Batch
# worker slots are threads in this one API process (batch_jobs.
# _run_worker_pool), so an in-process lock is enough to serialize them.
_conversion_locks = {}
_conversion_failures = {}
_conversion_locks_guard = threading.Lock()


def cached_dicom_series_to_nifti(series_dir, cache_dir):
    """dicom_series_to_nifti, but at most once per series folder per
    `cache_dir` -- the batch pipeline passes one cache dir per job (see
    routes.py's _nifti_cache_dir), so a series matched by several tools (a
    T1 MPRAGE feeding both SynthSeg and FastSurfer) or reused as another
    study's longitudinal timepoint only goes through dcm2niix once. Returns
    the cached NIfTI path, which callers must treat as read-only (they copy
    it into their own work dir, see materialize_role_inputs).

    The entry is keyed by the series folder's real path. Conversion happens
    in a uniquely-named temp dir that is renamed into place only once
    complete, so an entry interrupted mid-dcm2niix (a container restart)
    is never mistaken for a finished one when the job resumes. A failed
    conversion is remembered (in memory) and re-raised as the same
    RuntimeError to later callers rather than re-attempted."""
    key = hashlib.sha1(os.path.realpath(series_dir).encode()).hexdigest()[:16]
    entry_dir = os.path.join(cache_dir, key)
    cached_path = os.path.join(entry_dir, 'series.nii.gz')

    with _conversion_locks_guard:
        lock = _conversion_locks.setdefault(entry_dir, threading.Lock())

    with lock:
        if os.path.isfile(cached_path):
            return cached_path
        if entry_dir in _conversion_failures:
            raise RuntimeError(_conversion_failures[entry_dir])

        tmp_dir = os.path.join(cache_dir, f'{key}.tmp-{uuid.uuid4().hex}')
        try:
            nifti_path = dicom_series_to_nifti(series_dir, tmp_dir, 'series')
            os.replace(nifti_path, os.path.join(tmp_dir, 'series.nii.gz'))
            shutil.rmtree(os.path.dirname(nifti_path), ignore_errors=True)
            os.rename(tmp_dir, entry_dir)
        except RuntimeError as e:
            _conversion_failures[entry_dir] = str(e)
            raise
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return cached_path


def classify_series(series_dirs, tool_modality):
    """Classify every series folder in one study (NIfTI vs DICOM) and
    determine its modality, WITHOUT converting anything to NIfTI yet.
    Returns a list of {'name', 'path', 'kind', 'payload', 'modality',
    'description'} dicts, one per series folder, in the order given --
    'payload' is the NIfTI file path for kind='nifti', or the list of DICOM
    file paths for kind='dicom'; 'description' is the DICOM SeriesDescription
    (None for NIfTI, or when the tag is empty), see series_match_score.

    Deliberately cheap: os.listdir() plus (for DICOM) a stop-before-pixels
    read of the first file's Modality/SeriesDescription tags, no subprocess. A real study
    folder routinely holds far more series than a tool actually needs
    (localizers, dose reports, presentation states, derived/MPR series,
    ...); running dcm2niix eagerly on all of them (the previous approach)
    wastes time on the ones that will never match a role, and -- worse --
    some of them (e.g. a Presentation State exported into its own folder,
    which isn't an image series at all) aren't convertible and would abort
    the whole batch. See map_series_to_roles() + convert_matched_series(),
    which run role-matching on this list first and only then run dcm2niix,
    on only the series that actually won a role."""
    candidates = []
    for series_dir in series_dirs:
        series_name = os.path.basename(series_dir)
        kind, payload = classify_series_folder(series_dir)
        if kind == 'nifti':
            modality, description = tool_modality, None
        else:
            modality, description = read_dicom_modality_and_description(payload)
        candidates.append({
            'name': series_name,
            'path': series_dir,
            'kind': kind,
            'payload': payload,
            'modality': modality,
            'description': description,
        })
    return candidates


def study_date(series_candidates):
    """First usable StudyDate among a study's DICOM series (classify_series()'s
    candidates), or None. Only 'dicom' candidates are tried -- an
    already-NIfTI series folder carries no header to read one from, so a
    NIfTI-only study is always undated. A study's several series are
    normally acquired the same day, so which DICOM one answers first does
    not matter."""
    for candidate in series_candidates:
        if candidate['kind'] != 'dicom':
            continue
        date = read_dicom_study_date(candidate['payload'])
        if date is not None:
            return date
    return None


def compute_study_dates(root_dir):
    """StudyDate for every (patient_id, study_name) under root_dir, keyed
    the same way prepare_batch_for_tools' cases are -- None for a study
    whose series are all NIfTI (no DICOM header to read one from) or whose
    DICOM series carry no usable StudyDate (see study_date()).

    `tool_modality` is passed as None to classify_series() here: it only
    affects the assumed modality of a NIfTI candidate, which study_date()
    never looks at, so a single pass here covers every case in a job
    regardless of which tools were actually selected."""
    return {
        key: study_date(classify_series(series_dirs, tool_modality=None))
        for key, series_dirs in walk_batch_tree(root_dir).items()
    }


### ---------------------------------------------------------------------- ###
### series -> role mapping (reuse SERIES_CRITERIA against folder name)     ###
### ---------------------------------------------------------------------- ###

def series_match_score(candidate, criteria):
    """score_series_for_criteria for one classify_series() candidate, scored
    against both its folder name and its DICOM SeriesDescription, keeping the
    better of the two (None only if neither passes).

    The folder name alone is not enough: plenty of exports (anonymizers,
    PACS dumps) name series folders by an opaque hash such as '3019348C',
    against which no role can ever match -- every tool would then report
    its required series as missing on a study that plainly has them. The
    folder name is still scored too, so a cohort whose folders were named
    deliberately (or that is NIfTI, with no header to read) keeps matching
    exactly as before."""
    scores = [
        score_series_for_criteria(candidate['modality'], text, criteria)
        for text in (candidate['name'], candidate.get('description'))
        if text
    ]
    scores = [score for score in scores if score is not None]
    return max(scores) if scores else None


def series_label(candidate):
    """Human-readable name for a classify_series() candidate in reports:
    the folder name, plus its SeriesDescription when that says something
    the folder name does not."""
    description = candidate.get('description')
    if description and description != candidate['name']:
        return f"{candidate['name']} ({description})"
    return candidate['name']


def map_series_to_roles(series_candidates, required_roles):
    """Greedily assign the best-matching series to each role in
    `required_roles` (e.g. ['t1n'] for FastSurfer), mirroring
    backend_helpers.find_required_seriesUIDs but scoring each series folder's
    name as well as its DICOM SeriesDescription (see series_match_score).
    Takes classify_series()'s candidate list (no NIfTI conversion needed for
    this -- only name, description and modality are scored). Returns {role: candidate_entry}; a role with no
    acceptable match is omitted."""
    remaining_indices = set(range(len(series_candidates)))
    role_map = {}

    for role in required_roles:
        criteria = SERIES_CRITERIA[role]
        best_index, best_score = None, -1

        for i in remaining_indices:
            score = series_match_score(series_candidates[i], criteria)
            if score is not None and score > best_score:
                best_score = score
                best_index = i

        if best_index is None:
            log(f'Could not find a matching series for required role "{role}".')
            continue

        role_map[role] = series_candidates[best_index]
        remaining_indices.discard(best_index)

    return role_map


def map_series_to_roles_all(series_candidates, required_roles):
    """Like map_series_to_roles(), but for each role keeps *every* candidate
    that passes SERIES_CRITERIA's must-criteria filter, not just the single
    best-scoring one -- the "use every matching series" opt-out from the
    default best-series-picker (see the batch form's checkbox). Unlike
    map_series_to_roles, a winning candidate is not removed from the pool: a
    tool passing this 'all_matching' selection only ever has one role in
    `required_roles`, or several *alternative* roles handled independently
    (see TOOL_SERIES_SELECTION's docstring and prepare_batch_for_tool below),
    so there is nothing to greedily protect a later role from.

    Returns {role: [candidate_entry, ...]} in study order; a role with no
    acceptable match is omitted."""
    role_map = {}
    for role in required_roles:
        criteria = SERIES_CRITERIA[role]
        matches = [
            c for c in series_candidates
            if series_match_score(c, criteria) is not None
        ]
        if matches:
            role_map[role] = matches
    return role_map


def plan_series_for_roles(series_names, required_roles, tool_modality):
    """Decide which of a study's series folders are worth transferring at
    all, from their *names alone* -- no files needed.

    map_series_to_roles() above answers the same question authoritatively,
    but only once every series is already on local disk. That is the wrong
    way round for an iRODS cohort: a real study folder holds 13-18 series
    (localizers, perfusion, diffusion, derived MPRs, ...) of which BraTS
    needs 4 and FastSurfer 1, so staging the whole tree moves several times
    more data than the batch will ever open. This runs the same
    SERIES_CRITERIA scorer up front, against the series folder names the
    iRODS catalog already knows, so the stage-in can skip the rest.

    Deliberately *permissive*: every series scoring non-None for any
    required role is kept, not just the single best-scoring one per role.
    Two reasons. Modality here can only be `tool_modality` (a DICOM series'
    real Modality tag lives in a file this planner hasn't got), so a
    keyword-plausible series that turns out to be the wrong modality must
    not have crowded out the alternatives; and the greedy assignment in
    map_series_to_roles() takes roles in order, so a series that loses one
    role can still win the next. This is a filter, not a decision -- the
    real mapping still runs API-side afterwards, on real headers, and is
    free to pick differently among what arrives.

    Returns {'series_needed': [name, ...] (input order),
             'roles_matched': [role, ...], 'missing_roles': [role, ...]}.
    A study with no plausible series for a role is still reported rather
    than dropped, so the caller can stage an empty study folder for it and
    let the batch record the usual "missing required series" per-case error
    instead of silently omitting the case.
    """
    needed = []
    roles_matched = []
    for role in required_roles:
        matched_any = False
        for name in series_names:
            if score_series_for_criteria(tool_modality, name, SERIES_CRITERIA[role]) is None:
                continue
            matched_any = True
            if name not in needed:
                needed.append(name)
        if matched_any:
            roles_matched.append(role)

    return {
        'series_needed': [name for name in series_names if name in needed],
        'roles_matched': roles_matched,
        'missing_roles': [role for role in required_roles if role not in roles_matched],
    }


### ---------------------------------------------------------------------- ###
### NIfTI conversion -- deferred twice over: past role-matching (this       ###
### section, unchanged) and, by materialize_tool_input() further below,     ###
### past job creation entirely, into the per-case worker pool               ###
### ---------------------------------------------------------------------- ###

def convert_matched_series(role_map, conversion_dir):
    """Convert only the series that actually won a role match
    (map_series_to_roles()) to NIfTI -- the dcm2niix step classify_series()
    deliberately deferred. Already-NIfTI candidates just reuse their
    existing file. DICOM candidates go through cached_dicom_series_to_nifti
    with `conversion_dir` as its cache, so a series another tool entry (or
    case) already converted into the same dir is reused, not reconverted.
    Returns a new {role: candidate_entry} dict with
    'nifti_path' added, for materialize_role_inputs(); a role whose matched
    series turns out not to be convertible (rare once non-matching junk
    series are no longer even attempted, but still possible for a
    genuinely corrupt image series) is dropped -- same "note it, don't
    abort the whole multi-study batch" handling as a role with no match at
    all, since either way the batch is missing that role for this study."""
    resolved = {}
    for role, info in role_map.items():
        if info['kind'] == 'nifti':
            resolved[role] = {**info, 'nifti_path': info['payload']}
            continue
        try:
            nifti_path = cached_dicom_series_to_nifti(info['path'], conversion_dir)
            resolved[role] = {**info, 'nifti_path': nifti_path}
        except RuntimeError as e:
            log(f'Matched series for role "{role}" ({info["path"]}) failed to convert, dropping: {e}')
    return resolved


### ---------------------------------------------------------------------- ###
### working-dir materialization (<role>.nii.gz, as *_core() expects)       ###
### ---------------------------------------------------------------------- ###

def materialize_role_inputs(role_map, work_dir):
    """Copy each matched series' NIfTI into work_dir/<role>.nii.gz -- the
    standardized filenames fastsurfer_core()/*_core() expect. Returns
    {role: dest_path}."""
    os.makedirs(work_dir, exist_ok=True)
    role_paths = {}
    for role, info in role_map.items():
        dest = os.path.join(work_dir, f'{role}.nii.gz')
        shutil.copyfile(info['nifti_path'], dest)
        role_paths[role] = dest
    return role_paths


### ---------------------------------------------------------------------- ###
### top-level orchestrator                                                  ###
### ---------------------------------------------------------------------- ###

def _source_entry(candidate):
    """The subset of a classify_series() candidate a 'dicom' tool needs: where
    the original series folder is, what it holds, and what it's called."""
    return {
        'path': candidate['path'], 'kind': candidate['kind'], 'name': candidate['name'],
    }


def prepare_batch_for_tool(root_dir, required_roles, tool_modality,
                           input_kind='nifti', series_selection='roles'):
    """For every (patient, study) found under `root_dir`, resolve which
    series folder(s) the tool runs on. Deliberately does NOT run dcm2niix or
    write anything to disk -- role-matching only (name scoring plus, for
    DICOM, a stop-before-pixels header read), same cost class as
    compute_study_dates. The dcm2niix conversion this used to do inline is
    now materialize_tool_input()'s job, called lazily by the caller once a
    case is actually about to run (batch_jobs.py's per-case worker pool) --
    see that function's docstring for why: it lets /batch/run answer
    immediately instead of blocking on a serial dcm2niix pass over the whole
    cohort, and lets that conversion happen with the pool's own concurrency
    (batch_jobs.CONCURRENCY, GPU-pinned worker slots) instead of single
    -threaded in the request handler.

    Returns {(patient_id, study_name): {
        'input_kind': 'nifti' or 'dicom' (echoes the argument, so a caller
            holding just this dict knows whether materialize_tool_input has
            anything to do),
        'series_selection': 'roles', 'every' or 'all_matching' (echoes the
            argument, same reason),
        'required_roles': required_roles (kept so a post-conversion
            missing_roles can be recomputed once materialize_tool_input knows
            which matched series actually converted),
        'work_dir': None, 'role_paths': {} -- filled in by
            materialize_tool_input (stay empty/None for input_kind='dicom',
            which never converts),
        'source_paths': {role: {'path', 'kind', 'name'}},
        'source_series': [{'path', 'kind', 'name'}, ...],
        'match_sets': [] -- filled in by materialize_tool_input when
            series_selection='all_matching'; see there for shape,
        'role_candidates'/'match_candidates': internal -- the pre-conversion
            role map materialize_tool_input needs; not meant for callers to
            read directly,
        'series_found': [series_label(), ...] -- folder name plus
            SeriesDescription, for error messages,
        'missing_roles': [role, ...] (role-matching failures only here --
            see materialize_tool_input for the post-conversion version, which
            also covers a matched series that failed to convert),
    }}

    `source_paths` records the series folder that actually won each role --
    'path' is that folder, 'kind' is 'dicom' or 'nifti'
    (classify_series_folder). It is filled in for every tool, since
    role-matching has already worked it out either way, but only a 'dicom'
    tool reads it: those consume the original instances and never the
    converted NIfTI (see TOOL_INPUT_KIND).

    With series_selection='every' (see TOOL_SERIES_SELECTION) role matching
    is bypassed altogether: `source_series` lists *every* series of the study
    whose modality is `tool_modality`, in study order, for the runner to
    process one by one, and `missing_roles` reports the tool's roles only
    when that list is empty. Series of other modalities are dropped here;
    filtering by 'kind' is left to the runner, so it can tell "this study has
    no CT at all" from "its CT arrived as NIfTI, which I can't use".

    With series_selection='all_matching' (the batch form's "use every
    matching series" checkbox, see TOOL_SERIES_SELECTION), `match_candidates`
    holds one (role, candidate) pair per candidate that passes that role's
    must-criteria filter (map_series_to_roles_all) -- effectively N
    independent single-candidate "role maps" rather than one greedy
    best-of-N, for materialize_tool_input to convert each of into its own
    match_sets entry. This only ever makes sense for a tool with a single
    required role, or several *alternative* roles handled independently (see
    TOOL_SERIES_SELECTION's docstring) -- a tool that genuinely needs several
    roles at once (BraTS) must keep series_selection='roles' regardless of
    the checkbox, since there is no single "role" here to iterate that
    wouldn't have to be combined with the others. `missing_roles` reports a
    role with zero matching candidates, same semantics as the other modes.
    """
    results = {}
    for (patient_id, study_name), series_dirs in walk_batch_tree(root_dir).items():
        candidates = classify_series(series_dirs, tool_modality)

        if series_selection == 'all_matching':
            role_map_all = map_series_to_roles_all(candidates, required_roles)
            match_candidates = [
                {'role': role, 'candidate': candidate}
                for role, role_candidates in role_map_all.items()
                for candidate in role_candidates
            ]
            results[(patient_id, study_name)] = {
                'input_kind': input_kind,
                'series_selection': series_selection,
                'required_roles': required_roles,
                'work_dir': None,
                'role_paths': {},
                'source_paths': {},
                'source_series': [],
                'match_sets': [],
                'role_candidates': {},
                'match_candidates': match_candidates,
                'series_found': [series_label(c) for c in candidates],
                'missing_roles': [role for role in required_roles if role not in role_map_all],
            }
            continue

        if series_selection == 'every':
            role_map = {}
            source_series = [
                _source_entry(c) for c in candidates if c['modality'] == tool_modality
            ]
        else:
            role_map = map_series_to_roles(candidates, required_roles)
            source_series = []
        source_paths = {role: _source_entry(info) for role, info in role_map.items()}

        if series_selection == 'every':
            missing_roles = [] if source_series else list(required_roles)
        else:
            missing_roles = [role for role in required_roles if role not in role_map]

        results[(patient_id, study_name)] = {
            'input_kind': input_kind,
            'series_selection': series_selection,
            'required_roles': required_roles,
            'work_dir': None,
            'role_paths': {},
            'source_paths': source_paths,
            'source_series': source_series,
            'match_sets': [],
            'role_candidates': role_map,
            'match_candidates': [],
            'series_found': [series_label(c) for c in candidates],
            'missing_roles': missing_roles,
        }

    return results


def materialize_tool_input(tool_input, scratch_dir, nifti_cache_dir=None):
    """Run the dcm2niix conversion prepare_batch_for_tool() deferred: turns
    one case's one tool entry's role-matched (but not yet converted) series
    into <role>.nii.gz files under `scratch_dir`, materializing 'work_dir'/
    'role_paths' (or, for series_selection='all_matching', each 'match_sets'
    entry's own work_dir/role_paths). Called lazily, once per (case, tool
    entry), from inside batch_jobs.py's per-case worker pool -- see
    routes.py's _require_tool_input -- so cohort-wide conversion happens with
    the pool's own concurrency instead of serially before the job exists.

    Returns a new dict with the same keys as `tool_input`, 'missing_roles'
    recomputed to also cover a role whose matched series failed to convert
    (prepare_batch_for_tool could only report a role with no name match at
    all; whether a matched series actually converts is only knowable once
    conversion has been attempted, same "note it, don't abort the batch"
    treatment as everywhere else). A no-op (returns `tool_input` unchanged)
    for input_kind='dicom' entries -- nothing to convert; those consume the
    original DICOM instances (see TOOL_INPUT_KIND).

    `nifti_cache_dir` is where the dcm2niix output itself goes
    (cached_dicom_series_to_nifti). The batch runners pass one job-wide dir,
    so every tool entry picking the same series shares one conversion; each
    still gets its own <role>.nii.gz copy under `scratch_dir`. Defaults to a
    private `scratch_dir/_source_nifti` (no sharing)."""
    if tool_input['input_kind'] == 'dicom':
        return tool_input

    result = dict(tool_input)
    cache_dir = nifti_cache_dir or os.path.join(scratch_dir, '_source_nifti')

    if tool_input['series_selection'] == 'all_matching':
        match_sets = []
        for entry in tool_input['match_candidates']:
            role, candidate = entry['role'], entry['candidate']
            # Scoped by role as well as series name: two of a study's
            # *alternative* roles (e.g. SynthSeg's t1n/t2) could in
            # principle both accept the same series, and each such
            # (role, candidate) pair needs its own scratch/work dir.
            match_scratch = os.path.join(scratch_dir, 'matches', role, candidate['name'])
            match_work_dir = os.path.join(match_scratch, 'input')
            converted = convert_matched_series({role: candidate}, cache_dir)
            match_role_paths = materialize_role_inputs(converted, match_work_dir)
            if role not in match_role_paths:
                continue  # conversion failed for this candidate; skip it, like a dropped role elsewhere
            match_sets.append({
                'role': role,
                'series_name': candidate['name'],
                'label': f'{role}__{candidate["name"]}',
                'work_dir': match_work_dir,
                'role_paths': match_role_paths,
                'source_paths': {role: _source_entry(candidate)},
            })
        resolved_roles = {match['role'] for match in match_sets}
        result['match_sets'] = match_sets
        result['missing_roles'] = [r for r in tool_input['required_roles'] if r not in resolved_roles]
        return result

    work_dir = os.path.join(scratch_dir, 'input')
    converted = convert_matched_series(tool_input['role_candidates'], cache_dir)
    role_paths = materialize_role_inputs(converted, work_dir)
    result['work_dir'] = work_dir
    result['role_paths'] = role_paths

    if tool_input['series_selection'] == 'every':
        result['missing_roles'] = tool_input['missing_roles']
    else:
        resolved_roles = set(role_paths)
        result['missing_roles'] = [r for r in tool_input['required_roles'] if r not in resolved_roles]
    return result


def prepare_batch_for_tools(root_dir, tool_specs):
    """Like prepare_batch_for_tool, but for several tools against the same
    input tree in one job (running multiple selected tools per study in
    one batch, rather than one tool per job).

    `tool_specs` is {tool_id: {'required_roles', 'tool_modality',
    'input_kind', 'series_selection'}} -- the last two optional, defaulting
    to 'nifti'/'roles'; see prepare_batch_for_tool. Runs the existing
    single-tool planning once per tool rather than trying to share role
    mapping across tools: two tools can want the same role at different
    modalities (a NIfTI series' modality is only ever guessed as *the
    selected tool's* expected one), so reusing one tool's
    resolved role for another's role of the same name would silently carry
    over a possibly-wrong modality assumption. Conversion is still shared
    where it safely can be: when two tools end up picking the same series
    folder (e.g. a T1 MPRAGE for both SynthSeg and FastSurfer),
    materialize_tool_input's job-wide nifti_cache_dir
    (cached_dicom_series_to_nifti) runs dcm2niix on it only once -- the
    series folder, not the role, is the cache key, so no modality guess is
    carried over.

    Returns {(patient_id, study_name): {tool_id: {
        'work_dir', 'role_paths', 'series_found', 'missing_roles', ...
    }}} (see prepare_batch_for_tool for the full per-entry shape) -- every
    tool contributes an entry for every (patient, study) under root_dir
    (mirroring prepare_batch_for_tool, which reports a study even when a
    role is missing rather than dropping it), so a caller can record a
    per-(case, tool) skip instead of silently omitting it. Cheap: no
    dcm2niix runs here, see prepare_batch_for_tool/materialize_tool_input.
    """
    cases = {}
    for tool_id, spec in tool_specs.items():
        prepared = prepare_batch_for_tool(
            root_dir,
            spec['required_roles'],
            spec['tool_modality'],
            input_kind=spec.get('input_kind', 'nifti'),
            series_selection=spec.get('series_selection', 'roles'),
        )
        for key, info in prepared.items():
            cases.setdefault(key, {})[tool_id] = info
    return cases


### ---------------------------------------------------------------------- ###
### longitudinal tools: patient-level timepoint selection (FastSurfer Long, ###
### SIENA) -- the batch counterparts of fastsurfer_long_functions.          ###
### plan_longitudinal_fastsurfer / siena_functions.plan_siena_analysis,     ###
### sourced from the studies already in the batch cohort instead of PACS   ###
### ---------------------------------------------------------------------- ###

def classify_studies_for_patient(root_dir, patient_id, required_roles, tool_modality):
    """Every study folder this patient has under root_dir, each with its
    StudyDate and its role map against `required_roles` -- the building
    block a longitudinal batch tool needs to pick timepoints from the
    studies already present in the cohort, without any dcm4chee lookup (see
    fastsurfer_long_functions.plan_longitudinal_fastsurfer_batch /
    siena_functions.plan_siena_analysis_batch). Like prepare_batch_for_tool,
    but scoped to one patient and enriched with each study's date instead of
    materializing anything to disk.

    Returns a list of {'study_name', 'date' (None if undated), 'role_map'
    ({role: classify_series() candidate}, see map_series_to_roles),
    'series_found' ([series_label(), ...])} sorted
    by study_name, one entry per study folder this patient has.
    """
    studies = []
    for (pid, study_name), series_dirs in walk_batch_tree(root_dir).items():
        if pid != patient_id:
            continue
        candidates = classify_series(series_dirs, tool_modality)
        studies.append({
            'study_name': study_name,
            'date': study_date(candidates),
            'role_map': map_series_to_roles(candidates, required_roles),
            'series_found': [series_label(c) for c in candidates],
        })
    return sorted(studies, key=lambda s: s['study_name'])


def materialize_timepoint_inputs(timepoints, conversion_dir, work_dir):
    """Convert/copy each longitudinal timepoint's matched series to
    work_dir/<tpid>.nii.gz -- the per-timepoint counterpart of
    convert_matched_series() + materialize_role_inputs() for a tool whose
    input is several *dated timepoints* of one role rather than several
    *roles* of one study (FastSurfer Long / SIENA batch mode).

    `timepoints` is a list of dicts each carrying 'tpid' (the destination
    filename stem -- FastSurfer Long's own subject id, or an arbitrary
    'baseline'/'followup'/'current' for SIENA/SIENAX, see
    fastsurfer_long_functions.plan_longitudinal_fastsurfer_batch /
    siena_functions.plan_siena_analysis_batch) and 'candidate' (a
    classify_series() dict from the timepoint's matched role, see
    classify_studies_for_patient).

    Returns {tpid: nifti_path}; a timepoint whose matched series fails to
    convert is omitted, same as convert_matched_series -- so a caller sees
    it disappear from the timepoint set rather than aborting the whole run.
    """
    os.makedirs(work_dir, exist_ok=True)
    paths = {}
    for timepoint in timepoints:
        candidate = timepoint['candidate']
        if candidate['kind'] == 'nifti':
            nifti_path = candidate['payload']
        else:
            try:
                nifti_path = cached_dicom_series_to_nifti(candidate['path'], conversion_dir)
            except RuntimeError as e:
                log(f"Timepoint {timepoint['tpid']} ({candidate['path']}) failed to convert, dropping: {e}")
                continue
        dest = os.path.join(work_dir, f"{timepoint['tpid']}.nii.gz")
        shutil.copyfile(nifti_path, dest)
        paths[timepoint['tpid']] = dest
    return paths
