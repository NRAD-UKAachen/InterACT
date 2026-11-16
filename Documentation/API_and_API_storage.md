# Processing API and API_storage

The processing API (`API/`, Compose service **`ohif_api`**) is the Flask app
that does InterACT's imaging work. It runs the segmentation, volumetry,
registration and diffusion pipelines behind the OHIF toolbar buttons and the
GUI's Batch Processing page, and writes results back to dcm4chee as DICOM.
`API_storage/` is its working directory on the host.

To add a tool, see [How_To_Add_New_Tools.md](How_To_Add_New_Tools.md).

## At a glance

| | |
|---|---|
| Code | `API/app/` (Flask, dev server with `threaded=True`, `API/run.py`) |
| Service / container | `ohif_api` / `${STACK_NAME}-ohif_api` |
| Port | `127.0.0.1:${PORT_API}` → 9200, **loopback only** |
| Network | `interact-dcm4chee`. Reached as `http://ohif_api:9200` by the GUI, and talks to `arc:8080` (dcm4chee) and `ldct_api:8000` |
| Authentication | **none**. Browsers reach it only through the portal's authenticated `/api-proxy` (see [GUI.md](GUI.md#the-api-proxy)) |
| GPU | all host GPUs reserved; `shm_size: 8gb` |
| Image | `python:3.10-slim` + `docker.io`, `dcm2niix`, `bc`, `dc` (`API/Dockerfile`) |

### Running it

Start it from the **repository root**, like every InterACT service:

```sh
docker compose up -d ohif_api        # this service alone
docker compose restart ohif_api      # after editing anything under API/app/
docker compose up -d --build ohif_api   # after changing requirements.txt or the Dockerfile
docker compose logs -f ohif_api
```

`API/` is bind-mounted at `/app`, so code changes need only a restart.

### Prerequisites outside the repository

