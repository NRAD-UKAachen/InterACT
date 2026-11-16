# Project structure

This document explains how InterACT is put together: what each directory is,
which services run, how they talk to each other, and where configuration and
data live. It goes deeper than the [root README](../README.md), which covers
first-time setup. Each component has its own document:

| Document | Covers |
|---|---|
| [GUI.md](GUI.md) | the portal (Flask): pages, proxies, PHI audit trail, TLS/cookies |
| [OHIF_Viewer_and_DCM4CHEE.md](OHIF_Viewer_and_DCM4CHEE.md) | the viewer, the archive, oauth2-proxy, InterACT's changes to OHIF |
| [API_and_API_storage.md](API_and_API_storage.md) | the processing API, the batch pipeline, `API_storage/` and retention |
| [Federated_Learning.md](Federated_Learning.md) | multi-site training with Airflow |
| [SAM.md](SAM.md) | SAMinRAD interactive segmentation |
| [iRODS.md](iRODS.md) | research data management |
| [Keycloak.md](Keycloak.md) | identity, the realm, user management |
| [LDCT.md](LDCT.md) | low-dose CT simulation |
| [How_To_Add_New_Tools.md](How_To_Add_New_Tools.md) | adding a tool to OHIF, the batch pipeline and the GUI |

## What InterACT is

InterACT is a research imaging platform built from established open-source
components (dcm4chee-arc, OHIF, Keycloak, iRODS, Airflow). It adds a Flask
portal in front and a processing API behind. Studies live in the DICOM
archive. Users read them in OHIF and launch analysis pipelines from OHIF's
toolbar or, for whole cohorts, from the portal. Results are written back into
the archive as DICOM (SEG, Secondary Capture, new image series), or returned
as files.

