# LDCT (low-dose CT simulation)

The LDCT service simulates a **reduced-dose CT acquisition** from a normal
full-dose CT series by adding image-based noise. No vendor sinograms are
needed. It implements the 14-step method of Alsaihati et al., *Development,
validation, and application of a generic image-based noise addition method
for simulating reduced dose computed tomography images*, Medical Physics
(2024), [doi:10.1002/mp.17444](https://doi.org/10.1002/mp.17444). Typical
uses are dose-reduction studies and testing how other tools cope with noisier
input.

The simulated series is written **back into the same study** in dcm4chee as a
new series, so it opens in OHIF next to the original.

## Where to use it

- **OHIF**: Webhooks → Research Tools → **Low Dose CT Simulation**
  (CT studies). A dialog asks for the CT series, the dose factor `f`, `beta`,
  the GPU, and optionally a step range and output series description. Runs
  take about 15–30 minutes for a chest CT; the button polls for up to 60
  minutes.
- **Batch Processing** (portal): tool **LDCT** under CT → Image Simulation /
  Research. Modes are fixed dose levels: `dose10`, `dose25`, `dose50`,
  `dose75` (10/25/50/75 % of the original dose). Several can be selected in
  one job. LDCT runs on **every CT series** of a study (no keyword matching),
  needs the original **DICOM** (a NIfTI-only series is skipped), and in batch
  mode stops after `step8_3`. The DICOMweb upload (`step8_4`) is left out,
  because a cohort staged from iRODS has no dcm4chee study to upload to.

## Architecture

```
OHIF ─► portal /api-proxy/ldct/<study> (POST, JSON parameters)
          └─► API  POST /ldct/<study>   (API/app/ldct_functions.py)
                 start_bg_job ─► POST http://ldct_api:8000/ldct/simulate  {study_uid, series_uid, f, beta, ...}
                                 poll   /ldct/status/<job_id> every 10 s (timeout 90 min)
                                       ldct_api: pull series from dcm4chee (DICOMweb)
                                                 → 14-step pipeline (GPU)
                                                 → STOW simulated series into the same study
          ◄── OHIF polls the API's /job_status/<id> as for any other tool
```

The LDCT service keeps its own job registry and status vocabulary.
`ldct_functions.py` wraps a remote run in a normal API background job, so OHIF
and the batch pipeline treat LDCT like any other tool. Only these parameters
are forwarded: `f`, `beta`, `use_gpu`, `gpu_index`, `ldct_numpy_filename`,
`ldct_nifti_filename`, `start_at`, `stop_after`, `output_series_description`.

## Deployment

Compose profile **`ldct`** (or `all`), from the repository root:

```sh
docker compose --profile ldct up -d --build
```

| | |
|---|---|
| Service / container | `ldct_api` / `${STACK_NAME}-ldct_api` |
| Image | built from `LDCT_Alsaihati/Dockerfile` → `${STACK_NAME}-ldct_api` |
| Internal URL | `http://ldct_api:8000` on `interact-dcm4chee` |
| Published port | `127.0.0.1:${PORT_LDCT}` (default 8001), loopback only, **no authentication** |
| GPU | all GPUs reserved; which one is used comes from the request, or `LDCT_GPU_INDEX` |
| Storage | `${API_STORAGE_DIR}` mounted at the same path; runs under `${API_STORAGE_DIR}/ldct_alsaihati/` |

| Variable | Default | Meaning |
|---|---|---|
| `LDCT_USE_GPU` (root `.env`) | `True` | GPU acceleration |
| `LDCT_GPU_INDEX` (root `.env`) | 0 | default GPU |
| `PORT_LDCT` (root `.env`) | 8001 | host port |
| `DCM4CHEE_RS` (compose) | `http://arc:8080/dcm4chee-arc/aets/DCM4CHEE/rs` | where series are pulled from and stored to |
| `LDCT_SERVICE_URL` (root `.env`, read by the API) | `http://ldct_api:8000` | how the API reaches the service |

## Code layout

```
LDCT_Alsaihati/
  main_app.py           Flask app: /ldct blueprint, /, /api/health, /api/jobs, /api/jobs/<id>
  config/               settings.py, base_storage.py (output root under API_STORAGE_DIR)
  utils/                job_manager.py (job registry), dicom_utils.py
  services/ldct/        the pipeline itself
                        ldct/blueprint.py: /ldct/, /ldct/simulate, /ldct/status/<id>, /ldct/jobs, DELETE /ldct/jobs/<id>
  monitor_ldct.sh       terminal monitor for running jobs (talks to localhost:${PORT_LDCT})
  Dockerfile, docker-compose.yml, requirements.txt
```

The pipeline's own documentation (the method, the steps, the parameters
`f` and `beta`, and standalone use with a YAML config) is in `services/ldct/`:
[`services/ldct/README.md`](../LDCT_Alsaihati/services/ldct/README.md) and
[`services/ldct/README_API.md`](../LDCT_Alsaihati/services/ldct/README_API.md).

**Parameters in short.** `f` is the dose fraction to simulate (`0.25` =
quarter dose). `beta` is the assumed exponent of the dose–noise relationship.

## Operations

```sh
docker compose logs -f ldct_api
./LDCT_Alsaihati/monitor_ldct.sh                 # live job overview
curl -s http://localhost:8001/api/health
```

Simulation runs under `API_storage/ldct_alsaihati/` are hand-named, not keyed
by study, so the retention sweep never deletes them. Clean them up by hand.

`LDCT_Alsaihati/` is bind-mounted into the container, so code changes need
only `docker compose restart ldct_api`.
