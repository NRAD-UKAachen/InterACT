"""
Bridge from this API's background-job model to the standalone LDCT service.

The low-dose CT simulation itself lives in its own container (LDCT_Alsaihati/,
published as `ldct_api` on the shared interact-dcm4chee network). That
service is self-sufficient -- given a study/series UID it pulls the source
series from dcm4chee, runs the 14-step Alsaihati pipeline and STOWs the
simulated low-dose series back as a new series in the same study -- but it
tracks progress in its *own* job registry, with its own status vocabulary.

The viewer only knows how to poll this API's /job_status/<id> (see jobs.py),
so rather than teach OHIF a second polling protocol, this wrapper runs inside
one of our own background jobs and simply blocks on the LDCT service's status
endpoint until it reaches a terminal state. The viewer therefore sees an LDCT
simulation as just another background job, and its study auto-refresh fires
when the simulated series is actually on dcm4chee.
"""
import os
import shutil
import time
import uuid

import requests

from .backend_helpers import log
from .job_logging import follow_file

# Reached by container name over interact-dcm4chee -- the service also
# publishes 8001 on the host, but nothing here should depend on that.
LDCT_SERVICE_URL = os.environ.get('LDCT_SERVICE_URL', 'http://ldct_api:8000').rstrip('/')

# The pipeline is GPU-bound and typically takes 15-30 minutes for a chest CT,
# so poll unhurriedly and allow a wide margin before declaring it hung.
POLL_INTERVAL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 90 * 60

# Simulation parameters forwarded verbatim to the LDCT service (see
# build_config/run_ldct_pipeline in LDCT_Alsaihati's ldct/blueprint.py). Only
# these are passed on, so an unexpected key in the request body can't reach
# the service's config. study_uid/series_uid are added separately, from the
# route's own arguments rather than from the body.
SIMULATION_PARAMS = (
    'f',
    'beta',
    'use_gpu',
    'gpu_index',
    'ldct_numpy_filename',
    'ldct_nifti_filename',
    'start_at',
    'stop_after',
    'output_series_description',
)

# The LDCT service's job states. Anything else is treated as still in flight.
_TERMINAL_OK = 'completed'
_TERMINAL_FAILED = 'failed'

# Dose levels offered as the batch tool's `mode` (see _batch_tool_spec in
# routes.py). The interactive route takes an arbitrary `f`; a batch needs a
# small fixed vocabulary instead, since `mode` is what identifies an entry in
# a job's params and on disk. Selecting several of these in one job runs each
# dose level against every study, exactly like several BraTS segmenters.
LDCT_DOSE_MODES = {
    'dose10': 0.10,
    'dose25': 0.25,
    'dose50': 0.50,
    'dose75': 0.75,
}

# Batch runs stop here rather than running the full pipeline: step8_4 is the
# DICOMweb upload, and a cohort staged out of iRODS has no business being
# STOWed into the clinical archive -- unlike the interactive route, where
# uploading the simulated series back into the study being viewed is the
# entire point. upload_to_dicomweb=False below says the same thing a second
# way, so neither alone has to be load-bearing.
BATCH_STOP_AFTER_STEP = 'step8_3'

DEFAULT_BETA = 0.5

# What gets copied out of a finished run into the batch result tree. The
# service's own output dir also holds ~1.5 GB of per-case intermediates
# (sinograms, noise volumes, the source CT as .npy) plus a duplicate of the
# simulated volume as .npy/.nii.gz; the DICOM series is the product LDCT
# exists to produce and is losslessly convertible to either, so shipping the
# rest would multiply a cohort-sized download for no added information.
_RESULT_DIR = 'dicom_output'


def _json_or_none(response):
    try:
        return response.json()
    except ValueError:
        return None


def _await_remote_job(remote_job_id, output_dir=None):
    """_poll_remote_job, with the service's own log for this run streamed
    into this job's log meanwhile. The service writes it as
    ldct_simulation_<its job id>.log into the output_dir its submit response
    reports, which is on the API_storage mount both containers share -- so
    it can be followed straight off disk, no change to the service needed."""
    if not output_dir:
        return _poll_remote_job(remote_job_id)
    service_log = os.path.join(output_dir, f'ldct_simulation_{remote_job_id}.log')
    with follow_file(service_log, name='ldct_service'):
        return _poll_remote_job(remote_job_id)


