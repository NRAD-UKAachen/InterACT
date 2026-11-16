# OHIF viewer and dcm4chee

The imaging core of InterACT:

- **dcm4chee-arc** (5.29) is the DICOM archive (PACS). Every study, and every
  result the pipelines produce, lives here.
- **OHIF** (v3 fork, `viewers/`) is the web viewer users read studies in.
  InterACT adds its pipeline buttons.
- **oauth2-proxy** puts Keycloak login in front of both.

All of it is defined by one Compose file inside the OHIF repository:
`viewers/platform/app/.recipes/Nginx-Dcm4chee/docker-compose.yml`, which the
root `docker-compose.yml` includes. These services are always on (no profile).

## Access

| URL | What | Who |
|---|---|---|
| `http://<PUBLIC_HOST>:${PORT_OHIF}` (1081) | OHIF viewer (also `/dcm4chee-arc/…` DICOMweb and UI through OHIF's nginx) | every Keycloak user |
| `http://<PUBLIC_HOST>:${PORT_DCM4CHEE_ADMIN}/dcm4chee-arc/ui2/` (1082) | dcm4chee admin UI | Keycloak group **Admin** only; others get "403 Forbidden" after login |
| portal → OHIF Viewer (`/viewer/ohif`) | OHIF embedded in the portal | every user |
| portal → Database (`/viewer/database`) | dcm4chee UI embedded in the portal, via the GUI's audited proxy | portal admins |
| `<PUBLIC_HOST>:${PORT_DCM4CHEE_DICOM}` (11112) | DICOM DIMSE (C-STORE/C-FIND/C-MOVE), AE title `DCM4CHEE` | any DICOM node. **No Keycloak** |

Every other dcm4chee port (HTTPS 8443, WildFly management 9990/9993, LDAP
389, Postgres 5432, DICOM-TLS 2762, HL7 2575/12575) is bound to
`127.0.0.1` and reachable only from the host, for example over an SSH
tunnel. arc's own HTTP port 8080 is not published at all. Logout:
`http://<PUBLIC_HOST>:1081/oauth2/sign_out`.

## Architecture

```
                     Keycloak (keycloak-dev:8080, realm interact-realm, client ohif-viewer)
                         ▲                         ▲
            OIDC         │                         │        OIDC, --allowed-group=Admin
 :1081 ─► oauth2-proxy ──┘                         └── oauth2-proxy-admin ◄─ :1082
            │  sessions in oauth2-redis                     │
            ▼                                               ▼
   ohif_viewer (nginx)                              dcm4chee-ui-proxy (nginx)
     /            → OHIF static build                 injects the Aachen theme CSS
     /dcm4chee-arc/, /pacs → arc:8080                 → arc:8080
     writes logs/nginx/phi-audit.log                        │
            │                                               │
            └──────────────────► arc (dcm4chee-arc) ◄───────┘ ◄── portal /dcm4chee-proxy (audited)
                                 ├── ldap  (slapd-dcm4chee)
                                 └── db    (postgres-dcm4chee)
```

### Services

| Service | Image | Notes |
|---|---|---|
| `oauth2-proxy` | `oauth2-proxy:v7.6.0` | `${PORT_OHIF}:4180` → `ohif_viewer:80`. Client `ohif-viewer`. Issuer reached internally at `http://keycloak-dev:8080/realms/interact-realm` (hence `--insecure-oidc-skip-issuer-verification`, since Keycloak advertises the public URL). Cookie `_oauth2_proxy_${STACK_NAME}`. `--pass-user-headers` (needed for the PHI log), `--request-logging=false` (URIs carry PHI), `--whitelist-domain=${PUBLIC_HOST}:${PORT_PORTAL}` (so a session-expiry redirect can return to the portal). |
| `oauth2-proxy-admin` | same | `${PORT_DCM4CHEE_ADMIN}:4181` → `dcm4chee-ui-proxy:80`, `--allowed-group=Admin`, own cookie. |
| `oauth2-redis` | `redis:7-alpine` | session store for both proxies; not persisted, so a restart logs everyone out. |
| `dcm4chee-ui-proxy` | `nginx:1.27-alpine` | `config/dcm4chee-theme-proxy.conf`: proxies to `arc:8080` and injects `config/aachen-theme.css` into the dcm4chee UI. Also the upstream for the portal's Database page. |
| `ldap` | `dcm4che/slapd-dcm4chee:2.6.3-29.0` | dcm4chee configuration. Data in `dcm4chee-arc/ldap`, `dcm4chee-arc/slapd.d`. |
| `db` | `dcm4che/postgres-dcm4chee:14.5-29` | archive database. Data in `dcm4chee-arc/db`. |
| `arc` | `dcm4che/dcm4chee-arc-psql:5.29.0` | the archive. Objects in `dcm4chee-arc/storage`, WildFly in `dcm4chee-arc/wildfly`. Internally `http://arc:8080/dcm4chee-arc/aets/DCM4CHEE/rs` is the DICOMweb root everything else uses. |
| `ohif_viewer` | built locally → `${STACK_NAME}-webapp` | OHIF build served by nginx with `config/nginx.conf`; container `${STACK_NAME}-ohif_dcm4chee`. Not published; reached only through `oauth2-proxy`. |

All of them are on the `interact-dcm4chee` network. The two oauth2-proxies
are also on `interact-auth`, to reach Keycloak. The `x-proxy-env` block
blanks any outbound HTTP proxy for these containers on purpose.

Passwords (`DCM4CHEE_POSTGRES_PASSWORD`, `DCM4CHEE_LDAP_ROOTPASS`,
`DCM4CHEE_LDAP_CONFIGPASS`, `OIDC_OHIF_CLIENT_SECRET`,
`OAUTH2_PROXY_COOKIE_SECRET`, `OAUTH2_PROXY_REDIS_PASSWORD`) come from the
root `.env`; Compose refuses to start without them.

**Data.** The archive's state is the recipe's `dcm4chee-arc/` directory
(`db`, `ldap`, `slapd.d`, `storage`, `wildfly`). Those are bind mounts owned
by the containers' users and gitignored. Back up all of them together.

### OHIF's nginx (`config/nginx.conf`)

- `/` serves the static OHIF build.
- `/dcm4chee-arc/` and `/pacs` proxy to `arc:8080`. OHIF's DICOMweb data
  source uses the relative roots `/dcm4chee-arc/aets/DCM4CHEE/rs` (QIDO/WADO-RS)
  and `/dcm4chee-arc/aets/DCM4CHEE/wado` (WADO-URI).
- The `phi_audit` log format writes one JSON line per DICOMweb request to
  `/var/logs/nginx/phi-audit.log`, which is `logs/nginx/` in the recipe
  directory on the host. The GUI mounts that directory read-only for its PHI
  Access Log page. Format, identity headers and known gaps:
  [GUI.md › PHI access audit trail](GUI.md#phi-access-audit-trail).
- `/sw.js` has access logging off.

## InterACT's changes to OHIF

`viewers/` is a **git submodule** holding InterACT's fork of the
[OHIF Viewer](https://github.com/OHIF/Viewers) (MIT licence). `.gitmodules`
names its repository and the branch this deployment tracks. To see what
InterACT changed relative to the OHIF release it is based on, run
`git diff <ohif-release-tag> HEAD --stat` inside `viewers/`.

| What | Where |
|---|---|
| App configuration (data source, toolbar functions, theme, upload) | `platform/app/public/config/docker-nginx-dcm4chee.js` |
| Pipeline buttons: one "Webhooks" dropdown with nested groups, series picker, LDCT dialog, job polling, study refresh | `extensions/study-webhooks/` |
| Per-series "…" menu entries: download as DICOM / NIfTI | `extensions/series-webhooks/` |
| Side panel embedding portal pages (currently **disabled** in its `index.ts`) | `extensions/clinical-pipelines-panel/` |
| Plugin registration of the three extensions | `platform/app/pluginConfig.json` |
| Aachen colour theme (default; users can switch under gear → Appearance) | `defaultTheme: 'aachen'` in the config |
| Session guard: on Keycloak idle timeout, sends the whole tab (breaking out of the portal iframe) back to login and returns to the same study | bottom of the config file |
| Patch to `@cornerstonejs/ai` | `patches/` |
| Build recipe, nginx, port substitution | `platform/app/.recipes/Nginx-Dcm4chee/` |

All of OHIF's calls into InterACT go to the **portal**, not to the API:
`API_PROXY_BASE_URL = <scheme>//<host>:<PORT_PORTAL>/api-proxy`. Pipeline
buttons, series downloads and the NIfTI/NRRD branch of the **Upload** button
(`niftiUpload`) all use it with `credentials: 'include'`. The portal's port
cannot be known from the browser, so the config contains the placeholder
`__INTERACT_PORT_PORTAL__`. `config/40-interact-ports.sh` (run from
`/docker-entrypoint.d` at container start) replaces it with `PORT_PORTAL`.
If substitution fails, the page falls back to 5005 and logs a console
warning.

### The pipeline buttons

`studyWebhooks.functions` in the config is a tree of groups and functions.
Every function's `id` is the API path that is called
(`/api-proxy/<id>/<StudyInstanceUID>`). The current tree:

- **Dementia & Epilepsy**: FastSurfer seg / full, SynthSeg standard /
  robust, FastSurfer Longitudinal (only with ≥2 MR studies), CT Brain
  Segmentation, Hippocampus (FSL FIRST), Brain Atrophy (SIENA/SIENAX)
- **Tumor**: BraTS general (`goat`) and specific (glioma pre/post, glioma
  pre, meningioma, metastases, pediatric)
- **Diffusion (DTI)**: tensor, tractography
- Aneurysm segmentation (TOF MR), Hemorrhage segmentation (CT)
- **Research Tools**: Low Dose CT Simulation (parameter dialog),
  Co-Registration (series picker), FSL Register to MNI + QC

How a button works end to end, and how to add one:
[How_To_Add_New_Tools.md §2](How_To_Add_New_Tools.md#2-a-adding-a-button-to-the-ohif-viewer).

### Building and deploying the viewer

The app config is **baked in at build time**. The recipe's `dockerfile`
builds with node 24 + pnpm 11 (`APP_CONFIG=config/docker-nginx-dcm4chee.js`,
`pnpm run build`) and copies `platform/app/dist` into `nginx:alpine`. Any
change under `viewers/` therefore needs:

```sh
docker compose build ohif_viewer      # several minutes
docker compose up -d ohif_viewer
```

followed by a hard reload in the browser (OHIF uses a service worker).
`viewers/Dockerfile` at the submodule root is upstream's and is not used.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Keycloak error "Invalid redirect URI" | the `ohif-viewer` client's `redirectUris` must contain `${OHIF_BASE_URL}/oauth2/callback` (and `${DCM4CHEE_ADMIN_BASE_URL}/oauth2/callback` for 1082). Check in Keycloak → Clients → ohif-viewer. |
| 403 on 1082 after a successful login | the user isn't in the Keycloak group `Admin`. Expected for normal users. |
| oauth2-proxy can't reach Keycloak (proxy/Tinyproxy error page) | an outbound HTTP proxy is being applied; the `x-proxy-env` block must stay, and `keycloak-dev` must be in `INTERACT_NO_PROXY`. |
| "cookie_secret must be 16, 24, or 32 bytes" | `OAUTH2_PROXY_COOKIE_SECRET` must be a base64 32-byte key; `./generate-secrets.py` creates one. |
| Pipeline button: "failed to fetch" / CORS error | the portal port substituted into OHIF is wrong (check the browser console for the `[InterACT] PORT_PORTAL` warning), or the route prefix is missing from the portal's `ALLOWED_PREFIXES`. |
| Pipeline button reports a timeout but the job finishes later | the entry needs a larger `pollTimeoutMinutes`. |
| New results don't appear | hard-reload the study; dcm4chee can acknowledge a STOW before QIDO sees it, and the extension only retries for ~15 s. |
| `arc` won't start after changing ports | one of its seven published ports collides with another service; see the root README's multi-instance notes. |
