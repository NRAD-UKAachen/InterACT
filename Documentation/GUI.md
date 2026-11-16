# InterACT portal (GUI)

The portal (`docker-context-gui/`, Compose service **`interact-gui`**) is
the Flask app people log into. It holds the Keycloak session, renders the
pages, and proxies the browser through to everything else: OHIF, the dcm4chee
UI, the processing API, iRODS and SAMinRAD. It is the only service meant to
be reached directly by people. It does no imaging work of its own; that
happens in the [API](API_and_API_storage.md) and in OHIF.

## At a glance

| | |
|---|---|
| URL | `http://<PUBLIC_HOST>:${PORT_PORTAL}` (default 5005) |
| Service / container | `interact-gui` / `${STACK_NAME}-gui-interact-gui-container` |
| Image | `python:3.11` + `gosu`; `flask run --no-reload --with-threads` (single process) |
| Networks | `interact-dcm4chee` (API, dcm4chee, SAMinRAD, Airflow), `interact-irods` (iRODS HTTP API) |
| Identity | Keycloak client `flask-gui` via flask-oidc ([Keycloak.md](Keycloak.md)) |

## Running it

```sh
docker compose up -d interact-gui         # from the repository root
docker compose restart interact-gui       # after editing app/ (Python or templates)
docker compose up -d --build interact-gui # after requirements.txt / Dockerfile changes
```

`app/` is bind-mounted, so nothing needs a rebuild. The server runs with
`--no-reload` and without debug mode, though, so **Python and template edits
need a restart**.

## Configuration

Everything comes from the root `.env` through `docker-context-gui/docker-compose.yml`.

**`client_secrets.json` is generated, not stored.** As root,
`docker-entrypoint.sh` fixes ownership of the persisted directories
(`uids_logs`, `flask_sessions`, `audit_logs`, `static`, and
`$API_STORAGE_DIR/batch_inputs` and `/federated`), drops to
`HOST_UID:HOST_GID`, and renders `client_secrets.json.template` to
`/tmp/client_secrets.json` from `OIDC_CLIENT_SECRET`, `OIDC_ISSUER` and
`OIDC_CANONICAL_REDIRECT_URI`. The Keycloak client secret only exists in
`.env` and in the running container.

**No template knows a hostname.** `create_app()` injects `PORTAL_BASE_URL`,
`OHIF_BASE_URL` and `DCM4CHEE_ADMIN_BASE_URL` as template globals (and, via
`base.html`, as JS constants). Build links from them.

