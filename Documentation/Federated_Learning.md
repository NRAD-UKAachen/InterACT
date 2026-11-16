# Federated learning

Several InterACT instances (sites) can train one segmentation model together
without any imaging data leaving a site. Each site trains on its own private
cases, and only model weights and summary statistics are exchanged.
Orchestration runs on **Apache Airflow**; admins drive it from the portal's
**Federated Learning** menu. This feature is under active development.

The model currently available is **SegResNet via MONAI Auto3DSeg**
(`segresnet_auto3dseg`).

## How a run works

One site acts as **coordinator**. It may also train on its own data. Every
participating site, the coordinator included if it trains, runs the
**local** DAG once per round.

```
coordinator (fl_segresnet_coordinator)               each site (fl_segresnet_local)
───────────────────────────────────────              ─────────────────────────────────
round 0  "fingerprint"
  ask every site to run its local DAG ──────────────► analyse own data → per-case statistics
  wait for uploads ◄──────────────────────────────── upload fingerprint.yaml
  merge fingerprints, generate ONE shared recipe
  (spacing, patch size, intensity bounds,
   Auto3DSeg training scripts) → recipe.tar.gz

round 1 … num_rounds  "train"
  distribute: recipe + global model ────────────────► download recipe (+ global model, n>1)
                                                     train LOCAL_EPOCHS on own data (GPU)
  wait for uploads ◄──────────────────────────────── upload plaintext weight update
  FedAvg (equal weights) → global_model_next.pt
  ...
final model → downloadable deliverable (model, recipe, inference script, requirements)
```

The fingerprint round exists because Auto3DSeg normally derives its
preprocessing from the dataset it sees. Left alone, every site would build a
differently shaped network, and the results could not be averaged. So every
site's statistics are merged first and all sites train against the same
frozen recipe. `num_rounds` counts **training rounds only**; round 0 comes on
top.

**Aggregation** is plain FedAvg with every site weighted equally, regardless
of how many cases it trained on. Sample-weighted averaging is a possible
extension.

**Trust model.** Weight updates travel between instances **unencrypted**, with
no secure aggregation. Anyone who can observe the traffic can read the
weights; with a man-in-the-middle position, they could alter them. Run
participating instances behind HTTPS, a VPN or a private link if the network
between them isn't trusted. Instance-to-instance calls are authenticated with
per-partner bearer tokens (below).

## Using it (portal → Federated Learning, admins only)

| Page | Route | Purpose |
|---|---|---|
| How To | `/federated/rounds/how-to` | the user-facing walkthrough and requirements |
| Instances | `/federated/instances/` | this instance's own name and base URL; register partner sites; allow or revoke which local DAGs each partner may trigger here; rotate tokens |
| Training Data | `/federated/training-data/` | upload this site's private training cases (`images/` + `labels/`, matched by filename) in batches; enable or disable batches |
| Training Rounds | `/federated/rounds/` | start a run (model, rounds, modality, local epochs, participating sites), watch progress, cancel (locally and at remotes), download the final deliverable |

**Pairing two instances.** Each pair exchanges two tokens, one per direction:

1. On A: *Add Remote Instance* with B's name and base URL. A mints a token
   for B to use when calling A and shows it **once**.
2. A's admin sends B's admin A's name, base URL and that token.
3. On B: *Add Remote Instance* for A, pasting the token as B's outgoing token
   for A. B mints a token for A, shown once.
4. On A: paste B's token into B's card (*Update outgoing token*).

Tokens are checked by `require_instance_token`
(`app/utils/federated_auth.py`). A partner can only trigger the local DAGs it
has been allowed on this instance.

## Components

### Airflow (`airflow/`, Compose profile `airflow`)

```sh
docker compose --profile airflow up -d --build
```

| Service | Notes |
|---|---|
| `airflow-postgres` | metadata DB; published on `${PORT_AIRFLOW_DB}` (5433), all interfaces |
| `fl-segresnet-trainer` | **build-only**: builds `local-only/fl-segresnet-trainer:0.1.0` from `airflow/docker/fl-segresnet-trainer/` and exits. `airflow-init` waits for it, so the image exists before the first round. The tag must match `image=` in both DAGs |
| `airflow-init` | DB migration, creates the API user, checks that logs and workflows are writable |
| `airflow-webserver` | Airflow UI and REST API on `${PORT_AIRFLOW}` (8085); the portal uses `http://airflow-webserver:8080/api/v1` |
| `airflow-scheduler` | LocalExecutor. Mounts the Docker socket (group `DOCKER_GID`) to launch the trainer container |

The Airflow image is `airflow/docker/airflow-custom/` (`apache/airflow:2.9.3-python3.11` + requirements).

**DAGs** (`airflow/dags/`):