> **Research software handling patient data.** It is not a medical device,
> and nothing it produces is a clinical read. The defaults assume a trusted
> network; see [Security model](#security-model).

## Repository layout

```
docker-compose.yml        root Compose file: includes every stack, declares the shared networks
.env.example              every setting and port, in one file (copy to .env; .env is the only file you edit)
generate-secrets.py       fills every blank secret in .env
secrets/                  credentials you supply; gitignored except READMEs (FreeSurfer licence)

docker-context-gui/       the portal (Flask)                                 → GUI.md
API/                      the processing API (Flask)                         → API_and_API_storage.md
API_storage/              API working data, outputs, logs (gitignored)       → API_and_API_storage.md
viewers/                  OHIF fork (git submodule); the stack that runs is
                          platform/app/.recipes/Nginx-Dcm4chee/              → OHIF_Viewer_and_DCM4CHEE.md
keycloak-dev/             Keycloak + interact-realm.json + user scripts      → Keycloak.md
IRODS/                    iRODS catalog, provider, HTTP API                  → iRODS.md
airflow/                  Airflow, federated-learning DAGs, trainer image    → Federated_Learning.md
docker-context-saminrad/  SAMinRAD (SAM2 interactive segmentation)           → SAM.md
LDCT_Alsaihati/           low-dose CT simulation service                     → LDCT.md
Documentation/            this documentation
```

### Submodules

`viewers/` is a git submodule holding InterACT's fork of the
[OHIF Viewer](https://github.com/OHIF/Viewers) (MIT licence). `.gitmodules`
names its repository and the branch this deployment tracks. The LDCT
pipeline (`LDCT_Alsaihati/services/ldct/`) is included as an ordinary folder.

Clone with `git clone --recurse-submodules`, or run
`git submodule update --init` in an existing clone.

## How it runs

### One Compose project

The root `docker-compose.yml` sets `name: ${STACK_NAME}` and `include:`s
every stack file, in this order:

1. `keycloak-dev/docker-compose.yml`
2. `viewers/platform/app/.recipes/Nginx-Dcm4chee/docker-compose.yml`
3. `API/docker-compose.yml`
4. `docker-context-gui/docker-compose.yml`
5. `IRODS/docker-compose.yml`
6. `airflow/docker-compose.yml`
7. `docker-context-saminrad/docker-compose.yml`
8. `LDCT_Alsaihati/docker-compose.yml`

Always run `docker compose` **from the repository root**. Every stack reads
the root `.env`, and the shared networks are declared under the same names
in each file and merged by `include:`. A stack started from its own directory
gets a different project name and its own networks, and won't find the
others.

### Profiles

```sh
docker compose up -d                       # core: Keycloak, dcm4chee + OHIF, API, portal
docker compose --profile irods    up -d    # + iRODS
docker compose --profile airflow  up -d    # + Airflow / federated learning
docker compose --profile saminrad up -d    # + SAMinRAD (downloads ~900 MB of weights on first start)
docker compose --profile ldct     up -d    # + LDCT simulation
docker compose --profile all      up -d    # everything
```

Profiles are additive. Bringing one up leaves the rest alone. If the portal
was started before the `irods` profile, restart it
(`docker compose up -d interact-gui`).

### Services and ports

| Service | Profile | Host port (`.env`) | Internal address | Purpose |
|---|---|---|---|---|
| `interact-gui` | core | `PORT_PORTAL` 5005 | `interact-gui:5000` | portal |
| `oauth2-proxy` | core | `PORT_OHIF` 1081 | – | Keycloak login in front of OHIF |
| `oauth2-proxy-admin` | core | `PORT_DCM4CHEE_ADMIN` 1082 | – | Keycloak login (Admin group) in front of the dcm4chee UI |
| `ohif_viewer` | core | – | `ohif_viewer:80` | OHIF build + nginx (PHI log) |
| `oauth2-redis` | core | – | `oauth2-redis:6379` | oauth2-proxy sessions |
| `dcm4chee-ui-proxy` | core | – | `dcm4chee-ui-proxy:80` | themed dcm4chee UI |
| `arc` | core | `PORT_DCM4CHEE_DICOM` 11112 (all interfaces); HTTPS, MGMT, DICOM-TLS, HL7 on 127.0.0.1 | `arc:8080` | dcm4chee archive |
| `ldap`, `db` | core | `PORT_DCM4CHEE_LDAP` 389, `PORT_DCM4CHEE_DB` 5432 (127.0.0.1) | `ldap:389`, `db:5432` | dcm4chee config / database |
| `keycloak-dev`, `keycloak-db` | core | `PORT_KEYCLOAK` 8081 | `keycloak-dev:8080` | identity provider |
| `ohif_api` | core | `PORT_API` 9200 (**127.0.0.1**) | `ohif_api:9200` | processing API |
| `irods-catalog`, `irods-catalog-provider`, `irods-http-api` | irods | `PORT_IRODS_NATIVE` 1247 (provider) | `irods-http-api:9000` | iRODS |
| `airflow-*`, `fl-segresnet-trainer` | airflow | `PORT_AIRFLOW` 8085, `PORT_AIRFLOW_DB` 5433 (both on all interfaces) | `airflow-webserver:8080` | FL orchestration |
| `saminrad` | saminrad | `PORT_SAMINRAD` 8010 | `saminrad:8003` | SAMinRAD |
| `ldct_api` | ldct | `PORT_LDCT` 8001 | `ldct_api:8000` | LDCT simulation |

Container names, network names, volume names and locally built images all
carry the `STACK_NAME` prefix (for example `${STACK_NAME}-ohif_api`). That is
how two instances can share a host. The root README describes running
several instances with offset `PORT_*` values.

### Networks

| Compose name | Docker name | Members |
|---|---|---|
| `interact-dcm4chee` | `${STACK_NAME}-dcm4chee` | everything that talks to the archive or to each other: dcm4chee stack, OHIF, oauth2-proxies, API, portal, SAMinRAD, LDCT, Airflow |
| `interact-auth` | `${STACK_NAME}-auth` | Keycloak, its DB, both oauth2-proxies |
| `interact-irods` | `${STACK_NAME}-irods` | iRODS services and the portal |

Containers always reach each other **by service name** over these networks,
never via `PORT_*` or `PUBLIC_HOST`. `PORT_*` values only matter to things
outside Docker. Service names that are called over HTTP must be in
`INTERACT_NO_PROXY` when an outbound proxy is configured.

### Request flow

```
                                 Browser
       ┌─────────────────────────────┼─────────────────────────────┐
       │ :1081                       │ :5005                       │ :1082
 oauth2-proxy ── Keycloak ──── interact-gui (portal) ──── oauth2-proxy-admin (Admin)
       │                             │                             │
 ohif_viewer (nginx)   ┌─────────────┼───────────────┬─────────────┤
  ├ static OHIF        │ /api-proxy  │ /irods        │ /proxy/     │ /dcm4chee-proxy
  └ /dcm4chee-arc ─┐   ▼             ▼               ▼ colleague   ▼
                   │ ohif_api   irods-http-api    saminrad    dcm4chee-ui-proxy
                   │   │  ├─ sibling containers         │            │
                   │   │  │  (host Docker socket)       │            │
                   │   │  └─ ldct_api                   │            │
                   ▼   ▼                                ▼            ▼
                   arc (dcm4chee-arc)  ◄──── DICOMweb (QIDO / WADO / STOW) ────┘
```

1. A user logs in once to Keycloak, via the portal or oauth2-proxy.
2. OHIF loads studies from `arc` through its own nginx (audited).
3. An OHIF toolbar button calls the **portal's** `/api-proxy/<tool>/<study>`.
   The portal checks the session and an allow-list, records the call in the
   PHI trail, and forwards it to `ohif_api:9200`.
4. The API pulls the series from `arc`, runs the tool (in-process, as a
   sibling container through the host's Docker socket, or via another
   service), STOWs the results back into the same study, and reports progress
   on `/job_status/<id>`, which OHIF polls.
5. Batch processing follows the same path from the portal's Batch page, with
   input from an upload or iRODS. See
   [API_and_API_storage.md](API_and_API_storage.md#batch-pipeline).

## Configuration: `.env`

`cp .env.example .env`, then `./generate-secrets.py`, then set `PUBLIC_HOST`.
Every compose file reads only this file. Its sections:

| Section | Contents |
|---|---|
| 1. Where this deployment lives | `STACK_NAME`, `PUBLIC_HOST` (the name **browsers** use; it ends up in OIDC redirect URIs), `PUBLIC_SCHEME`, every `PORT_*`, and the derived `PORTAL_BASE_URL`, `OHIF_BASE_URL`, `DCM4CHEE_ADMIN_BASE_URL`, `KEYCLOAK_BASE_URL`; optional `OIDC_ALLOWED_HOSTS` |
| 2. Host paths | `INTERACT_ROOT`, `API_STORAGE_DIR`, log and storage retention, `FSL_DIR`, `FREESURFER_DIR`, `FS_LICENSE_DIR`, Airflow data dirs, `HOST_UID`/`HOST_GID`/`DOCKER_GID` |
| 3. Outbound HTTP proxy | `INTERACT_HTTP_PROXY`, `INTERACT_HTTPS_PROXY`, `INTERACT_NO_PROXY` (must list internal service names) |
| 4. Secrets | Keycloak admin/DB passwords; OIDC client secrets (`OIDC_GUI_CLIENT_SECRET`, `OIDC_OHIF_CLIENT_SECRET`, `IRODS_OIDC_CLIENT_SECRET`); `OAUTH2_PROXY_COOKIE_SECRET`, `OAUTH2_PROXY_REDIS_PASSWORD`; Flask keys; dcm4chee Postgres/LDAP; Airflow; iRODS; SMTP |
| 5. Cookies and sessions | `COOKIES_INSECURE`, `BEHIND_TLS_PROXY`, `SESSION_LIFETIME_HOURS` |
| 6. PHI audit trail | `AUDIT_RETENTION_DAYS`, `AUDIT_REASON_TTL_MINUTES` |
| 7. Compute tuning | `BATCH_JOB_CONCURRENCY`, `BATCH_JOB_NUM_GPUS`, `SCREW_MODEL_IMAGE`, `SAM2_DEVICE`, `DOWNSAMPLE_VIEWER`, `LDCT_USE_GPU`, `LDCT_GPU_INDEX` |

**`generate-secrets.py`** fills every blank secret, using a generator suited
to each one's constraints: iRODS passwords stay ≤ 42 characters,
`OAUTH2_PROXY_COOKIE_SECRET` is a base64 32-byte key. Values that are already set are kept. Flags:
`--force` (regenerate all), `--length N`, `--dry-run`. It never touches
`KC_SMTP_PASSWORD`.

**Secrets are never committed.** Client secrets are injected into the
Keycloak realm, the portal's rendered `client_secrets.json` and the
oauth2-proxies from `.env` at start-up. Compose refuses to start a service
whose required secret is empty (`${VAR:?…}`). Changing a secret in `.env` on
an existing installation also requires the change in Keycloak (see
[Keycloak.md](Keycloak.md#the-realm-keycloak-devinteract-realmjson)).

**Same-path mounts.** `API_STORAGE_DIR` is mounted at the same path inside
the API, portal, SAMinRAD and LDCT containers as on the host. The API
launches sibling containers with paths it resolved itself, and the portal
hands staged cohorts to the API by path, so every side must agree.

## Persistent data

| Where | What | Notes |
|---|---|---|
| `viewers/platform/app/.recipes/Nginx-Dcm4chee/dcm4chee-arc/` | archive DB, LDAP, objects, WildFly | **the patient data**; back up together |
| `viewers/.../Nginx-Dcm4chee/logs/nginx/phi-audit.log` | OHIF ↔ dcm4chee access log | no rotation configured |
| `docker-context-gui/audit_logs/` | portal PHI trail | retention `AUDIT_RETENTION_DAYS` |
| `docker-context-gui/flask_sessions/`, `uids_logs/` | sessions, bug reports | |
| `API_storage/` | API outputs, batch jobs, logs, weights, FL registry, SAMinRAD and LDCT data | see [API_and_API_storage.md](API_and_API_storage.md#api_storage) |
| named volumes `${STACK_NAME}-keycloak-pgdata`, `${STACK_NAME}-irods-pgdata`, `${STACK_NAME}-irods-vault`, `${STACK_NAME}-airflow-pgdata` | Keycloak DB, iRODS catalog and vault, Airflow DB | |
| `airflow/data/` (`AIRFLOW_WORKFLOWS_DIR`, `FL_LOCAL_TRAINING_DATA_DIR`) | FL runs, local training data | |

All of these are gitignored. The audit logs and the archive must be part of
any backup.

## Security model

What protects what, as currently configured:

| Surface | Protection |
|---|---|
| Portal :5005 | Keycloak login (flask-oidc); admin pages by group/role |
| OHIF :1081 | oauth2-proxy (any Keycloak user) |
| dcm4chee UI :1082 | oauth2-proxy, group `Admin` only |
| Processing API | **no auth**; bound to `127.0.0.1`. Browsers go through the portal's `/api-proxy` (login + path allow-list + audit) |
| iRODS | the user's own Keycloak token, introspected by the HTTP API; iRODS ACLs enforced per user |
| dcm4chee HTTP / admin / DB / LDAP / HL7 ports | not published (8080), or bound to `127.0.0.1` |
| DICOM port 11112 | **open, no Keycloak** (DICOM has no user identity; AE title only) |
| SAMinRAD :8010, LDCT :8001 | **no authentication**; bound to `127.0.0.1`. They exist for direct use but bypass the portal; reach them from elsewhere through an SSH tunnel |
| iRODS native :1247 | iRODS protocol authentication; bound to `IRODS_NATIVE_BIND` (`127.0.0.1` by default). Note that Docker-published ports bypass host firewalls like ufw |
| Keycloak :8081 | Keycloak itself (brute-force protection, password policy) |
| Airflow :8085, Airflow DB :5433 | Airflow's own login (`AIRFLOW_API_USER` / `AIRFLOW_API_PASSWORD`) and the Postgres password (`AIRFLOW_DB_PASSWORD`); **no Keycloak**. Both are published on all interfaces |

Other things to know:

- **Plain HTTP by default.** Tokens and PHI cross the network unencrypted
  until a TLS terminator is put in front (see [GUI.md](GUI.md#tls-and-cookies)).
  `COOKIES_INSECURE=1` must be set back to `0` at that point.
- **The API container is effectively root on the host.** It mounts
  `/var/run/docker.sock` to start sibling containers. Its loopback binding
  and the portal's allow-list are what keep that from being reachable. A
  socket proxy restricted to container creation would reduce the risk.
- **`DISABLE_OIDC=1`** in the portal's environment turns authentication off
  completely. Never set it outside development.
- **Logging hygiene.** Request headers, cookies and query strings are not
  logged (they carried tokens and PHI), and oauth2-proxy for OHIF has
  request logging off. The PHI trail is the only place access is recorded,
  in files with restricted access.
- **PHI audit trail**: who viewed, searched, downloaded or changed what, in
  dcm4chee and iRODS, with known gaps (the :1082 admin UI and raw DICOM
  traffic). See [GUI.md](GUI.md#phi-access-audit-trail).
- **Patient data stays out of git.** `.gitignore` covers outputs, inboxes,
  `*.dcm`, `.env`, audit logs and `keycloak-dev/users.csv` /
  `credentials-*.txt`. Delete credential handout files once used.
- **`API/requirements.txt` is unpinned**, unlike the portal's.

## Prerequisites (summary)

Docker with Compose v2; for the GPU tools, the NVIDIA Container Toolkit; for
FastSurfer, a FreeSurfer licence (`secrets/freesurfer/README.md`); for
TotalSegmentator `brain_structures`, a TotalSegmentator licence
(`secrets/totalsegmentator/README.md`); for SynthSeg, a host FreeSurfer ≥ 8.2
(`FREESURFER_DIR`); for the FSL-based tools, a host FSL (`FSL_DIR`). The
portal, viewer, archive and identity layer need none of the last five; tools whose prerequisite is missing report a clear
error.