def _poll_remote_job(remote_job_id):
    """Block until the LDCT service's job reaches a terminal state.

    Returns its success message, or raises RuntimeError with the service's own
    error text -- which is what jobs.run_in_background turns into the red toast
    the user sees.

    Transient network blips and 5xx responses are tolerated (the pipeline runs
    for tens of minutes; a single failed poll says nothing about the job), but
    a 404 is terminal: the service keeps jobs in memory only, so an unknown id
    means it restarted and the run is gone.
    """
    status_url = f'{LDCT_SERVICE_URL}/ldct/status/{remote_job_id}'
    deadline = time.time() + POLL_TIMEOUT_SECONDS
    last_step = None

    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)

        try:
            response = requests.get(status_url, timeout=(5, 30))
        except requests.RequestException as e:
            log(f"LDCT status poll for job {remote_job_id} failed, retrying: {e}")
            continue

        if response.status_code == 404:
            raise RuntimeError(
                f"The LDCT service no longer knows job {remote_job_id}; it likely "
                "restarted mid-run. The simulation has to be started again."
            )

        body = _json_or_none(response)
        if response.status_code >= 500 or body is None:
            log(f"LDCT status poll for job {remote_job_id} returned "
                f"{response.status_code}, retrying")
            continue

        status = body.get('status')

        if status == _TERMINAL_OK:
            return (
                body.get('message')
                or 'LDCT simulation completed and the simulated series was uploaded.'
            )

        if status == _TERMINAL_FAILED:
            raise RuntimeError(
                body.get('error') or body.get('message') or 'The LDCT simulation failed.'
            )

        # pending / running: log each new pipeline step once, so the server log
        # shows where a long run has got to without spamming a line per poll.
        current_step = body.get('current_step')
        if current_step and current_step != last_step:
            last_step = current_step
            log(f"LDCT job {remote_job_id}: {body.get('message') or current_step} "
                f"({body.get('progress', 0):.0f}%)")

    raise RuntimeError(
        f"The LDCT simulation was still running after "
        f"{POLL_TIMEOUT_SECONDS // 60} minutes and was given up on. It may still "
        "finish on the LDCT service -- check there before starting it again."
    )


def _submit(payload):
    """POST a simulation request and return the service's accepted job
    descriptor ({job_id, series_description, output_dir, ...}), raising
    RuntimeError with the service's own error text if it refuses."""
    try:
        response = requests.post(
            f'{LDCT_SERVICE_URL}/ldct/simulate', json=payload, timeout=(5, 60)
        )
    except requests.RequestException as e:
        raise RuntimeError(
            f"Could not reach the LDCT service at {LDCT_SERVICE_URL}: {e}. "
            "Is the ldct_api container running?"
        )

    body = _json_or_none(response)

    if response.status_code >= 400:
        raise RuntimeError(
            (body or {}).get('error')
            or f"The LDCT service rejected the request ({response.status_code})."
        )

    if not (body or {}).get('job_id'):
        raise RuntimeError("The LDCT service accepted the request but returned no job id.")

    log(f"LDCT service accepted the simulation as job {body['job_id']}")
    return body


def ldct_wrapper(studyInstanceUID, seriesInstanceUID, params):
    """Simulate a low-dose acquisition of `seriesInstanceUID` and wait for it.

    This is what the POST /ldct/<studyInstanceUID> route calls. The simulated
    series is uploaded to dcm4chee by the LDCT service's own final pipeline
    step (step8_4), into the same study as the source series -- nothing is
    uploaded from here.
    """
    payload = {
        'study_uid': studyInstanceUID,
        'series_uid': seriesInstanceUID,
        **{key: params[key] for key in SIMULATION_PARAMS if key in params},
    }

    log(f"Submitting LDCT simulation for series {seriesInstanceUID} "
        f"(study {studyInstanceUID}) to {LDCT_SERVICE_URL}")

    job = _submit(payload)
    return _await_remote_job(job['job_id'], job.get('output_dir'))


### ---------------------------------------------------------------------- ###
### batch entry point                                                       ###
### ---------------------------------------------------------------------- ###

