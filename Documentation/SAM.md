# SAMinRAD (interactive segmentation)

SAMinRAD is a Flask web app for interactive, semi-automatic segmentation of
volumetric images (DICOM, NIfTI, NRRD). It uses Meta's **SAM2** and
classical intensity thresholding, and can score a result against a
ground-truth mask. In the portal it is **Interactive Segmentation** in the
sidebar. It needs an NVIDIA GPU.

## Methods

| Method | What it does |
|---|---|
| **SAM2 point prompts** | Positive and negative clicks on one 2D slice (axial, sagittal or coronal) go to `SAM2VideoPredictor`, run as a single-frame pseudo-video, which returns a binary mask. |
| **Threshold** | A 3D mask from lower/upper intensity (HU) bounds over the whole volume, with optional hole-filling and removal of small components. Can also refine a SAM2 result (`/refine_sam_with_threshold`). |
| **Few-shot** | A mask on one annotated slice is propagated to adjacent slices with SAM2 in video mode; optional threshold refinement afterwards. |
| **S1SFQ** | Few-shot from **exactly one** support mask, propagated from that slice outward in both directions (two halves). |
| **Memory** | Propagation that uses SAM2's own memory across slices (vanilla SAM2 defaults: `confidence_threshold=0.0`, no re-injection). |

Masks can be refined morphologically (erosion, dilation, hole-filling). Each
segmentation yields a full-resolution **analysis mask** for metrics and a
downsampled **viewer mask** (`DOWNSAMPLE_VIEWER`, default 0.7) for the NiiVue
3D view in the browser.

**Ground-truth comparison.** Upload a reference mask (NIfTI/NRRD). The app
reports Dice, ASSD (mm), sensitivity, specificity, precision and relative
volume difference. Shape mismatches are resolved by axis permutation or
resampling, and the reference is overlaid in the 3D view.

**Inputs.** DICOM (single files, folder upload, ZIP), `.nii`/`.nii.gz`,
`.nrrd`; from upload, from the user's iRODS space, or from a dcm4chee series.
Uploads are limited to 2 GB (`MAX_CONTENT_LENGTH` in `app.py`).

## Deployment

Compose profile **`saminrad`** (or `all`), from the repository root:

```sh
docker compose --profile saminrad up -d --build
docker compose logs -f saminrad
```

| | |
|---|---|
| Service / container | `saminrad` / `${STACK_NAME}-saminrad` |
| Internal URL | `http://saminrad:8003` on `interact-dcm4chee` |
| Published port | `127.0.0.1:${PORT_SAMINRAD}` (default 8010), loopback only, **no Keycloak**, see below |
| GPU | one reserved |
| Server | gunicorn, 1 worker, 4 threads, no timeout |
| Data | `${API_STORAGE_DIR}/SAM_data` → `/app/data` |

**First start takes a while.** `docker-entrypoint.sh` downloads the SAM2
checkpoint `sam2.1_hiera_large.pt` (898 MB) into
`docker-context-saminrad/checkpoints/`, verifies its sha256, and only then
starts gunicorn (the healthcheck allows 300 s). The file stays on the host
and is reused across rebuilds. It is deliberately not in the image or in git
(LFS quota). For a different checkpoint, set `SAM2_CHECKPOINT`,
`SAM2_CHECKPOINT_URL` and `SAM2_CHECKPOINT_SHA256` in the root `.env`, and
`SAM2_MODEL_CONFIG` to the matching model config (default
`configs/sam2.1/sam2.1_hiera_l.yaml`).

| Variable (root `.env`) | Default | Meaning |
|---|---|---|
| `SAMINRAD_FLASK_SECRET_KEY` | – (required) | session signing |
| `SAM2_DEVICE` | `cuda` | inference device |
| `DOWNSAMPLE_VIEWER` | 0.7 | viewer-mask scale |
| `PORT_SAMINRAD` | 8010 | direct host port, bound to 127.0.0.1 |
| `SAM2_CHECKPOINT` | `/app/checkpoints/sam2.1_hiera_large.pt` | checkpoint path in the container |
| `SAM2_CHECKPOINT_URL`, `SAM2_CHECKPOINT_SHA256` | the official `sam2.1_hiera_large.pt` | where the first start downloads it from, and its checksum |
| `SAM2_MODEL_CONFIG` | `configs/sam2.1/sam2.1_hiera_l.yaml` | must match the checkpoint |

