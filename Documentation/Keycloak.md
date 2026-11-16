# Keycloak (identity and single sign-on)

Keycloak is InterACT's only identity provider. Users log in once and that
login covers the portal, OHIF, the dcm4chee admin UI and, through the portal,
iRODS. No component keeps its own user database.

## At a glance

| | |
|---|---|
| Directory | `keycloak-dev/` |
| Services | `keycloak-dev` (`quay.io/keycloak/keycloak:24.0.2`, `start-dev --hostname-strict=false --import-realm`), `keycloak-db` (`postgres:15`, named volume `${STACK_NAME}-keycloak-pgdata`) |
| URL | `http://<PUBLIC_HOST>:${PORT_KEYCLOAK}` (default 8081). Admin console at `/admin`, user `KEYCLOAK_ADMIN` (default `admin`) / `KEYCLOAK_ADMIN_PASSWORD` |
| Internal URL | `http://keycloak-dev:8080` on the `interact-auth` network |
| Realm | `interact-realm` |
| Profile | none (always started) |

## Who authenticates how

| Component | Keycloak client | Mechanism |
|---|---|---|
| Portal (`interact-gui`) | `flask-gui` (confidential) | flask-oidc. Redirect URI `${PORTAL_BASE_URL}/oidc_callback`. The token is kept in the server-side session. |
| OHIF (:1081) | `ohif-viewer` (confidential) | `oauth2-proxy`. Redirect `${OHIF_BASE_URL}/oauth2/callback`. |
| dcm4chee admin UI (:1082) | `ohif-viewer` | `oauth2-proxy-admin` with `--allowed-group=Admin`. Redirect `${DCM4CHEE_ADMIN_BASE_URL}/oauth2/callback`. |
| iRODS HTTP API | `irods-http-api` (service account only) | token **introspection**: the portal forwards the user's own `flask-gui` token, and the HTTP API validates it with its own client credentials ([iRODS.md](iRODS.md)). |
| Processing API, SAMinRAD, LDCT | – | no Keycloak. Reached through the portal's authenticated proxies, or loopback-only / direct ports (see [Project_Structure.md](Project_Structure.md#security-model)). |

The proxies and the portal reach Keycloak by its internal name, while
browsers see `KEYCLOAK_BASE_URL`. `KC_HOSTNAME_URL` makes Keycloak issue
tokens with the public issuer, which is why oauth2-proxy runs with
`--insecure-oidc-skip-issuer-verification`.

## The realm (`keycloak-dev/interact-realm.json`)

| Setting | Value |
|---|---|
| Login theme | `interact` (`keycloak-dev/themes/interact/login/`: a CSS child of the stock theme, plus a customised update-password page and messages) |
| Registration | off. Password reset by e-mail allowed (needs SMTP) |
| `sslRequired` | `external` (set to `all` once TLS is in place) |
| Password policy | ≥12 chars, upper + lower + digit + special, not the username, 3-deep history, 210 000 PBKDF2 iterations |
| Brute-force protection | on, temporary lockout only (no permanent lockout, so clinicians cannot be locked out for good) |
| Access token lifespan | 300 s |
| SSO session | idle 1800 s, max 36 000 s (10 h hard cap) |
| `verifyEmail` | on |
| Groups / realm roles | `Admin`, `User` |
| Client scope `irods-audience` | adds `irods-http-api` to the token's `aud`. Default scope of `flask-gui`. Without it the iRODS HTTP API rejects every token |

Groups reach the applications through a `groups` mapper on `flask-gui` and
`ohif-viewer`. The portal treats a user as **admin** if `admin` is among
their groups or realm roles (case-insensitive, `app/utils/roles.py`).
oauth2-proxy-admin checks for the group `Admin` exactly.

**Secrets and URLs are injected, not stored.** The realm file contains
`${…}` placeholders that Keycloak resolves from its environment at import:

