# iRODS (research data management)

[iRODS](https://irods.org/) 5.0.2 is InterACT's file store for research data:
cohorts, exported results, anything that isn't (or isn't yet) a DICOM study in
dcm4chee. Users reach it through the portal with their normal Keycloak login,
and iRODS enforces **their own** per-user permissions.

## For users

| | |
|---|---|
| File browser | portal → **iRODS → Browse Files** (`/irods/`) |
| Shared with me | `/irods/shared`: folders others gave you access to. **Look here, not under your home.** See [Sharing](#sharing) |
| User management | portal → **iRODS → Manage Users** (`/irods-admin/users`), admins only |
| Batch processing | the Batch page can stage a cohort in from iRODS and push results back ([GUI.md](GUI.md#batch-processing)) |

"You don't have an iRODS account yet" means an admin has to create one for
you first.

## Deployment

Compose profile **`irods`** (or `all`), from the repository root:

```sh
docker compose --profile irods up -d --build
docker compose up -d interact-gui          # if the portal was already running, so it joins the irods network
docker compose ps
docker compose logs -f irods-catalog-provider
```

| Service | Image | Role |
|---|---|---|
| `irods-catalog` | Postgres 17 | the iCAT catalog database (named volume `${STACK_NAME}-irods-pgdata`) |
| `irods-catalog-provider` | built from `IRODS/catalog_provider/` (iRODS 5.0.2) | the iRODS server; zone `IRODS_ZONE_NAME` (default `interactZone`). Fixed `hostname: irods-catalog-provider` (iRODS identifies itself by it). Vault in named volume `${STACK_NAME}-irods-vault` |
| `irods-http-api` | `irods/irods_http_api:0.6.0` | the official [HTTP API](https://github.com/irods/irods_client_http_api), configured as an OAuth protected resource. Listens on 9000, **not published** |

Network: `${STACK_NAME}-irods` (`interact-irods`), shared with the portal
only. The provider publishes the native iRODS protocol port on the host as
`${IRODS_NATIVE_BIND}:${PORT_IRODS_NATIVE}`, loopback (`127.0.0.1:1247`) by
default. Containers are named
`${STACK_NAME}-<service>`; using `docker compose <cmd> <service>` from the
repo root avoids having to know the prefix.

Files: `IRODS/docker-compose.yml`, `IRODS/catalog/` (DB init),
`IRODS/catalog_provider/` (Dockerfile, entrypoint, `unattended_install.json.template`),
`IRODS/http_api/config.json.template`.

### Environment

Set in the root `.env`:

| Variable | Used by | Notes |
|---|---|---|
| `IRODS_ZONE_NAME`, `IRODS_RESOURCE_NAME` | provider; GUI (as `IRODS_ZONE`) | defaults `interactZone`, `interactResc` |
| `IRODS_ADMIN_PASSWORD` | provider, HTTP API (`proxy_admin_account`) | **≤ 42 characters** (iRODS obfuscation limit); `generate-secrets.py` respects it |
| `IRODS_DB_PASSWORD`, `IRODS_POSTGRES_PASSWORD` | catalog | |
| `IRODS_OIDC_CLIENT_ID`, `IRODS_OIDC_CLIENT_SECRET` | HTTP API, Keycloak | the `irods-http-api` client, defined by the realm import |
| `PORT_IRODS_NATIVE` | provider | host port for the native protocol, default 1247 |
| `IRODS_NATIVE_BIND` | provider | host address that port is bound to, default `127.0.0.1`; `0.0.0.0` opens it to the network |

Set in `docker-context-gui/docker-compose.yml`:
`IRODS_HTTP_API_URL=http://irods-http-api:9000/irods-http-api/0.6.0`.

Read by the GUI, passed through from the root `.env` (the compose defaults
repeat the code's own):

| Variable | Default | Meaning |
|---|---|---|
| `IRODS_WRITE_CONCURRENCY` | 8 | parallel writes per upload |
| `IRODS_STAGE_CONCURRENCY` | 6 | parallel per-file reads during batch stage-in |
| `IRODS_STAGE_STALL_SECONDS` | 45 | abort a stage-in with no progress for this long |
| `IRODS_STAGE_BUNDLE` / `_MIN_FILES` / `_CONCURRENCY` | 1 / 8 / 4 | server-side tar bundling per series (see below) |
| `IRODS_RULE_ENGINE_INSTANCE` | (see `irods_client.py`) | rule engine used for bundling |

## Identity model

```
            Keycloak (interact-realm)
     client "flask-gui"        client "irods-http-api"
            │                          ▲  token introspection
            ▼                          │
   portal (Flask)  ── Authorization: Bearer <the user's own token> ──►  irods-http-api :9000
   logs the user in, keeps                                              1. introspects the token
   their access token in the                                            2. maps preferred_username →
   server-side session                                                     iRODS user (user_claim plugin)
                                                                        3. acts AS that user
                                                                               │
                                                                               ▼
                                                                  irods-catalog-provider
                                                                  enforces that user's ACLs
```

There is no iRODS login screen and no second password. Consequences:

- **The portal holds no iRODS credential.** Only the HTTP API knows
  `IRODS_ADMIN_PASSWORD`, which it uses solely as the proxy account through
  which it acts for the already-authenticated user. (An earlier design had
  the portal log in as `rods` and impersonate users, which put the zone's
  master password in the web app.)
- **The token must name the HTTP API in `aud`.** The `irods-audience` client
  scope on `flask-gui` adds it. Without it, every request is a 401.
- **Validation is by introspection**, so revoking a Keycloak session cuts
  iRODS access within the HTTP API's cache window, not at token expiry.
- **Accounts are provisioned explicitly.** The HTTP API can only map a token
  to an iRODS account that already exists, with *exactly* the Keycloak
  username.
- **Admin actions need real iRODS privileges.** The Keycloak `Admin` group
  grants the admin *page*, but creating users or groups runs under the
  admin's own token, so their iRODS account must be a `rodsadmin`.

All iRODS calls in the portal go through one client,
`docker-context-gui/app/utils/irods_client.py`, which also writes every
operation to the PHI audit trail ([GUI.md](GUI.md#phi-access-audit-trail)).
Routes: `app/routes/irods_browser.py` (browse, upload, download, folder ZIP,
share, shared-with-me) and `app/routes/irods_admin.py` (users,
groups, members; `@admin_required`).

## Provisioning users

1. The person needs a Keycloak account ([Keycloak.md](Keycloak.md#user-management)).
2. An admin with `rodsadmin` opens **iRODS → Manage Users** and creates an
   iRODS user with the **exact** Keycloak username. The user gets
   `/<zone>/home/<username>`.
3. Give access to shared data by adding them to a group that has it, or with
   "Grant / Change Directory Permission".

From the shell (repository root):

```sh
# make an existing iRODS user a rodsadmin
docker compose exec irods-catalog-provider su - irods -c 'iadmin moduser <username> type rodsadmin'
# create a user directly as rodsadmin
docker compose exec irods-catalog-provider su - irods -c 'iadmin mkuser <username> rodsadmin'
```

Without `rodsadmin`, the admin page loads but every action fails with
`SYS_NO_API_PRIV`; the page says so and shows the command.

Deleting a Keycloak user does not delete their iRODS account. Their access
ends anyway, because no token can be obtained. Remove the iRODS account
separately if the data should go too.

## Sharing

Anyone who owns a collection gets a **Share this folder** panel when browsing
it. Two iRODS rules explain most "sharing doesn't work" reports:

1. **There is no traversal permission.** Read access to
   `/zone/home/alice/project` does *not* let Bob list `/zone/home/alice`, and
   iRODS reports collections he can't see as non-existent. A tree rooted at
   his home can never reach the share. **Shared with me** finds grants by
   catalog query (`COLL_ACCESS_USER_NAME`) wherever they are. Because shares
   are recursive, the query returns every subfolder too. The page lists only
   the top of each shared tree, and the person browses into it from there.
   Files shared on their own (outside any listed folder) are listed as
   files, with a download link.
2. **A grant only covers the entry it is set on.** A plain grant on a
   collection does not reach its existing subcollections or files, so the
   grantee sees the folder but it looks empty. **Inheritance** only covers
   entries created *after* it is turned on. The share panel therefore shares a
   collection recursively (`irods_client.share_collection`, which runs
   `msiSetACL("recursive", …)` through `POST /rules`, the same as `ichmod -r`)
   and enables inheritance on the whole tree. Shares made before this fix only reached the top
   collection. Re-share them, or run `ichmod -r <level> <user> <path>`. A
   folder whose path contains `" \ * $` or a backtick can't go into rule text,
   so it falls back to a top-level-only grant and shows a warning. To check
   an existing share:
   ```sh
   docker compose exec irods-catalog-provider su - irods -c 'ils -A /interactZone/home/alice/project'
   ```
   `Inheritance - Disabled` means files added later are not shared.
3. **Inheritance is on for every folder, and users can't turn it off.** The
   portal has no toggle for it. Every collection under `/<zone>/home` had it
   switched on (`ichmod -M -r inherit /<zone>/home/<user>` per home), new
   subfolders get it from their parent, and the admin page enables it on the
   home of each user it creates. A user created outside the portal
   (`iadmin mkuser`) needs `ichmod -M -r inherit /<zone>/home/<user>` once.
   One consequence: everyone a folder is shared with gets access to every
   file added to it later, so revoke a share instead of relying on new files
   staying private.

Prefer groups over per-person grants: grant the group once, then manage
membership.

## Throughput tuning

`IRODS/http_api/config.json.template` raises several limits above the
upstream defaults, because batch stage-in moves whole cohorts (thousands of
DICOM files) through this API. JSON has no comments, so the reasons are
recorded here:

| Key | Default | Here | Why |
|---|---|---|---|
| `irods_client.max_number_of_bytes_per_read_operation` | 8192 | 1 MiB | at 8 KiB a 150 KiB DICOM file cost ~19 round trips; a 5829-file cohort ~110 000 |
| `irods_client.max_number_of_bytes_per_write_operation` | 8192 | 1 MiB | same, for uploads and stage-out |
| `http_server.requests.threads` | 3 | 16 | a streaming read or write holds a thread for its whole duration; at 8, one 8-way browser upload starved page loads |
| `http_server.background_io.threads` | 6 | 16 | same, for background I/O |
| `http_server.requests.timeout_in_seconds` | 30 | 300 | large objects and slow clients must not be cut off |
| `irods_client.connection_pool.size` | 6 | 32 | one connection per in-flight transfer; kept at 2× request threads |
| `irods_client.connection_pool.max_retrievals_before_refresh` | 16 | 256 | suspected trigger of the stage-in stall; unproven, and the client-side watchdog stays |
| `irods_client.max_number_of_rows_per_catalog_query` | 256 | 20000 | GenQuery results beyond this cap are **silently truncated** in this API version. A 1952-file series once staged as 512 files. `irods_client.genquery()` also raises if a result comes back exactly at the cap |

Memory cost is roughly (request + background threads) × read size, which
amounts to tens of MB. Note that creating a data object costs ~25–30 ms inside
iRODS regardless of size, so bulk speed is set by **client concurrency**
(`IRODS_WRITE_CONCURRENCY`, `IRODS_STAGE_CONCURRENCY`) and by **server-side
bundling**. Stage-in tars each series on the server (`msiTarFileCreate` via
`POST /rules`, which needs the rule engine instance named in
`IRODS_RULE_ENGINE_INSTANCE`) and fetches it in one read. Bundling needs a
real `read` ACL on the objects; being `rodsadmin` does not substitute.
`IRODS_STAGE_BUNDLE=0` turns it off, and failures fall back to per-file.

After editing the template, `docker compose restart irods-http-api` is
enough. The entrypoint re-renders the config from the template on every start.

## Troubleshooting

**"You don't have an iRODS account yet".** The HTTP API returned 401.
Check `docker compose logs --tail 20 irods-http-api`:
- `Could not find our [client_id] in [aud]`: the `irods-audience` scope is
  missing from the `flask-gui` client in Keycloak.
- `Access token is invalid or expired`: a Keycloak-side problem.
- no mapping error: the iRODS account doesn't exist. Create it.

**Admin actions fail with `SYS_NO_API_PRIV`.** The admin's iRODS account is
not a `rodsadmin`.

**"Could not reach the iRODS server."** The portal can't reach
`irods-http-api:9000`. Check that the `irods` profile is up and healthy, that
the portal was (re)started after it (so it joined `interact-irods`), and that
`irods-http-api` is in `INTERACT_NO_PROXY`.

**A shared folder shows up for the grantee but is empty.** The grant
reached only the top collection (a share made before the recursive fix, or
one made with `ichmod` without `-r`). Check a subfolder with `ils -A`, then
re-share it from the portal or run `ichmod -r`.

**Files added to a shared folder aren't visible to others.** Inheritance is
off on that collection, usually in a home created with `iadmin mkuser`
outside the portal. Run `ichmod -M -r inherit <path>` as `irods`.

**Folder listing or stage-in has exactly 20000 entries / errors at the cap.**
Raise `max_number_of_rows_per_catalog_query`.

## Extending

- **More storage**: add an `irods-catalog-consumer` resource server and
  register it with `iadmin mkresc`, following the official
  [`irods_demo`](https://github.com/irods/irods_demo) reference this setup is
  derived from.
- **S3-backed storage**: add MinIO or S3 and an `s3` resource
  (`irods-resource-plugin-s3`), again per `irods_demo`.
- **icommands for debugging**: `docker compose exec irods-catalog-provider su - irods`,
  or point an icommands client at the published native port.
- **Zone Management Tool**: the official admin web app runs on the same HTTP
  API and could replace the portal's admin page. It is untested here, and its
  Keycloak SSO story is unverified.
- **Faster bulk access**: scoped, expiring read-only tickets (`/tickets`)
  pulled over the native protocol would beat the HTTP API. The ticket would
  have to stay server-side, though, and it bypasses ACLs for its holder.