def ldct_core(dicom_dir, out_dir, dose_factor, gpu_index=0, beta=DEFAULT_BETA,
              case_label=''):
    """Simulate a low-dose acquisition of an already-staged DICOM series
    folder and leave the simulated series in `out_dir`. No dcm4chee access
    at all -- the batch equivalent of the *_core() functions the other tools
    expose, and what _run_ldct_tool in routes.py calls.

    Unlike those, the work does not happen in this process: the pipeline
    lives in the ldct_api container's own image (its own conda env), so this
    drives it over HTTP the same way fsl_registration_functions.py drives
    interact-gui's FSL. Both containers bind-mount API_storage at the same
    host path, which is what lets `dicom_dir` -- a path inside this job's
    work dir -- be read straight from over there with no transfer.

    The service decides where its own output goes (BASE_STORAGE_DIR keyed by
    the series description, see build_config in its blueprint.py); there is
    no output_dir parameter to point at `out_dir` directly. So each case gets
    a unique description -- which is also what the simulated series is named
    -- the products are copied out of the directory the submit response
    reports, and that directory is then removed. Removing it is not optional
    housekeeping: a run leaves ~1.5 GB of intermediates behind, so a
    100-case cohort would otherwise fill the mount.

    Returns {'dicom_dir', 'source_name', 'series_description', 'slices',
    'dose_factor', 'beta'} for ldct_batch_stats().
    """
    # Unique per call: the description picks the service's output directory,
    # and two cases (or two dose levels of one case) landing in the same one
    # would interleave their intermediates. The suffix survives the service's
    # own sanitizer, which keeps alphanumerics and underscores.
    percent = f'{round(dose_factor * 100)}pct'
    series_description = '_'.join(
        part for part in ('LDCT', percent, case_label, uuid.uuid4().hex[:8]) if part
    )

    payload = {
        'dicom_folder': str(dicom_dir),
        'f': dose_factor,
        'beta': beta,
        'use_gpu': True,
        'gpu_index': gpu_index,
        'output_series_description': series_description,
        'stop_after': BATCH_STOP_AFTER_STEP,
        'upload_to_dicomweb': False,
    }

    log(f"Batch LDCT ({percent}): submitting {dicom_dir}")
    job = _submit(payload)
    service_output_dir = job.get('output_dir')

    try:
        _await_remote_job(job['job_id'], service_output_dir)

        if not service_output_dir or not os.path.isdir(service_output_dir):
            raise RuntimeError(
                f"The LDCT service reported success but its output directory "
                f"({service_output_dir}) is not there."
            )

        produced = os.path.join(service_output_dir, _RESULT_DIR)
        if not os.path.isdir(produced):
            raise RuntimeError(
                f"The LDCT run produced no {_RESULT_DIR}/ directory in {service_output_dir}."
            )

        dest = os.path.join(out_dir, _RESULT_DIR)
        os.makedirs(out_dir, exist_ok=True)
        shutil.copytree(produced, dest, dirs_exist_ok=True)

        for name in os.listdir(service_output_dir):
            if name.endswith('.log'):
                shutil.copyfile(
                    os.path.join(service_output_dir, name), os.path.join(out_dir, name)
                )

        slices = len([n for n in os.listdir(dest) if n.endswith('.dcm')])
        if not slices:
            raise RuntimeError("The LDCT run produced no DICOM slices.")
    finally:
        # Always, including on failure: a failed run has usually still
        # written most of its multi-GB intermediates.
        if service_output_dir:
            shutil.rmtree(service_output_dir, ignore_errors=True)

    log(f"Batch LDCT ({percent}): {slices} simulated slice(s) -> {dest}")
    return {
        'dicom_dir': dest,
        'source_name': os.path.basename(str(dicom_dir).rstrip('/')),
        'series_description': series_description,
        'slices': slices,
        'dose_factor': dose_factor,
        'beta': beta,
    }


def ldct_batch_stats(results, failed=0):
    """Flat {metric: value} row for results_overview.csv, from the
    ldct_core() results of every CT series in one case -- mirrors
    fastsurfer_batch_stats/brats_batch_stats.

    There is nothing quantitative to measure here the way there is for a
    segmentation (LDCT produces an image, not a measurement), so this records
    what was simulated and how much of it. Deliberately a fixed set of
    columns rather than one per series: series names differ from study to
    study, so per-series columns would make the cohort's overview CSV as wide
    as the union of every study's series list and almost entirely empty. The
    per-series breakdown is in the manifest instead (each simulated series is
    its own output folder).
    """
    return {
        'dose_factor': results[0]['dose_factor'],
        'beta': results[0]['beta'],
        'series_simulated': len(results),
        'series_failed': failed,
        'slices_total': sum(r['slices'] for r in results),
        'source_series': '; '.join(r['source_name'] for r in results),
    }