| Realm placeholder | From root `.env` |
|---|---|
| `KC_FLASK_GUI_SECRET` | `OIDC_GUI_CLIENT_SECRET` |
| `KC_OHIF_VIEWER_SECRET` | `OIDC_OHIF_CLIENT_SECRET` |
| `KC_IRODS_HTTP_API_SECRET` | `IRODS_OIDC_CLIENT_SECRET` |
| `PORTAL_BASE_URL`, `OHIF_BASE_URL`, `DCM4CHEE_ADMIN_BASE_URL` | derived from `PUBLIC_HOST` / `PUBLIC_SCHEME` / `PORT_*` |
| SMTP settings | `KC_SMTP_HOST`, `KC_SMTP_PORT`, `KC_SMTP_FROM`, `KC_SMTP_USER`, `KC_SMTP_PASSWORD`, `KC_SMTP_AUTH`, `KC_SMTP_STARTTLS`, `KC_SMTP_SSL` |

The same `.env` values are passed to the portal, the oauth2-proxies and the
iRODS HTTP API, so both sides of each client always agree.

**When the realm file is applied.** `--import-realm` imports
`interact-realm.json` when Keycloak starts. Keycloak's default import
strategy skips a realm that already exists, so on an installation whose
database already holds `interact-realm`, editing the JSON changes nothing.
Make the change in the admin console or with `kcadm.sh` as well, and keep the
JSON in step so a fresh install gets the same result. Changing a client
secret in `.env` likewise needs the matching change in Keycloak (Clients →
*client* → Credentials) on an existing installation.

## User management

The realm import creates no users. Create every account, including the
first admin, with `create-users.sh` (below); each user sets their own
password at first login.

All scripts call `kcadm.sh` inside the `${STACK_NAME}-keycloak-dev`
container and need the admin password in the environment. Run them from
`keycloak-dev/`. Set `STACK_NAME=…` too if this isn't the default `interact`
stack.

**Create users**

```sh
cd keycloak-dev
cp users.csv.example users.csv      # gitignored
$EDITOR users.csv                   # username,email,firstName,lastName,role   (role: User or Admin)
KEYCLOAK_ADMIN_PASSWORD=<from .env> ./create-users.sh
```

Existing users are left untouched. Each new user gets a random temporary
password that satisfies the policy, and the passwords are written to
`credentials-<timestamp>.txt` (mode 600, gitignored). Hand them out, then
**delete the file**. `users.csv` contains names and e-mail addresses; keep
it at mode 600.

**Reset a password**

```sh
KEYCLOAK_ADMIN_PASSWORD=<from .env> ./reset-password.sh <username> [new-password]
```

Without a password argument, a temporary one is generated and printed.

**Give a user iRODS access.** Keycloak users have no iRODS account by
default. See [iRODS.md](iRODS.md#provisioning-users).

**Promote / demote an admin.** Move the user between the `Admin` and `User`
groups in the admin console (Users → *user* → Groups). They have to log in
again for the portal and oauth2-proxy to see the change.

**Remove someone.** Disable or delete the user in Keycloak. That ends all
access, because no token can be obtained any more. Delete their iRODS account
separately if their data should go too.

### `create-irods-client.sh`

This script creates the `irods-http-api` client and prints its secret. The
realm import already defines that client and the `irods-audience` scope with
the secret from `.env`, so on a normal installation it is not needed. Use it
only to recreate the client on a realm that lacks it, and put the printed
secret into `.env` as `IRODS_OIDC_CLIENT_SECRET`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Portal or OHIF: "Realm does not exist" | Keycloak started without `--import-realm`, or the import failed. Check `docker compose logs keycloak-dev`. |
| Keycloak refuses to start: "Set … in the root .env" | a required secret is empty. Run `./generate-secrets.py`. |
| "Invalid redirect URI" | the client's `redirectUris` doesn't contain the callback for the URL you used. Check `PUBLIC_HOST` / `PORT_*` and the client in the admin console. |
| "Invalid client credentials" / "unauthorized_client" | the secret in `.env` and the one in Keycloak differ, typically after regenerating secrets on an existing installation. Copy the value from Clients → Credentials into `.env`, or regenerate it there. |
| Login loops on plain HTTP | `COOKIES_INSECURE` must be `1` without TLS ([GUI.md](GUI.md#tls-and-cookies)). |
| Password reset / verify e-mails don't arrive | SMTP is not configured (`KC_SMTP_*`). |
| User logged out after 30 min idle or 10 h at most | realm SSO timeouts, by design. OHIF's session guard sends them back to the login page. |