`config.py` reads further settings (`MIN_AREA`, `MIN_SCORE`, `IMAGE_FOLDER`,
`ARCHIVE_FOLDER`, `FLASK_PORT`, …) with in-code defaults.

| Task | Command (repo root) |
|---|---|
| Rebuild after a code change | `docker compose --profile saminrad up -d --build saminrad` |
| Shell in the container | `docker compose exec saminrad bash` |
| GPU / memory use | `docker stats ${STACK_NAME}-saminrad` |
| Health | `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8010/` → `200` |

## Integration with the portal

```
base.html "Interactive Segmentation"
  → /colleague_iframe                 (app/routes/interactive_segmentation_viewer.py)
  → <iframe src="/proxy/colleague/">  (app/templates/interactive_iframe.html)
  → COLLEAGUE_BASE=http://saminrad:8003   (app/routes/reverse_proxy.py; login required)
```

If the service is renamed or its port changes, update `COLLEAGUE_BASE` in
`docker-context-gui/docker-compose.yml`, and the name in
`INTERACT_NO_PROXY`. Otherwise the portal's requests go to the outbound HTTP
proxy instead of the Docker network.

SAMinRAD holds **no iRODS or Keycloak credential**. Everything that touches
another system runs from the browser through the portal's endpoints, so the
portal can attribute it to a person in the PHI audit trail:

| Action | Path |
|---|---|
| Import from iRODS | `/irods/browse`, `/irods/download_folder` |
| Import a dcm4chee series | `/dcm4chee-proxy/…` |
| Save result to iRODS | browser fetches `/download_latest_segmentation` → `POST /irods/upload` |
| Store as DICOM SEG in dcm4chee | browser fetches `/export_mask_nifti` → `POST /api-proxy/segmentation_upload/<study>/<series>` → API `segmentation_upload_route` → `backend_helpers.upload_mask_file_as_segmentation` |

The DICOM SEG option is offered only when the series came from dcm4chee and
could be stacked as a volume, because only then is the slice mapping
guaranteed. `/export_mask_nifti` rotates the mask back from the stacking
axis of the last segmentation into the volume's layout, and
`mask_array_for_series` checks it against the series grid and reorders it
into DICOM frame order.

**Port 8010** bypasses the portal's login. It exists for very large uploads,
because the portal's proxy buffers request bodies in RAM. It is bound to
127.0.0.1, so from another machine go through an SSH tunnel:
`ssh -L 8010:localhost:8010 <host>`, then open `http://localhost:8010/`.

## Results on disk

`${API_STORAGE_DIR}/SAM_data/` (`/app/data` in the container):

- `archive/<date>/…`: finished segmentations: masks, overlays, cut-outs,
  3D models and `segmentation_parameters.json`
- `input/`: uploads; `temp_upload/`: in-progress uploads

"Save result…" in the UI offers a ZIP download, a save to iRODS, or a DICOM
SEG into the database (above). `SAM_data` is never touched by the storage
retention sweep.

## Code layout

```
docker-context-saminrad/
  app.py              Flask app factory, blueprint registration
  config.py, state.py configuration; the single global session state
  routes/             main, upload, image, segmentation, threshold, fewshot, comparison, download
  services/           sam_service (SAM2), image_processing, mask_processing, file_loader, archive_service
  utils/, templates/, static/   helpers; upload + viewer pages; JS incl. NiiVue
  configs/sam2.1/     model config
  docker-entrypoint.sh, Dockerfile, docker-compose.yml
```

## Limitations

- **One user at a time.** The app is a single process with shared state
  (`state.py`): one dataset, one session, one model in memory. Every logged-in
  user sees the menu entry, but two people cannot work independently at the
  same time. Real multi-user support would need per-session state.
- 2 GB upload limit; larger uploads should use port 8010 directly (via an
  SSH tunnel, see above).