- `dag_fl_segresnet_coordinator.py` (`fl_segresnet_coordinator`). Params:
  `run_id`, `num_rounds` (10), `modality` (`CT`), `local_epochs` (1),
  `remote_instances`. A single long task; a 4 h timeout per round, polling
  every 30 s; aborts cleanly when the portal writes a cancel marker.
- `dag_fl_segresnet_local.py` (`fl_segresnet_local`). One round on this
  site's data: `fingerprint` or `train`. Runs the trainer through
  `DockerOperator` with one GPU, mounting the workflows directory and the
  training data (read-only, at `/data/fl_local_training`).
- Trainer (`airflow/docker/fl-segresnet-trainer/`): `nvidia/cuda:11.8.0` +
  MONAI; `train_fl_round_segresnet.py` (fingerprint/train),
  `fl_segresnet_infer.py` (shipped with the deliverable). It trains on the
  cases listed in `enabled_cases.json`, which the Training Data page
  maintains.

### Portal (`docker-context-gui/`)

| File | Role |
|---|---|
| `app/utils/federated_models.py` | `FL_MODELS`: the model registry (DAG pair, directory name, title) |
| `app/routes/federated_rounds.py` | start, monitor, cancel and download runs; also the site-facing endpoints `round_<n>/global_model`, `recipe`, `round_<n>/upload/<site>`, `round_<n>/failed/<site>`, `final-model` |
| `app/routes/federated_instances.py` | partner registry and the machine endpoint `/federated/instances/incoming/job`, through which a remote coordinator asks this instance to run its local DAG |
| `app/routes/federated_training_data.py` | training-data upload and batches |
| `app/utils/federated_store.py`, `federated_auth.py` | instance registry and tokens |
| `app/utils/airflow_client.py` | Airflow REST client (`trigger_dag`, status) |

### Configuration and data

| Variable (root `.env`) | Meaning |
|---|---|
| `AIRFLOW_DB_PASSWORD`, `AIRFLOW_API_USER`, `AIRFLOW_API_PASSWORD` | Airflow DB and REST API credentials (the portal uses the latter two) |
| `AIRFLOW_WORKFLOWS_DIR` | host directory shared by Airflow, the trainer and the portal (`/data/airflow/workflows` inside) |
| `FL_LOCAL_TRAINING_DATA_DIR` | this site's private training cases (portal: `/app/fl_local_training_data`) |
| `DOCKER_GID`, `HOST_UID` | so the scheduler can use the Docker socket and write host-owned files |
| `PUBLIC_HOST`, `PUBLIC_SCHEME`, `PORT_PORTAL` | build this instance's public base URL, which remote sites upload to |

On disk:

```
API_storage/federated/
  instances.json                 partner registry (tokens included -- protect it)
  self_call_token.secret         token for the coordinator calling its own instance
  training_data_batches.json     training-data batch bookkeeping

${AIRFLOW_WORKFLOWS_DIR}/fl-segresnet/<run_id>/
  metadata.json                  coordinator's run record
  fingerprints/<site>.yaml       round-0 uploads
  recipe/, recipe.tar.gz         the shared recipe
  round_<n>/global_model.pt, uploads/<site>/uploaded/plaintext_update.pt, global_model_next.pt
  participant_metadata.json      this site's own record when it trains
  local/round_<n>/               this site's private per-round work

${FL_LOCAL_TRAINING_DATA_DIR}/images/, labels/, enabled_cases.json
```

`API_storage/federated/` is excluded from the storage retention sweep.

## Adding another federated model

1. Write a coordinator and local DAG pair in `airflow/dags/`, using
   `dag_fl_segresnet_*.py` as the template (round protocol, upload
   endpoints, cancel handling).
2. Build its trainer image (a build-only service in
   `airflow/docker-compose.yml`, like `fl-segresnet-trainer`, so it exists
   before the first run).
3. Add one entry to `FL_MODELS` in
   `docker-context-gui/app/utils/federated_models.py`: `coordinator_dag_id`,
   `local_dag_id`, `dir_name`, `title`, `description`, `icon`.

The instance registration and trust layer is model-agnostic; nothing else
changes. All participating sites need the new DAGs and image.

## Troubleshooting

- **Run stuck waiting for a site**: that site's local DAG failed or can't
  reach the coordinator. Check the site's Airflow UI (port 8085) and that
  its *Instances* page has the coordinator registered with a valid token. A
  round times out after 4 h.
- **403 "Token not recognized"**: the tokens of a pair don't match. Redo
  steps 3–4 of the pairing, or rotate the token.
- **Trainer image not found**: `fl-segresnet-trainer` was not built. Run
  `docker compose --profile airflow up -d --build`.
- **Airflow can't write logs/workflows**: `HOST_UID` / directory ownership.
  `airflow-init` reports which one.
