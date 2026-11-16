# How to add a new tool

This guide walks through adding an image-processing tool to InterACT, in three
places it can show up:

- **a)** a button in the OHIF viewer's "Webhooks" toolbar dropdown, which runs
  the tool on the open study and writes the results back into it as DICOM;
- **b)** the cohort batch pipeline, which runs it over every study in a
  `patient/study/series` folder tree;
- **c)** the portal GUI, either as a card on the Batch Processing page or as a
  page of its own.

All three call the same Python code in the processing API (`API/app/`), so
[section 1](#1-the-api-foundation-shared-by-a-b-and-c) is the foundation for
every case. **FastSurfer** is the worked example throughout: it is present in
all three places and was the pilot for the code layout described here. Read
`API/app/fastsurfer_functions.py` alongside this guide.

Background on the components: [API_and_API_storage.md](API_and_API_storage.md),
[OHIF_Viewer_and_DCM4CHEE.md](OHIF_Viewer_and_DCM4CHEE.md), [GUI.md](GUI.md).

---

## 0. How the pieces connect

```
OHIF (:1081, behind oauth2-proxy)
  └─ toolbar button  id: 'fastsurfer/seg'
       └─ fetch {PORTAL}/api-proxy/fastsurfer/seg/<StudyUID>      credentials: 'include'
            └─ GUI  api_proxy.py   Keycloak session check, ALLOWED_PREFIXES, PHI audit
                 └─ http://ohif_api:9200/fastsurfer/seg/<StudyUID>   (Docker network)
                      └─ routes.py  fastsurfer_route → start_bg_job → thread:
                           fastsurfer_wrapper(studyUID, mode)
                             stage-in   store_required_series_as_nifti  (dcm4chee → NIfTI)
                             core       fastsurfer_core(input_dir, output_dir, ...)
                             stage-out  upload_segmentation / upload_dataframe_as_sc  (→ dcm4chee)
       ← 202 {job_id, status_url}; OHIF polls /api-proxy/job_status/<id> every 4 s
       ← status "done" → OHIF reloads the study's series; the new series appear

GUI Batch Processing page
  └─ POST /batch/submit  → API POST /batch/run  tools=[{"tool":"fastsurfer","mode":"seg"}, ...]
       └─ batch_jobs worker pool → _run_batch_case → TOOL_RUNNERS['fastsurfer']
            └─ _run_fastsurfer_tool → fastsurfer_core → save_fastsurfer_core_outputs
                                    → fastsurfer_batch_stats   (→ results_overview.csv)
```

The **core** function is shared. The per-study path wraps it with dcm4chee
stage-in/stage-out; the batch path wraps it with folder-based input and
plain-file output. OHIF's nginx does not proxy to the API. Every browser
call goes through the portal's `/api-proxy`, because the API has no
authentication of its own and is bound to `127.0.0.1` only.

### Master checklist

| # | File | What to add | Needed for |
|---|---|---|---|
| 1 | `API/app/mytool_functions.py` (new) | `mytool_core`, `mytool_wrapper`; for batch also `save_mytool_core_outputs`, `mytool_batch_stats` | a, b |
| 2 | `API/app/routes.py` | import; per-study route via `start_bg_job` | a |
| 3 | `API/app/routes.py` | `BATCH_TOOL_ROLES`, `_batch_tool_spec` branch or `SINGLE_MODE_TOOLS`, `_run_mytool_tool`, `TOOL_RUNNERS` (optionally `ALL_MATCHING_EXCLUDED_TOOLS`) | b |
| 4 | `API/app/batch_io.py` | `TOOL_MODALITY` (optionally `TOOL_INPUT_KIND`, `TOOL_SERIES_SELECTION`) | b |
| 5 | `API/app/backend_helpers.py` + GUI `routes/batch.py` | new series role in `SERIES_CRITERIA`, mirrored in `SERIES_ROLE_INFO` (only if no existing role fits) | a, b |
| 6 | `API/app/storage_cleanup.py` | output root in `SWEPT_ROOTS` or `NEVER_TOUCH` | a |
| 7 | `docker-context-gui/app/routes/api_proxy.py` | `"mytool/"` in `ALLOWED_PREFIXES` (and `_ACTIONS` if not a plain view) | a, c (browser calls) |
| 8 | `viewers/platform/app/public/config/docker-nginx-dcm4chee.js` | entry in `studyWebhooks.functions`; rebuild `ohif_viewer` | a |
| 9 | `viewers/extensions/study-webhooks/src/icons/` + `index.ts` | optional new icon | a |
| 10 | `docker-context-gui/app/routes/batch.py` | `BATCH_TOOLS` entry + `BATCH_TOOL_LAYOUT` placement | b → c |
| 11 | compose / `.env.example` | only for host software, licences, weights, or a separate service | as needed |

---

## 1. The API foundation (shared by a, b and c)

### 1.1 Module layout: the "core contract"

Create `API/app/mytool_functions.py`. Each batch-capable tool exposes four
functions. A tool that will only ever have an OHIF button needs just
`mytool_core` and `mytool_wrapper`, but keeping the split costs nothing and
makes the tool batch-ready later.

| Function | FastSurfer signature | Contract |
|---|---|---|
| `<tool>_core` | `fastsurfer_core(input_dir, output_dir, mode='full', gpu_index=None, include_renders=True) -> dict` | **Local files only, no dcm4chee.** `input_dir` holds one `<role>.nii.gz` per required series role (here `t1n.nii.gz`). Returns a dict of products (arrays, DataFrames, paths). Accepts `gpu_index` and resolves it with `choose_gpu_index(gpu_index)` from `backend_helpers.py`, where `None` means "pick the emptiest GPU". |
| `<tool>_batch_stats` | `fastsurfer_batch_stats(result, mode) -> dict[str, float]` | Flat `{metric: number}` built from the core's result. Becomes columns `<tool>__<mode>__<metric>` in the batch job's `results_overview.csv`. |
| `save_<tool>_core_outputs` | `save_fastsurfer_core_outputs(result, input_dir, mode, out_dir)` | Writes plain NIfTI / CSV / PNG into `out_dir`. No DICOM, because a batch case has no dcm4chee study to attach to. |
| `<tool>_wrapper` | `fastsurfer_wrapper(studyInstanceUID, mode='full')` | Thin per-study adapter: **stage-in** → `<tool>_core` → **stage-out** (see 1.4). Raise an exception with an actionable message on failure; the text becomes the red toast in OHIF. |

Skeleton:

```python
import os
from .backend_helpers import (
    client, log, choose_gpu_index, store_required_series_as_nifti,
    upload_segmentation, upload_dataframe_as_sc,
)
from .job_logging import run_logged
from .storage_paths import API_STORAGE_DIR

MYTOOL_STORAGE_ROOT = f'{API_STORAGE_DIR}/mytool'
MYTOOL_IMAGE = 'vendor/mytool:1.2.3'


def mytool_core(input_dir, output_dir, mode='default', gpu_index=None):
    """Run MyTool on <input_dir>/t1n.nii.gz; returns the label map and a stats table."""
    os.makedirs(output_dir, exist_ok=True)
    gpu = choose_gpu_index(gpu_index)            # None if nvidia-smi can't be read
    run_logged([
        'docker', 'run', '--rm', '--gpus', 'all' if gpu is None else f'device={gpu}',
        '--user', f'{os.getuid()}:{os.getgid()}',
        '-v', f'{input_dir}:/input:ro', '-v', f'{output_dir}:/output',
        MYTOOL_IMAGE, '--in', '/input/t1n.nii.gz', '--out', '/output',
    ], name='mytool', check=True)
    ...  # load outputs
    return {'segmentation': seg, 'labels': labels, 'values': values, 'table': df}


def mytool_batch_stats(result):
    return {f'{r.Region}_mm3': float(r.Volume) for r in result['table'].itertuples()}


def save_mytool_core_outputs(result, input_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    ...  # nib.save(...), result['table'].to_csv(...)


def mytool_wrapper(studyInstanceUID):
    study_dir = os.path.join(MYTOOL_STORAGE_ROOT, studyInstanceUID)
    uids = store_required_series_as_nifti(studyInstanceUID, ['t1n'], study_dir)
    if not uids or 't1n' not in uids:
        raise ValueError(f"Study {studyInstanceUID} has no T1 series MyTool can use.")
    result = mytool_core(study_dir, os.path.join(study_dir, 'output'))
    reference = client.retrieve_series(study_instance_uid=studyInstanceUID,
                                       series_instance_uid=uids['t1n'])[0]
    upload_segmentation(result['segmentation'], labels=result['labels'], values=result['values'],
                        study_uid=studyInstanceUID, series_uid=uids['t1n'],
                        seg_description='MyTool segmentation', seg_series_number=6600)
    upload_dataframe_as_sc(result['table'], title='MyTool volumes', study_uid=studyInstanceUID,
                           reference_instance=reference, series_description='MyTool volumes',
                           series_number=6601)
```

### 1.2 Running the actual tool

The API container mounts the host Docker socket (`/var/run/docker.sock`) and
binary, so a tool shipped as a Docker image runs as a **sibling container**
on the host, not inside the API container. The current tools do it three
ways:

| Pattern | Example | How |
|---|---|---|
| `docker run` via subprocess | FastSurfer, FastSurfer-long | `run_logged(['docker', 'run', '--rm', '--gpus', f'device={idx}', ...], name='fastsurfer')` |
| docker-py | BraTS (via the `brats` package), screw inference | `run_container_logged(docker_client, name='screw', image=..., volumes={host: {'bind': ..., 'mode': 'ro'}}, ...)` |
| In-process / host binaries | TotalSegmentator (Python package), SynthSeg (`mri_synthseg` from host FreeSurfer), FSL FIRST/FLIRT/SIENA (host FSL) | `run_logged([...], name=...)` |
| HTTP to another service | LDCT (`ldct_functions.py` → `http://ldct_api:8000`) | `requests` + polling; see [LDCT.md](LDCT.md) |

Rules that every one of these has to follow:

- **Paths handed to a sibling container must live under `API_STORAGE_DIR`.**
  It is bind-mounted at the *same path* inside the API container and on the
  host, so a path the API resolves is also valid for the Docker daemon. Import
  it with `from .storage_paths import API_STORAGE_DIR` and never hardcode an
  absolute path.
- **Never use `/tmp`.** `TMPDIR` is set to `${API_STORAGE_DIR}/tmp` for this
  reason; `tempfile` already honours it.
- **No `:` in any path you bind-mount.** `docker run -v host:container:ro` is
  colon-delimited. (Batch entry ids are `tool__mode`, with a double
  underscore, for exactly this reason.)
- **Pin the GPU.** Accept `gpu_index` and pass `choose_gpu_index(gpu_index)`
  through, as `--gpus device=<idx>`, `CUDA_VISIBLE_DEVICES`, or the tool's
  own device flag (TotalSegmentator: `--device gpu:<idx>`). With `--gpus all`
  every concurrent batch worker would land on the same card.
- **Log through `job_logging`.** Use `run_logged(cmd, name='mytool', ...)` in
  place of `subprocess.run(..., capture_output=True)` and
  `run_container_logged(...)` for docker-py. Both stream the tool's output
  live into the job's own log file, which admins read on the GUI's Job Logs
  page. Use `log()` from `backend_helpers` for your own messages. If you
  start a thread inside a job, wrap its target with
  `job_logging.propagate_context(fn)`, or its output will not reach the job
  log.

### 1.3 Which series the tool gets: series roles

`store_required_series_as_nifti(studyUID, ['t1n'], dir)` (per-study) and the
batch matcher both choose series by **role**. Roles are defined in
`SERIES_CRITERIA` in `API/app/backend_helpers.py`:

```python
't1n': {'modality': 'MR', 'must_contain': [...], 'should_contain': [...],
        'mustnt_contain': [...], 'or_contain': [...]},
```

`score_series_for_criteria()` filters on modality first, then scores keywords
against `SeriesDescription` (per-study) or the series folder name (batch).
Reuse an existing role wherever you can (`t1n`, `t1c`, `t2`, `flair`, `ct`,
`ct_tissue`, `ct_screw`, `tof`, …). If you add a new one:

1. add it to `SERIES_CRITERIA` in `API/app/backend_helpers.py`. This is the
   authoritative copy;
2. mirror it in `SERIES_ROLE_INFO` in `docker-context-gui/app/routes/batch.py`.
   That copy only feeds the "required series" hover cards on the Batch page,
   but users rely on it to name their folders correctly.

### 1.4 Writing results back to dcm4chee

The wrapper writes into the **same study** the user is looking at. Helpers
in `API/app/backend_helpers.py` (DICOMweb client `client`, pointing at
`http://arc:8080/dcm4chee-arc/aets/DCM4CHEE/rs`):

| Helper | Produces |
|---|---|
| `upload_segmentation(segmentation, labels, values, series_uid, study_uid, seg_description, seg_series_number, ...)` | DICOM SEG referencing the source series (highdicom) |
| `upload_dataframe_as_sc(df, title, study_uid, reference_instance, series_description, series_number, highlight_rows=None, highlight_note=None)` | a table rendered as a Secondary Capture image |
| `upload_images_as_sc(pixel_arrays, study_uid, reference_instance, series_description, series_number)` | arbitrary images (plots, renders) as Secondary Capture |
| `nifti_to_dicom.build_and_store_dicom_series_from_volume(...)` | a real image series from a NIfTI volume, with headers copied from a reference series (used by co-registration) |

**Series numbers.** Pick an unused block so new series sort predictably in
OHIF. Blocks currently in use (grep `series_number=` and `seg_series_number=`
under `API/app/` before choosing):

| Block | Tool |
|---|---|
| 1234+ | co-registration (one per moving series) |
| 5000 | default for `upload_segmentation` and the SAMinRAD DICOM SEG upload |
| 5001–5003 | hippocampus (stats, coronal QC, links) |
| 5100–5103 | SIENA / SIENAX |
| 5100–5104, 5110, 5130 | DTI (overlaps SIENA / SIENAX) |
| 6000–6004 | FastSurfer |
| 6100 | FSL registration |
| 6100–6104 | FastSurfer longitudinal (overlaps FSL registration) |
| 6200 | TotalSegmentator volume table |
| 6300 | BraTS volume table |
| 6400–6401 | Screw inference |
| 6500+ | SynthSeg |
| 7000–7001 | hippocampus, reoriented for 3D Slicer |

### 1.5 The per-study route

In `API/app/routes.py`, import your wrapper and add a route that hands it to
`start_bg_job`:

```python
from .mytool_functions import (
    mytool_wrapper, mytool_core, mytool_batch_stats, save_mytool_core_outputs,
)

@main.route('/mytool/<studyInstanceUID>', methods=['GET'])
def mytool_route(studyInstanceUID):
    """Start MyTool in the background and return a job id."""
    work = logged_work(
        f"MyTool for study {studyInstanceUID}",
        lambda: mytool_wrapper(studyInstanceUID)
        or f"MyTool completed and uploaded for study {studyInstanceUID}.",
    )
    return start_bg_job(kind="mytool", work=work,
                        message=f"MyTool started for study {studyInstanceUID}.")
```

- **The StudyInstanceUID must be the last path segment**, because OHIF builds
  the URL as `<baseUrl>/<id>/<StudyInstanceUID>`. A mode goes in front of it:
  `/fastsurfer/<mode>/<studyInstanceUID>`.
- **Name the parameter `studyInstanceUID`.** `jobs._request_meta()` reads it
  from the URL to tag the job log with the study.
- `start_bg_job` handles everything else: job registry, thread, per-job log
  file under `API_storage/logs/jobs/`, and the
  `202 {job_id, status, message, status_url}` response. `kind` becomes part
  of the log file name and the Job Logs filter.
- Single-study jobs are kept **in memory** (one-hour retention) and are lost
  on an API restart. That is acceptable for a per-study button; batch jobs
  are durable (section 3).
- A synchronous route (like `/download_series`) returns its response
  directly. Use `jobs.cors_json()` / `jobs.cors_file()`.

### 1.6 Storage and retention

Per-study output goes to `${API_STORAGE_DIR}/<tool>/<StudyInstanceUID>/`.
The daily retention sweep (`API/app/storage_cleanup.py`) only touches roots
it is explicitly told about:

- add `'mytool': 'uid'` to `SWEPT_ROOTS` if the outputs can be regenerated
  from DICOM. Children whose names look like DICOM UIDs and are older than
  `STORAGE_RETENTION_DAYS` are then deleted;
- add `'mytool_weights'` (or similar) to `NEVER_TOUCH` for model weights,
  atlases or anything stateful.

A root in neither list is never swept. The module's self-check refuses a
root that appears in both. Run `python3 -m app.test_storage_cleanup` (inside
the container) after editing.

### 1.7 Compose and environment

Usually **nothing**. The `ohif_api` service already has all GPUs reserved,
`shm_size: 8gb`, the Docker socket and the `API_STORAGE_DIR` mount. Otherwise:

| Your tool needs… | Do this |
|---|---|
| a Docker image | keep the tag as a module constant (`FASTSURFER_IMAGE`), or make it env-overridable like `SCREW_MODEL_IMAGE` (`API/docker-compose.yml` + `.env.example`) |
| host-installed software or a licence | add an env var + read-only mount in `API/docker-compose.yml`, following `FSL_DIR` / `FREESURFER_DIR` / `FS_LICENSE_DIR`, and document it in `.env.example`. If it is unset, fail with a message that names the variable |
| model weights | store under `API_STORAGE_DIR` (e.g. `TOTALSEG_HOME_DIR`) and add the directory to `NEVER_TOUCH` |
| internet on first run | nothing; the API already inherits `INTERACT_HTTP_PROXY` / `INTERACT_NO_PROXY` |
| its own long-running service | own directory with a `docker-compose.yml`, `include:` it in the root `docker-compose.yml`, attach to the `interact-dcm4chee` network (copy the network block verbatim from another stack file), give it a profile, and add its service name to `INTERACT_NO_PROXY`. See `LDCT_Alsaihati/` |

**Reloading.** `API/` is bind-mounted, so Python changes need only
`docker compose restart ohif_api`. Rebuild with
`docker compose up -d --build ohif_api` only when `requirements.txt` or
`API/Dockerfile` change.

### 1.8 Testing the API part on its own

The API listens on `127.0.0.1:${PORT_API}` (9200 by default) on the host:

```sh
curl -s http://127.0.0.1:9200/mytool/<StudyInstanceUID>
# {"job_id": "...", "status": "pending", "status_url": "/job_status/<id>", ...}
curl -s http://127.0.0.1:9200/job_status/<id>
docker compose logs -f ohif_api          # or API_storage/logs/jobs/<date>/*_mytool_<id>.log
```

---

## 2. a) Adding a button to the OHIF viewer

The viewer's pipeline buttons come from the **study-webhooks** extension
(`viewers/extensions/study-webhooks/`). It reads the `studyWebhooks` block of
the app config and registers every leaf entry as a toolbar button inside one
"Webhooks" dropdown, on every mode, through its `onModeEnter` hook. **No OHIF
mode file needs editing.**

### 2.1 Add the config entry

Edit `viewers/platform/app/public/config/docker-nginx-dcm4chee.js`, under
`studyWebhooks.functions`. Entries are either functions (leaves) or groups
with `items`, which render as flyout submenus and can be nested:

```js
studyWebhooks: {
  baseUrl: API_PROXY_BASE_URL,      // {PORTAL}/api-proxy -- do not change
  method: 'GET',
  functions: [
    {
      id: 'dementia', label: 'Dementia & Epilepsy', icon: 'hemi-icon',
      items: [
        { id: 'fastsurfer/seg', label: 'FastSurfer Segmentation', icon: 'surf-icon', modalities: ['MR'] },
        // new:
        { id: 'mytool', label: 'MyTool Volumetry', icon: 'brain-icon',
          modalities: ['MR'], pollTimeoutMinutes: 60 },
      ],
    },
    ...
```

Fields (schema: `viewers/extensions/study-webhooks/src/types.ts`):

| Field | Meaning |
|---|---|
| `id` | **The URL path.** The request goes to `` `${baseUrl}/${id}/${StudyInstanceUID}` ``, so `id: 'fastsurfer/seg'` calls `/api-proxy/fastsurfer/seg/<uid>`. Also used as the button id (`StudyWebhook-<id>`). A *group's* `id` is only a React key. |
| `label`, `tooltip` | Menu text. |
| `icon` | A registered icon name (see 2.2). Defaults to `external-link`. |
| `modalities` | Only enable the button when the study contains one of these modalities (`evaluate.modality.supported`). |
| `pollTimeoutMinutes` | How long to poll before reporting a timeout. **Default 20.** Set it for anything slower, or a healthy run is reported as failed. Existing values: FastSurfer full 60, SIENA 120, DTI 45/150, LDCT 60, FastSurfer-long 480. |
| `requiresSeriesSelection` | Opens a series picker; sends **POST** with JSON `{centerSeriesUID, movingSeriesUIDs}` (co-registration). |
| `requiresLdctParameters` | Opens the LDCT parameter dialog; sends **POST** with the simulation parameters. |
| `requiresMultipleStudies` | Hides the entry unless the patient has more than one study of `modalities[0]` (longitudinal tools). |

A button that needs a different kind of user input needs a new modal in
`StudyWebhooksDropdown.tsx` / `getCommandsModule.ts`, following
`SeriesSelectionModal.tsx` or `LDCTSimulationModal.tsx`.

### 2.2 Optional: a new icon

1. Add `viewers/extensions/study-webhooks/src/icons/MyIcon.tsx`. It is an
   SVG React component that uses `stroke="currentColor"` and spreads
   `{...props}`; copy `FiberIcon.tsx`.
2. Register it in `viewers/extensions/study-webhooks/src/index.ts`:
   ```ts
   import { MyIcon } from './icons/MyIcon';
   Icons.addIcon('my-icon', MyIcon);
   ```

### 2.3 Open the path in the portal's API proxy

In `docker-context-gui/app/routes/api_proxy.py`, add the route prefix to
`ALLOWED_PREFIXES`:

```python
ALLOWED_PREFIXES = (
    ...
    "mytool/",
)
```

**Without this the button gets a 404 from the portal.** The proxy exposes
nothing that is not listed. If the route downloads, exports or uploads data
(rather than computing and writing back into the study), also add it to
`_ACTIONS` so the PHI audit trail records the right action; unlisted paths
are audited as `view`. The GUI runs `flask run --no-reload`, so apply the
change with `docker compose restart interact-gui`. No rebuild is needed,
because `app/` is bind-mounted.

### 2.4 What the API must return

The extension's `sendStudyWebhook` command (`getCommandsModule.ts`) expects:

1. `202` with `{job_id, status_url, message}`. `start_bg_job` produces this.
   A plain `200` is treated as "already finished" (legacy synchronous mode).
2. `GET {baseUrl}/job_status/<id>` → `{status: pending|running|done|failed, message, error}`,
   polled every 4 s.
3. On `done`, the extension drops OHIF's cached metadata for the study and
   re-queries QIDO, retrying with 1/2/4/8 s back-off until the series count
   goes up (dcm4chee can acknowledge a STOW before QIDO sees it). On
   `failed`, `error` is shown as a red toast.

### 2.5 Rebuild and deploy the viewer

The config file is **baked into the image at build time** (the recipe's
`dockerfile` sets `APP_CONFIG=config/docker-nginx-dcm4chee.js` and runs
`pnpm run build`). A restart is not enough. From the repository root:

```sh
docker compose build ohif_viewer
docker compose up -d ohif_viewer
```

The build takes several minutes. Hard-reload the browser
(Ctrl+Shift+R) afterwards; OHIF registers a service worker.

### 2.6 Commit

`viewers/` is a git submodule. Commit the viewer change inside it, on the
branch `.gitmodules` names, then record the new viewer version in this
repository:

```sh
cd viewers
git commit -am "Add MyTool webhook button" && git push
cd .. && git add viewers && git commit -m "Bump viewers: MyTool button"
```

---

## 3. b) Adding the tool to batch processing

The batch pipeline (`API/app/batch_jobs.py`, `batch_io.py`, and the `/batch/*`
routes in `routes.py`) is tool-agnostic. A tool plugs in through a handful
of registries. **Do not edit** `_run_batch_case`, `_start_batch_job`,
`/batch/run`, `/batch/plan`, `batch_jobs.py` or `jobs.py`; they dispatch on
the registries below.

Every selection in a batch job is a **(tool, mode) pair**. The `mode` is
whatever second choice the tool offers (FastSurfer `seg`/`full`, a
TotalSegmentator task, a BraTS segmenter). A tool without a real choice
still declares one fixed mode.

### 3.1 Registries in `API/app/routes.py`

1. **Required roles**: `BATCH_TOOL_ROLES`:
   ```python
   BATCH_TOOL_ROLES = {
       'fastsurfer': ['t1n'],
       ...
       'mytool': ['t1n'],
   }
   ```
   (TotalSegmentator is the exception: its roles vary per task, via
   `TASK_REQUIRED_SERIES` / `TASK_MODALITY` in `total_segmentator_functions.py`.)

2. **Mode validation**: `_batch_tool_spec(tool, mode)` must return
   `(required_roles, tool_modality, label)` or raise `ValueError` with a
   user-facing message. Either add a branch:
   ```python
   if tool == 'mytool':
       if mode not in MYTOOL_MODES:          # constant exported by your module
           raise ValueError(f"Unknown MyTool mode '{mode}'.")
       return BATCH_TOOL_ROLES['mytool'], TOOL_MODALITY['mytool'], f"MyTool ({mode})"
   ```
   or, for a tool with only one variant, add it to `SINGLE_MODE_TOOLS`:
   ```python
   SINGLE_MODE_TOOLS = {
       ...
       'mytool': ('default', 'MyTool volumetry'),
   }
   ```

3. **Optional**: add the tool to `ALL_MATCHING_EXCLUDED_TOOLS` if it needs
   several roles *at once* (like BraTS) or picks series from other studies
   (like the longitudinal tools). These tools ignore the form's "use every
   matching series" checkbox.

4. **The runner**: one function per tool, with signature
   `(job, case_id, entry_id, out_dir, gpu_index) -> stats dict`. Copy
   `_run_fastsurfer_tool`:
   ```python
   def _run_mytool_tool(job, case_id, entry_id, out_dir, gpu_index):
       tool_input = _require_tool_input(job, case_id, entry_id)   # raises → entry "skipped"
       mode = job['params']['tools'][entry_id]['mode']
       scratch = os.path.join(job['work_dir'], 'mytool_output', case_id, mode)  # key by mode, not entry_id

       if tool_input['match_sets']:                 # "use every matching series" was ticked
           def run_one(match, series_out_dir):
               result = mytool_core(match['work_dir'], os.path.join(scratch, match['label']),
                                    mode=mode, gpu_index=gpu_index)
               save_mytool_core_outputs(result, match['work_dir'], series_out_dir)
               return mytool_batch_stats(result)
           return _run_over_matches(tool_input['match_sets'], out_dir, run_one)

       result = mytool_core(tool_input['work_dir'], scratch, mode=mode, gpu_index=gpu_index)
       save_mytool_core_outputs(result, tool_input['work_dir'], out_dir)
       return mytool_batch_stats(result)
   ```
   - `_require_tool_input` performs the lazy dcm2niix conversion and raises
     `_MissingRequiredSeriesError` when the study has no matching series. The
     entry is then recorded as **skipped**, not failed, and the case's other
     tools carry on.
   - `tool_input['work_dir']` contains `<role>.nii.gz` for every required
     role. `tool_input['role_paths'][role]` gives individual paths.
   - Any other exception marks this entry **failed** for this case only.

5. **Dispatch**: add the runner to `TOOL_RUNNERS`:
   ```python
   TOOL_RUNNERS = {
       'fastsurfer': _run_fastsurfer_tool,
       ...
       'mytool': _run_mytool_tool,
   }
   ```

### 3.2 Registries in `API/app/batch_io.py`

- **`TOOL_MODALITY`** (required): `'mytool': 'MR'`. NIfTI series folders
  carry no DICOM header, so this is the modality they are assumed to have
  when matching roles.
- **`TOOL_INPUT_KIND`** (optional): `'dicom'` if the tool needs the
  original DICOM folder instead of a NIfTI. Only LDCT does.
- **`TOOL_SERIES_SELECTION`** (optional): `'every'` to run once per
  series of the modality, with no role matching (LDCT).

### 3.3 What happens at run time (for orientation)

1. The GUI stages the cohort into `API_storage/batch_inputs/<stage_id>/`
   (an upload, or a download from iRODS that fetches only the series that
   `/batch/plan` says the selected tools will open), then POSTs
   `/batch/run` with `input_path` and `tools`.
2. `batch_io.prepare_batch_for_tools` walks the
   `patient_id/study/series/` tree and role-matches series per (tool, mode)
   entry.
3. `batch_jobs` persists the job as `API_storage/batch_jobs/<job_id>.json`
   and runs cases through `BATCH_JOB_CONCURRENCY` worker threads. Slot *i*
   gets GPU `i % BATCH_JOB_NUM_GPUS`, passed to your runner as `gpu_index`.
4. Each finished entry's `out_dir` is
   `batch_jobs/<job_id>/result/<patient>/<study>/<tool>/<mode>/`.
   `result/manifest.csv` (one row per case × entry) and
   `result/results_overview.csv` (your `*_batch_stats` columns) are written
   when the job finishes.
5. Logs: `API_storage/logs/batch/<job_id>/job.log` and
   `cases/<case_id>.log`.

### 3.4 Testing

Via the GUI page once section 4 is done, or directly against the API from the
host. The input tree must be under `API_storage/batch_inputs/`:

```sh
mkdir -p API_storage/batch_inputs/test1/P001/S1/t1_mprage_tra_nativ && cp t1.nii.gz $_/
# which series would the tool open? (name-based, no data needed)
curl -s -X POST http://127.0.0.1:9200/batch/plan -H 'Content-Type: application/json' \
     -d '{"tools":[{"tool":"mytool","mode":"default"}],
          "studies":[{"patient_id":"P001","study_name":"S1","series":["t1_mprage_tra_nativ"]}]}'
curl -s -X POST http://127.0.0.1:9200/batch/run \
     -F input_path=$PWD/API_storage/batch_inputs/test1 \
     -F 'tools=[{"tool":"mytool","mode":"default"}]' -F owner=me
curl -s http://127.0.0.1:9200/batch/status/<job_id>
```

Unit tests live next to the code: `API/app/test_batch_io.py`,
`test_batch_jobs.py`. Run them in the container with
`docker compose exec ohif_api python3 -m app.test_batch_io`.

---

## 4. c) Adding the tool to the GUI

### 4.1 As a card on the Batch Processing page (the usual case)

Two registry edits in `docker-context-gui/app/routes/batch.py`. **No template
or JavaScript changes.** `batch.html` renders everything from these lists.

1. **`BATCH_TOOLS`**: what the tool is:
   ```python
   BATCH_TOOLS = [
       ...
       {
           "id": "mytool",                       # must equal the API's tool id
           "label": "MyTool",
           "description": "Volumetry from a T1 MPRAGE. ~5 min per study.",
           "enabled": True,
           "series_roles": ["t1n"],              # SERIES_ROLE_INFO keys, for the hover cards
           "modes": [                            # ids must equal what _batch_tool_spec accepts
               {"id": "default", "label": "Default"},
           ],
       },
   ]
   ```
   A mode can carry its own `"series_roles"` when the inputs differ per mode
   (see the TotalSegmentator entry). The GUI re-validates the submitted
   (tool, mode) pairs against this list before forwarding to the API.

2. **`BATCH_TOOL_LAYOUT`**: where the card appears (modality → clinical
   category):
   ```python
   {"label": "Volumetry & Atrophy",
    "entries": [{"id": "fastsurfer"}, {"id": "synthseg"}, {"id": "mytool"}, ...]},
   ```
   An entry may restrict the tool to some modes and rename it
   (`"key"`, `"modes"`, `"label"`, `"description"`). TotalSegmentator is split
   across three categories this way.

Apply with `docker compose restart interact-gui` (the GUI does not
auto-reload Python), then reload the page.

### 4.2 As a page of its own

Use this for a tool that needs an interactive UI of its own.

1. **Blueprint**: `docker-context-gui/app/routes/mytool.py`:
   ```python
   import os, requests
   from flask import Blueprint, render_template, request, jsonify
   from app.oidc_auth import oidc
   from app.utils.roles import admin_required

   mytool_bp = Blueprint("mytool", __name__, url_prefix="/mytool")
   API_BASE = os.environ.get("BATCH_API_BASE", "http://ohif_api:9200")

   @mytool_bp.route("/")
   @oidc.require_login            # or @admin_required for admin-only pages
   def index():
       return render_template("mytool.html")

   @mytool_bp.route("/run/<study_uid>", methods=["POST"])
   @oidc.require_login
   def run(study_uid):
       r = requests.get(f"{API_BASE}/mytool/{study_uid}", timeout=30)
       return jsonify(r.json()), r.status_code
   ```
   Server-side calls reach the API by service name (`http://ohif_api:9200`)
   over the `interact-dcm4chee` network; `127.0.0.1:9200` is the *host's*
   loopback and is not reachable from the GUI container. A browser-side
   `fetch` must go through `/api-proxy/...` instead, which means adding the
   prefix to `ALLOWED_PREFIXES` (2.3).

   If the page touches patient data in a way the existing proxies don't
   already record, call `app.utils.audit` so it shows up in the PHI trail
   (see [GUI.md](GUI.md#phi-access-audit-trail)).

2. **Register it** in `docker-context-gui/app/__init__.py`, next to the
   others:
   ```python
   from app.routes.mytool import mytool_bp
   ...
   app.register_blueprint(mytool_bp)
   ```

3. **Template**: `docker-context-gui/app/templates/mytool.html`:
   ```jinja
   {% extends "base.html" %}
   {% block content %} ... {% endblock %}
   ```
   `is_admin`, `username`, `embedded` and the public URLs (`PORTAL_BASE_URL`,
   `OHIF_BASE_URL`, …) are injected into every template. Build links from
   them; never write a hostname or port into a template.

4. **Sidebar entry**: in `docker-context-gui/app/templates/base.html`:
   ```html
   <li class="{% if request.endpoint == 'mytool.index' %}active{% endif %}">
     <a href="{{ url_for('mytool.index') }}"><i class="fa-solid fa-brain"></i> MyTool</a>
   </li>
   ```
   Put it inside the `{% if is_admin %}` block for admin-only pages. Hiding
   the link is cosmetic; the `@admin_required` decorator does the actual
   enforcement. "Admin" means the Keycloak `admin` group or realm role
   (`app/utils/roles.py`).

5. Restart the GUI so the new blueprint and template are loaded:
   `docker compose restart interact-gui`. It runs `flask run --no-reload`
   in production mode, so neither Python nor template edits are picked up
   until a restart.

### 4.3 Logs

Nothing to register. Jobs started through `start_bg_job` or the batch
pipeline appear automatically on the admin **Job Logs** page
(`/job-logs/`), filterable by the `kind` you passed.

---

## 5. Checklist for "mytool"

```
API
[ ] API/app/mytool_functions.py   core / wrapper / save_*_core_outputs / *_batch_stats
                                   MYTOOL_STORAGE_ROOT under API_STORAGE_DIR, run_logged(name=...),
                                   gpu_index via choose_gpu_index, free series-number block
[ ] API/app/routes.py             import; @main.route('/mytool/<studyInstanceUID>') → start_bg_job
[ ] API/app/routes.py             BATCH_TOOL_ROLES; _batch_tool_spec branch or SINGLE_MODE_TOOLS;
                                   _run_mytool_tool; TOOL_RUNNERS
[ ] API/app/batch_io.py           TOOL_MODALITY (+ TOOL_INPUT_KIND / TOOL_SERIES_SELECTION)
[ ] API/app/backend_helpers.py    SERIES_CRITERIA (only for a new role)
[ ] API/app/storage_cleanup.py    SWEPT_ROOTS or NEVER_TOUCH
[ ] docker compose restart ohif_api;  curl 127.0.0.1:9200/mytool/<uid>

GUI
[ ] routes/api_proxy.py           ALLOWED_PREFIXES += "mytool/"  (+ _ACTIONS if not a view)
[ ] routes/batch.py               BATCH_TOOLS, BATCH_TOOL_LAYOUT (+ SERIES_ROLE_INFO for a new role)
[ ] (own page)                    blueprint, __init__.py registration, template, base.html nav
[ ] docker compose restart interact-gui

OHIF
[ ] public/config/docker-nginx-dcm4chee.js   studyWebhooks entry (id, modalities, pollTimeoutMinutes)
[ ] (icon)                         src/icons/*.tsx + Icons.addIcon in study-webhooks/src/index.ts
[ ] docker compose build ohif_viewer && docker compose up -d ohif_viewer
[ ] commit in viewers/, bump the submodule pointer
```
