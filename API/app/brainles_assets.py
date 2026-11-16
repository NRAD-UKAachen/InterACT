"""Download and cache brainles_preprocessing's Zenodo assets (registration
atlases, SynthStrip weights) robustly enough for a fresh install.

brainles_preprocessing fetches both lazily from Zenodo the first time a
coreg/BraTS job needs them (utils/zenodo.py's fetch_atlases() /
fetch_synthstrip()), and that path does not hold up from this kind of host:

- Each file is fetched with one requests.get() and no retry or resume, and
  any failure throws away everything staged so far. Zenodo is slow from here
  (~20 min for the 88 MB atlas record) and drops connections mid-file
  (2026-10-14: IncompleteRead on brats_sri24_skullstripped.nii), so a single
  blip failed the whole job and left the cache empty for the next one.
- Its 10 s connect timeout regularly misses Zenodo's slow responses (one
  metadata request took 10.9 s), reported as "Zenodo could not be reached".
- Even with the assets cached, every fetch first asks Zenodo for the record
  metadata, and a non-200 answer (e.g. a 429 rate limit) raises instead of
  falling back to the local copy -- so a working install could still fail
  jobs whenever Zenodo misbehaved.

So this module downloads the records itself -- per-file retries with
backoff, HTTP Range resume, md5 verification, partial files kept on
API_storage across restarts -- into the exact folder layout the library
expects ({record_id}_v{version} under ATLASES_FOLDER / SYNTHSTRIP_FOLDER,
both bind-mounted onto API_storage, see docker-compose.yml). It then seeds
the library's process-level ZenodoRecord._cache with those folders, so the
library's own fetch_*() calls return the local copy without any network
request. Once cached, jobs no longer depend on Zenodo at all.

start_prefetch_thread() (called from create_app()) starts this in the
background on API startup, retrying until it succeeds, so a fresh install
has the assets ready before the first job. Jobs call
ensure_brainles_assets() before touching brainles_preprocessing; it shares a
lock with the prefetch, so a job started mid-download waits for it instead of
racing it.

A cached version is kept even if Zenodo later publishes a newer one, so
results stay reproducible; delete the {record_id}_v* folder under
API_storage/brainles_atlases (or brainles_synthstrip_weights) to pick up a
new version.

ZenodoRecord._cache, the folder naming and the record IDs are
brainles_preprocessing internals; re-check them when changing its pinned
version in requirements.txt.
"""
import hashlib
import logging
import os
import shutil
import threading
import time
import zipfile
from pathlib import Path

import requests
from brainles_preprocessing.utils.zenodo import (
    ATLASES_FOLDER, ATLASES_RECORD_ID, SYNTHSTRIP_FOLDER, SYNTHSTRIP_RECORD_ID, ZenodoRecord,
)

from .backend_helpers import log

ZENODO_RECORDS_URL = 'https://zenodo.org/api/records'
# (connect, read) -- generous: Zenodo routinely takes >10 s to answer from here.
REQUEST_TIMEOUT = (30, 120)
CHUNK_SIZE = 1024 * 1024

# (label, Zenodo record ID, folder the library looks in)
ASSETS = (
    ('registration atlases', ATLASES_RECORD_ID, Path(ATLASES_FOLDER)),
    ('SynthStrip weights', SYNTHSTRIP_RECORD_ID, Path(SYNTHSTRIP_FOLDER)),
)

# Per-request retries within one ensure_brainles_assets() call. Downloads
# resume, so each retry only fetches what is still missing.
MAX_ATTEMPTS = 10
MAX_BACKOFF_SECONDS = 300
# The startup prefetch never gives up; it just waits longer between rounds.
PREFETCH_MAX_BACKOFF_SECONDS = 1800

_lock = threading.Lock()


class _RateLimited(Exception):
    def __init__(self, retry_after):
        super().__init__(f'rate limited by Zenodo, retry after {retry_after}s')
        self.retry_after = retry_after


def ensure_brainles_assets(max_attempts=MAX_ATTEMPTS):
    """Make sure every asset in ASSETS is on disk, downloading what is
    missing, and point brainles_preprocessing at the local copies. Blocks
    while another thread (e.g. the startup prefetch) is downloading. Raises
    if an asset still can't be fetched after max_attempts per request."""
    with _lock:
        for label, record_id, target_dir in ASSETS:
            folder = _local_folder(record_id, target_dir)
            if folder is None:
                folder = _download_record(label, record_id, target_dir, max_attempts)
            ZenodoRecord._cache[(record_id, str(target_dir))] = folder


