"""One place on disk for everything this API logs: API_storage/logs/.

Before this existed nothing here used the logging module -- every line was a
print() to the Flask process' stdout, which only ever reached `docker logs`
(rotated away, lost on recreate, every job's lines interleaved), and tool
subprocesses ran with capture_output=True so their output only surfaced after
they exited, if at all. This module gives each job its own file, streams tool
output into it live, and leaves an api.log catch-all next to them:

  logs/api.log                                        every line, all threads
  logs/jobs/<YYYY-MM-DD>/<HHMMSS>_<kind>_<job_id>.log one per single-study job
  logs/jobs/<YYYY-MM-DD>/<...>.meta.json              its status/label/study
  logs/batch/<job_id>/job.log                         every case, interleaved
  logs/batch/<job_id>/cases/<case_id>.log             one per batch case
  logs/batch/<job_id>/meta.json

Routing works per thread: jobs.py and batch_jobs.py wrap each job/case thread
in log_to(...), and JobRoutingHandler writes every record that thread emits
-- including ones from third-party libraries using stdlib logging -- to those
files. A thread started *inside* a job does not inherit that automatically;
wrap its target with propagate_context() (run_logged/follow_file already do).

interact-gui reads these files directly off the shared API_storage mount for
its admin Job Logs page (docker-context-gui/app/utils/job_logs.py), keyed by
the layout and meta sidecars above -- keep the two sides in step.
"""

import contextlib
import json
import logging
import logging.handlers
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta

from .storage_paths import API_STORAGE_DIR

LOG_DIR = f'{API_STORAGE_DIR}/logs'
JOBS_LOG_DIR = f'{LOG_DIR}/jobs'
BATCH_LOG_DIR = f'{LOG_DIR}/batch'
BATCH_JOBS_ROOT = f'{API_STORAGE_DIR}/batch_jobs'

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'DEBUG').upper()
# 0 or less disables pruning.
RETENTION_DAYS = int(os.environ.get('LOG_RETENTION_DAYS', '30'))

_API_LOG_MAX_BYTES = 20 * 1024 * 1024
_API_LOG_BACKUPS = 10

logger = logging.getLogger('interact')