| Needed for | What | Set via |
|---|---|---|
| FastSurfer | a **FreeSurfer licence** (free, per user, must not be redistributed) | `FS_LICENSE_DIR`; see [secrets/freesurfer/README.md](../secrets/freesurfer/README.md) |
| SynthSeg | a host **FreeSurfer** install, 8.2 or later (also supplies the SynthSeg weights) | `FREESURFER_DIR` |
| Hippocampus, SIENA, FSL registration, DTI | a host **FSL** install | `FSL_DIR` |
| every GPU tool | NVIDIA Container Toolkit | – |
| TotalSegmentator `brain_structures` | a **TotalSegmentator licence key** (free for non-commercial use; `cerebral_bleed` and `brain_aneurysm` don't need one) | `TOTALSEG_LICENSE_DIR`; see [secrets/totalsegmentator/README.md](../secrets/totalsegmentator/README.md) |

All are bind-mounted read-only. If one is unset, the tools that need it fail
with a message naming the variable, and everything else keeps working.

### Mounts and environment (`API/docker-compose.yml`)

| Mount | Why |
|---|---|
| `.` → `/app` | live code |
| `${API_STORAGE_DIR}` → **same path** | sibling containers receive paths resolved here, and the Docker daemon resolves them on the host, so both sides must agree |
| `/var/run/docker.sock`, `/usr/bin/docker` | to launch sibling containers (FastSurfer, BraTS, screw model) |
| `${FSL_DIR}` → `/usr/local/fsl:ro`, `${FREESURFER_DIR}` → `/usr/local/freesurfer:ro`, `${FS_LICENSE_DIR}` → `/fs_license:ro`, `${TOTALSEG_LICENSE_DIR}` → `/totalseg_license:ro` | host tools and licences |
| `API_storage/brainles_atlases`, `brainles_synthstrip_weights` → site-packages | BraTS/coreg preprocessing assets (~120 MB from Zenodo), downloaded once in the background at API startup, resuming across failures and restarts (`app/brainles_assets.py`); jobs wait for it if it's still running |

| Variable | Default | Meaning |
|---|---|---|
| `API_STORAGE_DIR` | `${INTERACT_ROOT}/API_storage` | root of all outputs (`app/storage_paths.py`) |
| `TMPDIR` | `${API_STORAGE_DIR}/tmp` | scratch, so temporary files are visible to sibling containers |
| `TOTALSEG_HOME_DIR` | `${API_STORAGE_DIR}/totalsegmentator_weights` | TotalSegmentator weights and `config.json` (incl. licence) |
| `LOG_LEVEL` (`API_LOG_LEVEL` in `.env`) | `DEBUG` | log verbosity |
| `LOG_RETENTION_DAYS` | 30 | age-out for `API_storage/logs` |
| `STORAGE_RETENTION_DAYS` | 30 | age-out for per-study outputs (see [Retention](#retention-sweep)); `0` disables |
| `BATCH_JOB_CONCURRENCY` | 3 | batch worker threads |
| `BATCH_JOB_NUM_GPUS` | 1 | GPUs the batch slots are spread over (`slot % N`) |
| `SCREW_MODEL_IMAGE` | – | Docker image for screw inference |
| `API_PUBLIC_BASE_URL` | `${PORTAL_BASE_URL}/api-proxy` | browser-facing base for links the API writes into reports (hippocampus "open in 3D Slicer") |
| `LDCT_SERVICE_URL` | `http://ldct_api:8000` | LDCT service |
| `DCM4CHEE_RS` | `http://arc:8080/dcm4chee-arc/aets/DCM4CHEE/rs` (code default) | DICOMweb endpoint |

## How it is built

### Request model

Every processing route follows the same pattern: the route hands a `work()`
callable to `jobs.start_bg_job(kind, work, message)`. That registers a job,
runs `work` in a background thread with its own log file, and returns
immediately:

```json
202 {"job_id": "...", "status": "pending", "message": "...", "status_url": "/job_status/<id>"}
```

The caller polls `GET /job_status/<job_id>` until `status` is `done` or
`failed`. Pipelines run for minutes to hours, far longer than an HTTP
connection should be held open. Single-study jobs live **in memory**
(retained for one hour after finishing) and do not survive a restart. Batch
jobs are durable (below).

### The core contract

Each tool module `API/app/<tool>_functions.py` is split into three stages:

```
stage-in   store_required_series_as_nifti(studyUID, roles, dir)   dcm4chee → <role>.nii.gz
core       <tool>_core(input_dir, output_dir, ..., gpu_index)      local files only
stage-out  upload_segmentation / upload_dataframe_as_sc / ...      local → dcm4chee
```

`<tool>_wrapper(studyUID, ...)` is the per-study path (OHIF button).
The batch path calls the same `<tool>_core` plus `save_<tool>_core_outputs`
and `<tool>_batch_stats`. Details and a worked example:
[How_To_Add_New_Tools.md §1](How_To_Add_New_Tools.md#1-the-api-foundation-shared-by-a-b-and-c).

### Sibling containers

Several tools run as their own Docker images (FastSurfer
`deepmi/fastsurfer`, the BraTS algorithm images, the screw model). The API
starts them through the host's Docker socket, so they run **next to** the
API container, not inside it. This is why `API_STORAGE_DIR` must be the same
path on both sides, why scratch space lives in `API_storage/tmp` rather than
`/tmp`, and why the API container is effectively root-equivalent on the host
(see [Project_Structure.md](Project_Structure.md#security-model)).

### Choosing series: roles

Tools ask for series by **role** (`t1n`, `t1c`, `t2`, `flair`, `ct`,
`ct_screw`, `tof`, `dwi`, …). `SERIES_CRITERIA` in `backend_helpers.py` defines
each role as a modality plus `must_contain` / `should_contain` /
`mustnt_contain` / `or_contain` keyword lists. `score_series_for_criteria()`
matches them against `SeriesDescription` (per study) or the series folder name
(batch). The GUI keeps a display-only copy in `SERIES_ROLE_INFO`
(`docker-context-gui/app/routes/batch.py`) for its "required series" hints.

## Routes

| Route | Method | What it does |
|---|---|---|
| `/` | GET | landing page |
| `/job_status/<job_id>` | GET | poll a single-study background job |
| `/download_series/<study>/<series>` | GET | series as a ZIP of DICOM |
| `/download_series_nifti/<study>/<series>` | GET | series as one `.nii.gz` |
| `/tumorseg/<segmenter>/<study>` | GET | BraTS tumour segmentation (`goat`, `gliomapre`, `gliomaprepost`, `meningioma`, `metastases`, `pediatric`) |
| `/totalsegmentator/<task>/<study>` | GET | TotalSegmentator (`brain_structures`, `cerebral_bleed`, `brain_aneurysm`) |
| `/fastsurfer/<mode>/<study>` | GET | FastSurfer `seg` (~3 min) or `full` with surfaces (~30 min) |
| `/fastsurfer_long/<study>` | GET | FastSurfer longitudinal over the patient's MR studies (max 4 timepoints) |
| `/synthseg/<mode>/<study>` | GET | SynthSeg `standard` / `robust`, on whichever of T1/T2 exist |
| `/hippocampus/<study>` | GET | FSL FIRST hippocampus volumetry |
| `/hippocampus_reoriented/<study>` | GET | upload the reoriented (3D Slicer-ready) hippocampus series |
| `/hippocampus_files/<study>/<file>`, `/hippocampus_links/[<study>]` | GET | download hippocampus products / link page |
| `/siena/<study>` | GET | SIENA against the furthest earlier study, or SIENAX if there is none |
| `/fsl_register_qc/<study>` | GET | FLIRT to MNI + QC overlay |
| `/dti/tensor/<study>` | GET | DTI tensor fit (FA, MD, colour FA) |
| `/dti/tractography/<study>` | GET | bedpostx + XTRACT tractography |
| `/dti/reupload/<study>`, `/dti_files/<study>/<file>` | GET | re-upload DTI results / fetch DTI products (fibre viewer) |
| `/screw_infer/<study>` | GET | pedicle-screw segmentation (CT) |
| `/coreg/<study>` | POST | co-register `movingSeriesUIDs` onto `centerSeriesUID` |
| `/ldct/<study>` | POST | low-dose CT simulation via the LDCT service ([LDCT.md](LDCT.md)) |
| `/new_study_uid` | GET | mint a StudyInstanceUID (OHIF Upload button) |
| `/volume_upload` | POST | NIfTI/NRRD → DICOM series → STOW (`VOLUME_CONVERTERS`) |
| `/segmentation_upload/<study>/<series>` | POST | mask → DICOM SEG on that series (used by SAMinRAD) |
| `/batch/plan` | POST | which series a cohort's selected tools will open (name-based) |
| `/batch/run` | POST | start a batch job |
| `/batch/jobs[?owner=]` | GET | list batch jobs |
| `/batch/status/<id>` | GET | batch job progress, per case and per tool |
| `/batch/jobs/<id>` | DELETE | delete a batch job and its files |
| `/batch/jobs/<id>/pause`, `/resume`, `/stop` | POST | control a running batch (takes effect between cases) |
| `/batch/result/<id>`, `/batch/result/<id>/partial` | GET | result ZIP (whole job / what is finished so far) |

Only paths listed in the GUI's `ALLOWED_PREFIXES` can be reached from a
browser.

### Module map (`API/app/`)

| Area | Modules |
|---|---|
| Infrastructure | `__init__.py` (app factory, batch resume, retention thread, CORS `*`), `routes.py`, `jobs.py`, `batch_jobs.py`, `batch_io.py`, `job_logging.py`, `storage_paths.py`, `storage_cleanup.py` |
| DICOM I/O | `backend_helpers.py` (DICOMweb client, series roles, NIfTI stage-in, SEG/SC upload, GPU choice), `nifti_to_dicom.py`, `nifti_upload.py`, `nrrd_upload.py` |
| Segmentation | `brats_functions.py`, `total_segmentator_functions.py`, `screw_inference_functions.py`, `screw_functions.py` |
| Brain morphometry | `fastsurfer_functions.py`, `fastsurfer_long_functions.py`, `synthseg_functions.py`, `hippocampus_functions.py`, `siena_functions.py` |
| Registration | `coreg_functions.py`, `fsl_registration_core.py`, `fsl_registration_functions.py`, `apply_transform_3dslicer_style.py` |
| Diffusion | `dti_functions.py`, `dti_gradients.py`, `dti_streamlines.py` |
| Bridges | `ldct_functions.py` (to the LDCT service) |
| Tests | `test_batch_io.py`, `test_batch_jobs.py`, `test_storage_cleanup.py`. Run with `docker compose exec ohif_api python3 -m app.test_batch_io` (etc.) |

### Helper reference

- **`backend_helpers.py`**: `log()`; `choose_gpu_index()`;
  `retrieve_series_sorted`, `series_to_zip`, `series_to_nifti` (downloads);
  `SERIES_CRITERIA`, `score_series_for_criteria`, `find_required_seriesUIDs`
  (roles); `sort_instances_by_slice_normal` (non-axial series);
  `store_series_as_nifti`, `store_required_series_as_nifti` (stage-in with a
  correct affine); `upload_segmentation`, `upload_mask_file_as_segmentation`,
  `mask_array_for_series` (SEG); `upload_dataframe_as_sc`,
  `upload_images_as_sc` (Secondary Capture).
- **`jobs.py`**: `start_bg_job`, `logged_work`, `job_status_response`;
  `cors_json` / `cors_file` for synchronous routes.
- **`job_logging.py`**: `run_logged` (streamed `subprocess.run`
  replacement), `run_container_logged` (docker-py), `propagate_context`,
  `follow_file`, `timed`.
- **`nifti_to_dicom.py`**: `build_and_store_dicom_series_from_volume`.
- **`apply_transform_3dslicer_style.py`**: `TransformApplier.apply_transform`
  (ANTs/ITK `.mat` applied to a label map).

Docstring style: one to a few sentences on *what* the function does and,
only where the code doesn't already say it, *why*.

## Batch pipeline

The batch pipeline runs one or more tools over a whole cohort (for example
FastSurfer over 200 scans of a dementia cohort). It sits alongside the
per-study buttons and reuses each tool's `_core` function.

### Flow

1. **Input**, prepared by the GUI ([GUI.md](GUI.md#batch-processing)): a folder
   tree `patient_id/study_name/series_name/`, with DICOM or NIfTI per series
   folder. It comes from a browser upload or an **iRODS stage-in**, and lands in
   `API_storage/batch_inputs/<stage_id>/`. The GUI then POSTs `/batch/run`
   with `input_path` (confined to that root by
   `batch_io.resolve_staged_input_dir`) and `tools` (a JSON array of
   `{"tool", "mode"}`). A multipart ZIP upload is still accepted from callers
   without the shared mount.
2. **Planning**: `batch_io.prepare_batch_for_tools` walks the tree and
   role-matches series per (tool, mode) entry. dcm2niix conversion is
   deferred to the worker that needs it and cached job-wide in
   `scratch/_nifti_cache`.
3. **Execution**: `batch_jobs.py` persists the job as
   `API_storage/batch_jobs/<job_id>.json` (write-then-rename) and runs cases
   through `BATCH_JOB_CONCURRENCY` worker threads. Slot *i* is pinned to GPU
   `i % BATCH_JOB_NUM_GPUS`. Each case runs every selected entry in turn via
   `TOOL_RUNNERS`, and each entry ends as `done`, `skipped` (no matching
   series) or `failed`, independently of the others. A case is `done`,
   `partial` or `failed` overall.
4. **Output**: `batch_jobs/<job_id>/result/<patient>/<study>/<tool>/<mode>/`,
   plus `result/manifest.csv` (one row per case × entry: status, error,
   files) and `result/results_overview.csv` (one row per case, a column per
   `<tool>__<mode>__<metric>`). The user downloads a ZIP, or pushes the result
   tree to iRODS from the GUI.
5. **Durability**: at startup `batch_jobs.load_jobs_from_disk()` resets
   `running` cases to `pending` and resumes the job. Resume works per case:
   a case interrupted halfway reruns all of its entries. Pause, resume and
   stop take effect between cases.

Selections are always **(tool, mode) pairs**, keyed internally as
`entry_id = "<tool>__<mode>"` (double underscore, because it ends up in
bind-mounted paths and `:` would break `docker -v`). One job can run several
modes of one tool, for example several BraTS segmenters side by side.

Batch-capable tools: `fastsurfer`, `synthseg`, `totalsegmentator`, `brats`,
`ldct`, `fsl_register`, `hippocampus`, `screw_infer`, `fastsurfer_long`,
`siena`. (`screw_infer` currently has no card on the GUI's Batch page and no OHIF button; it is reachable only through the API.)

## Logging

`job_logging.py` routes everything to `API_storage/logs/`:

```
logs/api.log                                         every line, all threads
logs/jobs/<YYYY-MM-DD>/<HHMMSS>_<kind>_<job_id>.log  one per single-study job (+ .meta.json)
logs/batch/<job_id>/job.log                          all cases, interleaved
logs/batch/<job_id>/cases/<case_id>.log              one per case
logs/batch/<job_id>/meta.json
```

Routing works per thread. Tool subprocesses started with `run_logged` /
`run_container_logged` stream into the job's file live. Files older than
`LOG_RETENTION_DAYS` are pruned daily, except those of batch jobs that are
still active. Admins read these files on the GUI's **Job Logs** page, which
reads the shared mount directly.

## API_storage

`API_STORAGE_DIR` (default `<repo>/API_storage`, gitignored) is mounted into
the API, GUI, SAMinRAD and LDCT containers at the same path. Directories are
created on demand.

| Directory | Contents | Retention sweep |
|---|---|---|
| `<tool>/<StudyUID>/` (`fastsurfer`, `fastsurfer_long`, `synthseg`, `tumorseg`, `coreg`, `dti`, `siena`, `totalsegmentator`, `screw_inference`) | per-study working files and outputs | swept (`uid`) |
| `hippocampus/`, `fsl_registration/` | per-study outputs | **never** (held back) |
| `batch_inputs/<stage_id>/` | GUI → API cohort handoff | swept (`any`) |
| `batch_jobs/<id>.json`, `batch_jobs/<id>/` | durable batch registry and work dirs | only orphaned work dirs; jobs are deleted from the GUI |
| `tmp/` | `TMPDIR` scratch | swept (`any`) |
| `logs/` | API logs (see above) | own pruning (`LOG_RETENTION_DAYS`) |
| `totalsegmentator_weights/`, `brainles_atlases/`, `brainles_synthstrip_weights/`, `synthseg_cuda11/` | model weights and atlases | never |
| `federated/` | federated-learning instance registry and tokens ([Federated_Learning.md](Federated_Learning.md)) | never |
| `SAM_data/` | SAMinRAD uploads and archive ([SAM.md](SAM.md)) | never |
| `ldct_alsaihati/` | LDCT simulation runs ([LDCT.md](LDCT.md)) | never |

### Retention sweep

`API/app/storage_cleanup.py` runs once a day in a daemon thread started by
`create_app()`:

- Only roots listed in `SWEPT_ROOTS` are touched. Under a `'uid'` root, only
  children named like a DICOM UID (`^[0-9]+(\.[0-9]+)+$`) qualify. Under
  `'any'`, every child qualifies.
- A child is deleted when the **newest** mtime anywhere in its tree is older
  than `STORAGE_RETENTION_DAYS`. `0` disables the sweep.
- `NEVER_TOUCH` names what must never be swept. A self-check fails if the two
  lists overlap.
- Batch jobs are never aged out. Only work directories without a matching
  `.json` are collected.
- Results go to `logs/api.log`. There is deliberately no HTTP trigger,
  because the API has no auth. To run it by hand:
  `docker compose exec ohif_api python3 -m app.storage_cleanup`.

Everything the sweep deletes can be regenerated by re-running the tool on
the study in dcm4chee.