def start_prefetch_thread():
    """Fetch the assets in the background on API startup (see module
    docstring). Called once from create_app(); never delays startup."""
    def run():
        delay = 60
        while True:
            try:
                ensure_brainles_assets()
                log('BrainLes assets (registration atlases, SynthStrip weights) are ready')
                return
            except Exception as e:
                log(f'BrainLes asset prefetch failed: {e}; retrying in {delay}s', logging.WARNING)
                time.sleep(delay)
                delay = min(delay * 2, PREFETCH_MAX_BACKOFF_SECONDS)

    threading.Thread(target=run, name='brainles-assets', daemon=True).start()


def _local_folder(record_id, target_dir):
    """The newest complete, non-empty {record_id}_v{version} folder in
    target_dir, or None. (In-progress downloads live in a dot-prefixed
    staging folder, which this never matches.)"""
    candidates = []
    for path in target_dir.glob(f'{record_id}_v*'):
        try:
            version = tuple(int(part) for part in path.name.split('_v', 1)[1].split('.'))
        except ValueError:
            continue
        if path.is_dir() and any(path.iterdir()):
            candidates.append((version, path))
    return max(candidates)[1] if candidates else None


def _download_record(label, record_id, target_dir, max_attempts):
    log(f'BrainLes {label} not found locally; downloading Zenodo record {record_id}')
    record = _with_retries(lambda: _get_record(record_id), f'fetch Zenodo record {record_id}', max_attempts)
    version = record['metadata']['version']
    final_dir = target_dir / f'{record_id}_v{version}'
    # Kept across failures and restarts, so the next attempt resumes.
    staging_dir = target_dir / f'.{record_id}_v{version}.partial'
    staging_dir.mkdir(parents=True, exist_ok=True)

    for entry in record['files']:
        key = entry['key']
        if os.path.basename(key) != key or key in ('', '.', '..'):
            raise RuntimeError(f'Unexpected file name in Zenodo record {record_id}: {key!r}')
        _with_retries(
            lambda: _download_file(entry['links']['self'], staging_dir / key, entry.get('size'), entry.get('checksum')),
            f'download {label} file {key}', max_attempts,
        )

    for archive in list(staging_dir.glob('*.zip')):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(staging_dir)
        archive.unlink()

    if final_dir.exists():
        shutil.rmtree(final_dir)
    staging_dir.rename(final_dir)
    log(f'BrainLes {label} v{version} downloaded to {final_dir}')
    return final_dir


def _get_record(record_id):
    response = requests.get(f'{ZENODO_RECORDS_URL}/{record_id}', timeout=REQUEST_TIMEOUT)
    _raise_for_status(response)
    record = response.json()
    if not record.get('files'):
        raise RuntimeError(f'Zenodo record {record_id} lists no files')
    return record


def _download_file(url, destination, size, checksum):
    """Download url to destination, resuming a partial file from an earlier
    attempt, and verify it against Zenodo's size and md5."""
    if destination.exists() and size is not None and destination.stat().st_size > size:
        destination.unlink()
    offset = destination.stat().st_size if destination.exists() else 0
    if not (size is not None and offset == size):
        headers = {'Range': f'bytes={offset}-'} if offset else {}
        with requests.get(url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT) as response:
            if response.status_code != 416:  # 416: nothing left to fetch
                _raise_for_status(response)
                # A plain 200 means the server ignored the Range header.
                mode = 'ab' if response.status_code == 206 else 'wb'
                if offset:
                    log(f'Resuming {destination.name} at {offset / 1e6:.1f} MB'
                        + ('' if mode == 'ab' else ' (server ignored Range; restarting file)'))
                with open(destination, mode) as f:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        f.write(chunk)

    actual_size = destination.stat().st_size
    if size is not None and actual_size != size:
        # Short read without an exception; keep the partial file to resume.
        raise RuntimeError(f'{destination.name}: got {actual_size} of {size} bytes')
    if checksum and checksum.startswith('md5:'):
        md5 = hashlib.md5()
        with open(destination, 'rb') as f:
            for block in iter(lambda: f.read(CHUNK_SIZE), b''):
                md5.update(block)
        if md5.hexdigest() != checksum[4:]:
            destination.unlink()
            raise RuntimeError(f'{destination.name}: md5 mismatch, discarded')


def _raise_for_status(response):
    if response.status_code == 429:
        raise _RateLimited(int(response.headers.get('Retry-After', 60)))
    response.raise_for_status()


def _with_retries(fn, what, max_attempts):
    delay = 5
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == max_attempts:
                raise RuntimeError(f'Could not {what} after {max_attempts} attempts: {e}') from e
            wait = e.retry_after if isinstance(e, _RateLimited) else delay
            log(f'Failed to {what} (attempt {attempt}/{max_attempts}): {e}; retrying in {wait}s',
                logging.WARNING)
            time.sleep(wait)
            delay = min(delay * 2, MAX_BACKOFF_SECONDS)