_FORMATTER = logging.Formatter(
    '%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)


### ---------------------------------------------------------------------- ###
### per-thread routing                                                      ###
### ---------------------------------------------------------------------- ###

_ctx = threading.local()

# Absolute path -> open append handle. One handle per file no matter how many
# threads write to it (a batch's job.log is shared by the driver and every
# worker slot); _files_lock serializes both the cache and the writes.
_files = {}
_file_refs = {}
_files_lock = threading.Lock()


def _targets():
    return getattr(_ctx, 'targets', ())


def _acquire(path):
    with _files_lock:
        if path not in _files:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _files[path] = open(path, 'a', encoding='utf-8')
            _file_refs[path] = 0
        _file_refs[path] += 1


def _release(path):
    with _files_lock:
        _file_refs[path] -= 1
        if _file_refs[path] <= 0:
            _files.pop(path).close()
            del _file_refs[path]


def _write(path, text):
    with _files_lock:
        handle = _files.get(path)
        try:
            if handle is None:
                # A straggler thread writing after its owning context closed
                # the file -- still worth keeping the line.
                with open(path, 'a', encoding='utf-8') as f:
                    f.write(text)
            else:
                handle.write(text)
                handle.flush()
        except OSError:
            pass


class JobRoutingHandler(logging.Handler):
    """Writes each record to every log file the emitting thread is routed to."""

    def emit(self, record):
        targets = _targets()
        if not targets:
            return
        try:
            text = self.format(record) + '\n'
        except Exception:  # noqa: BLE001 - logging must never raise into a job
            self.handleError(record)
            return
        for path in targets:
            _write(path, text)


@contextlib.contextmanager
def log_to(*rel_paths):
    """Additionally route this thread's log records to `rel_paths` (relative
    to LOG_DIR) for the duration of the block. Nests: an inner block adds to
    the outer one's targets rather than replacing them."""
    previous = _targets()
    added = [p for p in (os.path.join(LOG_DIR, r) for r in rel_paths) if p not in previous]
    for path in added:
        _acquire(path)
    _ctx.targets = previous + tuple(added)
    try:
        yield
    finally:
        _ctx.targets = previous
        for path in added:
            _release(path)


def propagate_context(fn):
    """Wrap `fn` so a new thread running it logs to the same files as the
    thread that called this. Capture happens now, not when fn runs."""
    targets = _targets()

    def wrapper(*args, **kwargs):
        previous = _targets()
        _ctx.targets = targets
        try:
            return fn(*args, **kwargs)
        finally:
            _ctx.targets = previous

    return wrapper


### ---------------------------------------------------------------------- ###
### job log files + meta sidecars                                           ###
### ---------------------------------------------------------------------- ###

def _slug(value):
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', str(value).replace('/', '__')).strip('_') or 'job'


def _now():
    # With the UTC offset, so the GUI can show it in its own local time.
    return datetime.now().astimezone()


def _now_iso():
    return _now().isoformat(timespec='seconds')


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f'{path}.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp_path, path)


def new_job_log_path(kind, job_id):
    """Relative path (under LOG_DIR) a single-study job will log to. Minted
    when the job is created so the job-status response can name it before the
    job's thread has even started."""
    now = datetime.now()
    return f'jobs/{now:%Y-%m-%d}/{now:%H%M%S}_{_slug(kind)}_{job_id}.log'


def batch_job_log_path(job_id):
    return f'batch/{job_id}/job.log'


def batch_case_log_path(job_id, case_id):
    return f'batch/{job_id}/cases/{_slug(case_id)}.log'


class JobLog:
    """Handle on a running single-study job's log: its meta sidecar is
    rewritten on every update() so the GUI always sees current status."""

    def __init__(self, rel_path, meta):
        self.rel_path = rel_path
        self.meta_path = os.path.join(LOG_DIR, rel_path[:-len('.log')] + '.meta.json')
        self.meta = meta
        self.finished = False

    def update(self, **fields):
        self.meta.update(fields)
        try:
            _write_json(self.meta_path, self.meta)
        except OSError:
            logger.exception('Could not write log meta %s', self.meta_path)

    def finish(self, status, message=None, error=None):
        self.finished = True
        started = self.meta.get('started_at')
        duration = None
        if started:
            duration = round((_now() - datetime.fromisoformat(started)).total_seconds(), 1)
        self.update(status=status, message=message, error=error,
                    finished_at=_now_iso(), duration_s=duration)


@contextlib.contextmanager
def job_log(rel_path, **meta):
    """Route this thread to a single-study job's log for the block, with a
    meta sidecar that ends 'done' or 'failed' (or whatever finish() set)."""
    handle = JobLog(rel_path, {
        'type': 'single', 'log_file': rel_path, 'status': 'running',
        'started_at': _now_iso(), 'finished_at': None, **meta,
    })
    handle.update()
    with log_to(rel_path):
        try:
            yield handle
        except BaseException as e:
            if not handle.finished:
                handle.finish('failed', error=str(e) or e.__class__.__name__)
            raise
        else:
            if not handle.finished:
                handle.finish('done')


def write_batch_meta(job):
    """(Re)write logs/batch/<job_id>/meta.json from a batch job record. The
    job's live status stays in batch_jobs/<job_id>.json, which the GUI reads
    alongside this -- this only pins down what that file may already have
    lost by the time the logs are read (it is deleted with the job)."""
    params = job.get('params') or {}
    tools = params.get('tools') or {}
    path = os.path.join(BATCH_LOG_DIR, job['id'], 'meta.json')
    try:
        existing = {}
        if os.path.exists(path):
            with open(path) as f:
                existing = json.load(f)
        _write_json(path, {
            'type': 'batch',
            'job_id': job['id'],
            'kind': job.get('kind'),
            'owner': job.get('owner'),
            'status': job.get('status'),
            'tools': sorted({f"{t.get('tool')}:{t.get('mode')}" for t in tools.values()}) if isinstance(tools, dict) else [],
            'cases': sorted(job.get('cases', {}).keys()),
            'log_file': batch_job_log_path(job['id']),
            'case_logs': {cid: batch_case_log_path(job['id'], cid) for cid in job.get('cases', {})},
            'started_at': existing.get('started_at') or _now_iso(),
            'updated_at': _now_iso(),
        })
    except (OSError, ValueError):
        logger.exception('Could not write batch log meta for %s', job.get('id'))


### ---------------------------------------------------------------------- ###
### running tools with their output streamed into the log                  ###
### ---------------------------------------------------------------------- ###

# tqdm-style progress bars redraw with \r many times a second; text-mode pipes
# turn each redraw into a line. Keep one every few seconds (and the final one).
_PROGRESS_RE = re.compile(r'\d+%\|')
_PROGRESS_INTERVAL = 5.0


class _LineLogger:
    def __init__(self, log, prefix=''):
        self.log = log
        self.prefix = prefix
        self.last_progress = 0.0

    def __call__(self, line):
        line = line.rstrip('\r\n')
        if not line.strip():
            return
        if _PROGRESS_RE.search(line) and '100%|' not in line:
            now = time.monotonic()
            if now - self.last_progress < _PROGRESS_INTERVAL:
                return
            self.last_progress = now
        self.log.info('%s%s', self.prefix, line)


def _tool_logger(name):
    return logging.getLogger(f'interact.tool.{_slug(name)}')


def _command_name(cmd):
    first = cmd.split()[0] if isinstance(cmd, str) else str(cmd[0])
    return os.path.basename(first)


def run_logged(cmd, *, timeout=None, check=False, name=None, redact=(), **popen_kwargs):
    """subprocess.run(cmd, capture_output=True, text=True, ...) that also
    logs every stdout/stderr line as it arrives, instead of only once the
    process has exited. Returns the same CompletedProcess (full stdout and
    stderr captured) and raises the same TimeoutExpired/CalledProcessError, so
    existing callers' returncode/stdout/stderr handling keeps working.
    Any string in `redact` (e.g. a licence key) is shown as *** in the logged
    command line, which ends up in api.log and on the GUI's Job Logs page."""
    popen_kwargs.pop('capture_output', None)
    popen_kwargs.pop('text', None)
    name = name or _command_name(cmd)
    tool_log = _tool_logger(name)
    printable = cmd if isinstance(cmd, str) else ' '.join(shlex.quote(str(c)) for c in cmd)
    for secret in redact:
        if secret:
            printable = printable.replace(secret, '***')
    logger.info('$ %s', printable)

    started = time.monotonic()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        text=True, errors='replace', bufsize=1, **popen_kwargs,
    )
    out_lines, err_lines = [], []

    def pump(stream, sink, emit):
        for line in stream:
            sink.append(line)
            emit(line)
        stream.close()

    thread_name = threading.current_thread().name
    readers = [
        threading.Thread(target=propagate_context(pump), args=(proc.stdout, out_lines, _LineLogger(tool_log)),
                         name=f'{thread_name}:{name}', daemon=True),
        threading.Thread(target=propagate_context(pump), args=(proc.stderr, err_lines, _LineLogger(tool_log, '[stderr] ')),
                         name=f'{thread_name}:{name}', daemon=True),
    ]
    for reader in readers:
        reader.start()

    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        for reader in readers:
            reader.join(timeout=10)
        logger.error('%s killed after exceeding its %s s timeout', name, timeout)
        raise subprocess.TimeoutExpired(cmd, timeout, output=''.join(out_lines), stderr=''.join(err_lines))

    for reader in readers:
        reader.join()
    elapsed = time.monotonic() - started
    stdout, stderr = ''.join(out_lines), ''.join(err_lines)
    level = logging.INFO if returncode == 0 else logging.WARNING
    logger.log(level, '%s exited with code %s after %.1f s', name, returncode, elapsed)

    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