| Variable | Purpose |
|---|---|
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` (`OIDC_GUI_CLIENT_SECRET`) / `OIDC_ISSUER` | Keycloak client |
| `OIDC_CANONICAL_REDIRECT_URI`, `OIDC_ALLOWED_HOSTS` | the redirect URI is built from the request host only if that host is in the allow-list; otherwise the canonical one is used |
| `FLASK_SECRET_KEY` (`GUI_FLASK_SECRET_KEY`) | session signing; the app refuses to start without it |
| `COOKIES_INSECURE`, `BEHIND_TLS_PROXY`, `SESSION_LIFETIME_HOURS` | cookie flags, `ProxyFix`, session length (see [TLS](#tls-and-cookies)) |
| `CORS_ALLOWED_ORIGINS` | the OHIF origin, allowed to call `/api/*` and `/api-proxy/*` with credentials |
| `BATCH_API_BASE`, `BATCH_API_INTERNAL` | `http://ohif_api:9200` |
| `COLLEAGUE_BASE` | `http://saminrad:8003` |
| `DCM4CHEE_INTERNAL_URL` | `http://dcm4chee-ui-proxy:80` |
| `AIRFLOW_API_URL`, `AIRFLOW_API_USER`, `AIRFLOW_API_PASSWORD`, `AIRFLOW_WORKFLOWS_DIR`, `FEDERATED_LOCAL_BASE_URL` | federated learning |
| `IRODS_HTTP_API_URL`, `IRODS_ZONE` | iRODS. Tuning variables have code defaults and are not in the compose file; see [iRODS.md](iRODS.md#environment) |
| `AUDIT_LOG_DIR`, `AUDIT_RETENTION_DAYS`, `NGINX_AUDIT_LOG`, `AUDIT_REASON_TTL_MINUTES` | PHI audit trail |
| `DISABLE_OIDC` | `1` turns authentication off entirely. Development only |

Persisted host directories (all gitignored): `uids_logs/` (bug reports from
the home page), `flask_sessions/` (server-side sessions, mode 0600),
`audit_logs/` (the PHI trail).

## Layout

```
docker-context-gui/
  app/
    __init__.py       create_app(): OIDC, sessions, CORS, cookie flags, public URLs
    oidc_auth.py      the flask-oidc instance (DISABLE_OIDC switch)
    routes/           one blueprint per page or proxy
    utils/            audit trail, iRODS / Airflow clients, roles, job logs, FL store
    templates/        Jinja; base.html is the sidebar + iframe shell
    static/
  docker-entrypoint.sh, client_secrets.json.template, Dockerfile
  nginx-tls.conf      optional TLS terminator (not part of compose)
```

### What `create_app()` sets up

- **Sessions are server-side** (Flask-Session with a filesystem cache in
  `flask_sessions/`), because the Keycloak token (~4 KB) does not fit in a
  cookie.
- **flask-oidc** with scopes `openid email profile`. `auth_bp` is registered
  before `oidc.init_app`, so its own `/login` wins.
- **CORS**: `/api/*` and `/api-proxy/*` are open to the OHIF origin, the
  latter with credentials and `Content-Disposition` exposed (for downloads).
- **Iframe breakout**: OHIF and other pages are shown inside the portal's
  iframe, and Keycloak refuses to render in a frame. An `after_request` hook
  therefore turns a cross-origin redirect to Keycloak from inside an iframe
  (`Sec-Fetch-Dest: iframe`) into a top-level navigation.
- `is_admin`, `username` and `embedded` (`?embedded=1` renders a page without
  the sidebar) are injected into every template.
- Request headers and cookies are **never logged**; an old debug hook leaked
  tokens and PHI that way.

## Pages and routes

| Blueprint (file) | Prefix | Routes | Access |
|---|---|---|---|
| `auth` (`auth.py`) | – | `/login`, `/auth/logout`, `/auth/logged-out`, `/auth/change-password`, `/auth/force-login` | – |
| `home` (`home.py`) | – | `/` (landing page + bug reports), `/delete-report/<id>` | login |
| `viewer` (`viewer.py`) | `/viewer` | `/viewer/ohif` (OHIF in an iframe); `/viewer/database` (dcm4chee UI in an iframe) | login; database: **admin** |
| `interactive` (`interactive_segmentation_viewer.py`) | – | `/colleague_iframe` (SAMinRAD in an iframe) | login |
| `proxy` (`reverse_proxy.py`) | `/proxy` | `/proxy/colleague/<path>` → `saminrad:8003` | login (blueprint-wide) |
| `dcm4chee_proxy`, `dcm4chee_redirect` (`dcm4chee_proxy.py`) | – | `/dcm4chee-proxy/<path>`, `/dcm4chee-arc/<path>` → `dcm4chee-ui-proxy` | login, audited |
| `api_proxy` (`api_proxy.py`) | `/api-proxy` | `/<path>` → `ohif_api:9200` (allow-listed) | login (401, not redirect), audited |
| `batch` (`batch.py`) | `/batch` | `/`, `/stage_irods`, `/stage_status/<id>`, `/submit`, `/my_jobs`, `/job/<id>` (DELETE, `/pause`, `/resume`, `/stop`), `/jobs` (DELETE), `/push_to_irods/<id>` | login |
| `irods_browser` (`irods_browser.py`) | `/irods` | `/`, `/shared`, `/browse/<p>`, `/mkdir`, `/upload[/prepare|/finish]`, `/download/<p>`, `/download_folder/<p>`, `/delete`, `/share`, `/inheritance` | login + iRODS account |
| `irods_admin` (`irods_admin.py`) | `/irods-admin` | `/users`, `/users/create`, `/users/delete`, `/groups/create`, `/groups/delete`, `/groups/members` | **admin** (+ `rodsadmin`) |
| `audit_log` (`audit_log.py`) | `/audit` | `/`, `/reason`, `/end-session`, `/export.csv` | **admin** |
| `job_logs` (`job_logs.py`) | `/job-logs` | `/`, `/view/<rel>`, `/tail/<rel>`, `/download/<rel>` | **admin** |
| `federated_instances` | `/federated/instances` | site registry, token rotation, `/incoming/job` (instance token) | **admin** |
| `federated_rounds` | `/federated/rounds` | start, monitor and cancel runs; round upload/download endpoints for remote sites | **admin** (site endpoints: instance token) |
| `federated_training_data` | `/federated/training-data` | upload, enable and delete local training cases | **admin** |

The sidebar (`templates/base.html`) shows the OHIF Viewer, Interactive
Segmentation, iRODS and Batch Processing to everyone. Database, PHI Access Log,
Job Logs, iRODS → Manage Users and the Federated Learning menu appear for
admins only.

**Admin** means the Keycloak group `admin` or the realm role `admin`
(case-insensitive; `app/utils/roles.py`). Routes enforce it with
`@admin_required`. Hiding the sidebar link is only cosmetic.

### The API proxy

The processing API has no authentication and listens on the host's loopback
only. `api_proxy.py` is its authenticated front door for browsers (OHIF
toolbar buttons, OHIF Upload, the batch page, report links, SAMinRAD's
"store as DICOM-SEG"):

- requires a Keycloak session and returns **401** rather than a login
  redirect, since the callers are `fetch()`es;
- forwards only paths starting with an entry in **`ALLOWED_PREFIXES`**, and
  only if they match `^[A-Za-z0-9._/-]+$`;
- never forwards the caller's cookies upstream, and strips hop-by-hop and
  upstream CORS headers;
- writes every call to the PHI trail as system `api`. `_ACTIONS` maps
  prefixes to `download` / `export` / `upload` / `modify`, everything else is
  `view`, and status polling (`_NOT_PHI_PREFIXES`) is not audited.

Server-side code in the GUI calls the API directly at
`BATCH_API_BASE` (`http://ohif_api:9200`).

### Batch Processing

`/batch/` lets a user run one or more tools over a cohort laid out as
`patient_id/study_name/series_name/` (DICOM or NIfTI per series folder):

1. **Input**: drag-and-drop a folder (uploaded into
   `API_storage/batch_inputs/<stage_id>/`), or pick an iRODS collection. The
   GUI asks the API's `/batch/plan` which series the selected tools will
   open, then **stages in** only those under the user's own token
   (`/stage_irods`, `/stage_status/<id>`). Each series is bundled server-side
   into one tar where possible, with a per-file fallback.
2. **Tools**: cards are rendered from `BATCH_TOOLS` and grouped by
   `BATCH_TOOL_LAYOUT` (modality → clinical category). Each tool exposes one
   or more modes; hover cards list the required series roles
   (`SERIES_ROLE_INFO`), so users can name their folders correctly. "Use every
   matching series" runs a tool on every candidate series instead of the
   best match.
3. **Submit**: `/batch/submit` validates the (tool, mode) pairs and POSTs
   `input_path`, `tools`, `owner`, `output_mode` and `use_all_series` to the
   API's `/batch/run`.
4. **Monitor**: one card per job, polling `/api-proxy/batch/status/<id>`;
   `/batch/my_jobs` lists the user's jobs across sessions. Pause, resume,
   stop and delete act between cases.
5. **Output**: ZIP download (`/api-proxy/batch/result/<id>`, or `/partial`
   while running), or **push to iRODS** (`/push_to_irods/<id>`). Pushing uses
   the clicker's fresh token and is refused for someone else's job.

Pipeline internals: [API_and_API_storage.md](API_and_API_storage.md#batch-pipeline).
Adding a tool card: [How_To_Add_New_Tools.md §4](How_To_Add_New_Tools.md#4-c-adding-the-tool-to-the-gui).

### Job Logs

`/job-logs/` (admin) lists and tails the processing API's log files straight
off the shared `API_storage/logs/` mount (`app/utils/job_logs.py`): one file
per single-study job and per batch case, filterable by job kind. Viewing and
downloading are audited. Batch job cards link to their log for admins.

## PHI access audit trail

The portal records who viewed, searched, downloaded and edited patient data in
**dcm4chee** and **iRODS**. These records support GDPR Art. 30 (records of
processing) and Art. 15 (access requests), and answer the first question
after an incident: *who touched this study?*

Neither system can name the person behind a request here. dcm4chee has no
authentication of its own and only sees container IPs. iRODS knows the user,
but its server logs aren't reachable and can't see higher-level acts like
"downloaded a cohort as a ZIP". The Keycloak identity and the object accessed
only exist together at the proxy layer, which is where every record is
written.

### Where records come from

```
Browser
  ├── :1081 oauth2-proxy ── nginx (ohif_viewer) ──────────► dcm4chee   (A) nginx phi-audit.log
  ├── :1082 oauth2-proxy-admin ── dcm4chee-ui-proxy ──────► dcm4chee   (D) NOT audited — see gaps
  └── :5005 portal
        ├── /dcm4chee-arc/, /dcm4chee-proxy/ ─────────────► dcm4chee   (B) routes/dcm4chee_proxy.py
        ├── /api-proxy/… ─────────────────────────────────► API        (B) routes/api_proxy.py
        └── /irods/…, /batch/… ───────────────────────────► iRODS      (C) utils/irods_client.py
```

**(A) OHIF ↔ dcm4chee.** This is the busiest path and the only one that never
touches Flask. OHIF's nginx
(`viewers/platform/app/.recipes/Nginx-Dcm4chee/config/nginx.conf`,
`log_format phi_audit`) writes one JSON object per DICOMweb request to
`logs/nginx/phi-audit.log` in that recipe directory:

```json
{"ts":"2026-08-11T08:45:10+00:00","actor":"jsmith","actor_sub":"<keycloak sub>",
 "actor_email":"…","actor_ip":"172.20.0.10","forwarded_for":"198.51.100.23",
 "system":"dcm4chee","method":"GET","object":"/dcm4chee-arc/aets/DCM4CHEE/rs/studies/1.2.840…/frames/1",
 "criteria":"","status":200,"bytes":524288,"duration_s":0.031,"user_agent":"…"}
```

The username is only there because `oauth2-proxy` runs with
`--pass-user-headers`; remove that flag and the log goes anonymous. nginx
takes the identity from `X-Forwarded-Preferred-Username` (the login name, the
same value the GUI trail uses, so the two join on it), and falls back to
`X-Forwarded-User` (Keycloak's `sub`, logged separately as `actor_sub` because
it survives a rename) and then to `X-Auth-Request-User`. The end user's
address is `forwarded_for`; `actor_ip` is the proxy container. oauth2-proxy
itself runs with `--request-logging=false`, so study UIDs and search terms do
not also land in the unprotected `docker logs`.

Under DICOMweb, method + URI is the action: `GET …/studies?PatientID=` is a
**search** (terms in `criteria`), `GET …/studies/<uid>/…` and `…/wado?` are
a **view or download**, `POST …/studies` is an **upload** (STOW),
`DELETE …/studies/<uid>` is a **delete**, and `POST …/reject/…` is an
**edit** (rejection note).

**(B) and (C) — the GUI's own trail.** `app/utils/audit.py` appends to
`AUDIT_LOG_DIR` (host: `docker-context-gui/audit_logs/`), one JSON Lines file
per UTC day, `phi-access-YYYY-MM-DD.jsonl`. It never writes to stdout, and it
redacts credentials:

```json
{"ts":"2026-08-11T07:41:02.918Z","actor":"jsmith","actor_ip":"10.1.2.3","system":"irods",
 "action":"download","object_type":"data_object","object":"/interactZone/home/jsmith/CT/1.2.840….dcm",
 "outcome":"success","method":"GET","path":"/irods/download/…","request_id":"6990d554e14641c6"}
```

| Field | Meaning |
|---|---|
| `actor` | Keycloak `preferred_username`; `anonymous` without a session, `system` outside a request |
| `system` | `dcm4chee`, `irods`, `pacs`, `api`, `audit` |
| `action` | `view` `search` `list` `download` `export` `upload` `modify` `delete` `permission_change` `account_admin` |
| `outcome` | `success`, `denied`, `error`. **Refused attempts are recorded too** |
| `object` / `criteria` | iRODS path, DICOMweb URI or API path / search terms (credentials redacted) |
| `request_id`, `count` | group the many object-level lines of one bulk operation |

iRODS coverage is **structural**: every iRODS call goes through
`irods_client.py`, so listings, reads, writes, deletes, ACL changes and admin
actions are all recorded there. That includes background workers such as
batch stage-in, which pass the `actor` down explicitly. Two events are
recorded at route level because no single iRODS call represents them: a
folder downloaded as one ZIP (`export`), and a drag-and-drop folder upload.
A server-side tar bundle is audited as a read of the source collection, with
its file count.

### The PHI Access Log page

`/audit/` (admin) merges both logs, so an OHIF study view appears next to the
iRODS activity. It filters by person, subsystem, action, date range or free
text, and **Export CSV** returns the filtered set. If the nginx log is
unreadable, a banner says so.

- **A reason is required.** Before any record is shown, the admin must state
  why they are looking (at least 10 characters). The reason is written into
  the trail and stays valid for `AUDIT_REASON_TTL_MINUTES` (30). Reading the
  trail is itself audited.
- **Rows are collapsed for reading.** Opening one study is ~1100 frame
  requests, and typing a search term fires one QIDO query per keystroke. The
  page collapses requests per person, per study (or search term), within a
  15-minute window into one row with a count. Deletes, uploads and
  rejections are never collapsed. The files keep every request.

### Retention and handling

- `AUDIT_RETENTION_DAYS=0` (the default) keeps the GUI trail forever. Set
  it in the root `.env` once a retention period has been agreed with the data
  owner; German clinical documentation is commonly kept ten years (`3650`).
  Older files are deleted on the first write of a new day.
- The nginx `phi-audit.log` has **no rotation**. Add `logrotate` with
  `copytruncate` (or an `nginx -s reopen` hook) before it becomes a disk
  problem. The GUI mounts the log *directory*, so rotation replacing the file
  is picked up.
- Include both logs in backups. Neither may be committed; `.gitignore`
  covers both.

Shell recipes:

```sh
# Everything one person did in August
jq -c 'select(.actor=="jsmith")' docker-context-gui/audit_logs/phi-access-2026-08-*.jsonl
# Everyone who touched one study, across both logs
grep -h 1.2.840.113 docker-context-gui/audit_logs/*.jsonl \
     viewers/platform/app/.recipes/Nginx-Dcm4chee/logs/nginx/phi-audit.log | jq -c '{ts,actor,action,object}'
# Refused attempts
jq -c 'select(.outcome=="denied")' docker-context-gui/audit_logs/*.jsonl
```

### Known gaps

1. **(D) The dcm4chee admin portal on :1082 is not audited.** It goes
   `oauth2-proxy-admin` → `dcm4chee-ui-proxy` → `arc`, bypassing the nginx
   that writes `phi-audit.log`, and it can browse, export and delete any
   study. Only Admin-group users can reach it.
2. **Direct DICOM traffic** (C-STORE/C-FIND/C-MOVE on the DICOM port) passes
   no proxy. dcm4chee's own ATNA audit logger is the right tool for it; it
   records the calling AE title.
3. **No integrity protection.** The files are append-only by convention.
   Ship them to a write-once destination (for example a remote syslog
   collector) if tamper evidence is required.
4. **Single writer.** `flask run --with-threads` is one process, so a lock
   suffices. Moving to multi-process gunicorn would need this revisited.

## TLS and cookies

The stack speaks plain HTTP by default:

- Session cookies are `HttpOnly` and `SameSite=Lax`, and `Secure` unless
  `COOKIES_INSECURE=1`. `.env.example` ships `COOKIES_INSECURE=1`, because a
  `Secure` cookie is never sent over `http://` and the login would loop. **Set
  it back to `0` once TLS is in front.**
- `docker-context-gui/nginx-tls.conf` is a starting point for a TLS
  terminator (TLS 1.2/1.3, HSTS, 80→443, long timeouts for large transfers)
  that proxies to `interact-gui:5000`. It is not part of the Compose project;
  run it yourself. Then set `BEHIND_TLS_PROXY=1` (enables `ProxyFix`, which
  must not be on without a proxy in front, or clients can spoof their scheme
  and IP) and `COOKIES_INSECURE=0`, switch `PUBLIC_SCHEME` to `https`, and
  set the realm's `sslRequired` to `all`.
- Don't `source .env` in a shell you run `docker compose` from. Exported
  variables override the `.env` file, and Compose will silently keep stale
  values.