def run_container_logged(docker_client, *, timeout=None, name=None, **run_kwargs):
    """docker_client.containers.run(...) with the container's output streamed
    into the log while it runs. Returns the combined output as text; a
    non-zero exit raises RuntimeError carrying the tail of that output. The
    container is always removed afterwards."""
    run_kwargs.pop('remove', None)
    run_kwargs['detach'] = True
    image = run_kwargs.get('image', '?')
    name = name or image.split('/')[-1].split(':')[0]
    tool_log = _tool_logger(name)
    logger.info('docker run %s %s', image, run_kwargs.get('command') or '')

    started = time.monotonic()
    container = docker_client.containers.run(**run_kwargs)
    chunks = []

    def pump():
        emit = _LineLogger(tool_log)
        pending = ''
        for chunk in container.logs(stream=True, follow=True):
            text = chunk.decode(errors='replace')
            chunks.append(text)
            pending += text.replace('\r', '\n')
            *lines, pending = pending.split('\n')
            for line in lines:
                emit(line)
        if pending:
            emit(pending)

    reader = threading.Thread(target=propagate_context(pump),
                              name=f'{threading.current_thread().name}:{name}', daemon=True)
    reader.start()
    try:
        try:
            result = container.wait(timeout=timeout)
        except Exception as e:  # noqa: BLE001 - requests' ReadTimeout/ConnectionError
            container.kill()
            raise RuntimeError(f'Container {image} did not finish within {timeout} s: {e}')
        reader.join(timeout=60)
        output = ''.join(chunks)
        code = result.get('StatusCode', 1)
        level = logging.INFO if code == 0 else logging.WARNING
        logger.log(level, 'Container %s exited with code %s after %.1f s', image, code, time.monotonic() - started)
        if code != 0:
            raise RuntimeError(f'Container {image} failed (exit {code}):\n{output[-4000:]}')
        return output
    finally:
        try:
            container.remove(force=True)
        except Exception:  # noqa: BLE001 - already gone is fine
            pass


@contextlib.contextmanager
def follow_file(path, name=None, poll_interval=1.0):
    """Forward lines another process appends to `path` into this thread's
    log while the block runs -- for tools that only log to a file of their
    own (the brats package's container log, BrainLes' loguru sink, the LDCT
    service). Content already in the file when the block starts is skipped,
    since it belongs to an earlier run."""
    tool_log = _tool_logger(name or os.path.basename(path))
    stop = threading.Event()
    start_offset = os.path.getsize(path) if os.path.exists(path) else 0

    def tail():
        emit = _LineLogger(tool_log)
        offset, pending = start_offset, ''
        while True:
            stopping = stop.is_set()
            try:
                size = os.path.getsize(path)
                if size < offset:  # truncated/rewritten from scratch
                    offset = 0
                if size > offset:
                    with open(path, 'r', encoding='utf-8', errors='replace') as f:
                        f.seek(offset)
                        pending += f.read()
                        offset = f.tell()
                    *lines, pending = pending.replace('\r', '\n').split('\n')
                    for line in lines:
                        emit(line)
            except OSError:
                pass
            if stopping:
                if pending:
                    emit(pending)
                return
            stop.wait(poll_interval)

    thread = threading.Thread(target=propagate_context(tail),
                              name=f'{threading.current_thread().name}:tail', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=30)


@contextlib.contextmanager
def timed(step, log=None):
    """Log the start, end and duration of one pipeline stage."""
    log = log or logger
    log.info('>> %s', step)
    started = time.monotonic()
    try:
        yield
    except BaseException:
        log.warning('!! %s failed after %.1f s', step, time.monotonic() - started)
        raise
    log.info('<< %s done in %.1f s', step, time.monotonic() - started)


### ---------------------------------------------------------------------- ###
### setup + retention                                                       ###
### ---------------------------------------------------------------------- ###

class _QuietPollFilter(logging.Filter):
    """Drop werkzeug's access-log line for the status endpoints the web
    frontends poll every couple of seconds -- thousands of identical lines a
    day that bury everything else in api.log."""

    _POLL_PATHS = ('/job_status/', '/batch/status/', '/batch/jobs')

    def filter(self, record):
        if record.name != 'werkzeug':
            return True
        message = record.getMessage()
        return not (' 200 ' in message and any(p in message for p in self._POLL_PATHS))


_configured = False


def configure_logging():
    """Install the stdout, api.log and per-job handlers on the root logger.
    Call once, before anything logs (create_app does)."""
    global _configured
    if _configured:
        return
    _configured = True

    os.makedirs(LOG_DIR, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            f'{LOG_DIR}/api.log', maxBytes=_API_LOG_MAX_BYTES, backupCount=_API_LOG_BACKUPS, encoding='utf-8',
        ),
        JobRoutingHandler(),
    ]
    quiet = _QuietPollFilter()
    for handler in handlers:
        handler.setFormatter(_FORMATTER)
        handler.addFilter(quiet)
        root.addHandler(handler)

    logger.setLevel(getattr(logging, LOG_LEVEL, logging.DEBUG))
    for noisy in ('urllib3', 'docker', 'matplotlib', 'PIL', 'h5py', 'numexpr'):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.captureWarnings(True)

    def thread_excepthook(args):
        logger.error('Uncaught exception in thread %s', args.thread.name if args.thread else '?',
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = thread_excepthook

    logger.info('Logging to %s (level %s, job logs kept %s days)', LOG_DIR, LOG_LEVEL,
                RETENTION_DAYS if RETENTION_DAYS > 0 else 'forever')
    prune_old_logs()
    threading.Thread(target=_prune_daily, name='log-retention', daemon=True).start()


def _batch_job_active(job_id):
    try:
        with open(os.path.join(BATCH_JOBS_ROOT, f'{job_id}.json')) as f:
            return json.load(f).get('status') in ('pending', 'running', 'pausing', 'paused', 'stopping')
    except (OSError, ValueError):
        return False


def newest_mtime(directory):
    """Newest mtime anywhere in a tree -- "when was this last touched?".

    A directory's own mtime only tracks its immediate entries, so a tree whose
    work happened several levels down still looks old by that alone. Public
    because storage_cleanup.py ages whole per-study trees the same way.
    """
    newest = os.path.getmtime(directory)
    for dirpath, _dirs, files in os.walk(directory):
        for name in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
            except OSError:
                pass
    return newest


def prune_old_logs():
    """Delete job logs older than RETENTION_DAYS. A batch job's folder is
    kept for as long as the job itself can still continue."""
    if RETENTION_DAYS <= 0:
        return
    cutoff_day = date.today() - timedelta(days=RETENTION_DAYS)
    removed = 0

    if os.path.isdir(JOBS_LOG_DIR):
        for name in os.listdir(JOBS_LOG_DIR):
            try:
                day = datetime.strptime(name, '%Y-%m-%d').date()
            except ValueError:
                continue
            if day < cutoff_day:
                shutil.rmtree(os.path.join(JOBS_LOG_DIR, name), ignore_errors=True)
                removed += 1

    if os.path.isdir(BATCH_LOG_DIR):
        cutoff = time.time() - RETENTION_DAYS * 86400
        for name in os.listdir(BATCH_LOG_DIR):
            path = os.path.join(BATCH_LOG_DIR, name)
            try:
                if os.path.isdir(path) and newest_mtime(path) < cutoff and not _batch_job_active(name):
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
            except OSError:
                continue

    if removed:
        logger.info('Log retention: removed %d log folder(s) older than %d days', removed, RETENTION_DAYS)


def _prune_daily():
    while True:
        time.sleep(24 * 3600)
        try:
            prune_old_logs()
        except Exception:  # noqa: BLE001 - keep the retention thread alive
            logger.exception('Log retention pass failed')
