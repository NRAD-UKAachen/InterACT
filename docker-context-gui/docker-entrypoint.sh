#!/bin/sh
# Container entrypoint for the InterACT portal.
#
# Renders client_secrets.json from its template using the OIDC_* environment,
# then creates the runtime directories the app writes into, then execs the CMD.
#
# The rendering used to be a manual step on the host
# (`set -a; source .env; set +a; ./render-client-secrets.sh` before every
# `docker compose up`), and the rendered file was bind-mounted in. Doing it here
# is what lets the whole platform start from one `docker compose up -d`: the
# client secret only ever exists in the root .env and in this container's
# memory, never in a file on the host.
set -eu

HOST_UID="${HOST_UID:-1000}"
HOST_GID="${HOST_GID:-1000}"

if [ "$(id -u)" = "0" ]; then
    # The image no longer runs as a fixed non-root user (compose doesn't set
    # `user:` any more), so we land here as root. Bind-mount sources that
    # didn't exist on the host yet -- always true on a fresh clone -- were
    # just created by Docker as root before this container even started, so
    # the runtime user could never write into them. Fix ownership once, then
    # drop to the real runtime user for everything else, including the CMD.
    mkdir -p /app/uids_logs /app/flask_sessions /app/audit_logs /app/static
    chown "$HOST_UID:$HOST_GID" /app/uids_logs /app/flask_sessions /app/audit_logs \
             /app/static
    # The API_storage mount (API_STORAGE_DIR, shared with the API container
    # at the identical host path) is the one bind mount this block used to
    # miss. It is gitignored -- it holds tens of GB of model weights -- so it
    # is absent on a fresh clone and Docker creates it as root, and the app
    # then cannot write into it as HOST_UID: a 500 on the first iRODS batch
    # stage-in (app/routes/batch.py) or federated round
    # (app/utils/federated_store.py), long after startup looked fine.
    # Only the two subtrees this app owns are created and chowned: the rest
    # of the store is the API container's (which runs as root), so this must
    # never become a recursive chown of the mount itself.
    if [ -n "${API_STORAGE_DIR:-}" ]; then
        mkdir -p "$API_STORAGE_DIR/batch_inputs" "$API_STORAGE_DIR/federated"
        chown "$HOST_UID:$HOST_GID" "$API_STORAGE_DIR/batch_inputs" \
                 "$API_STORAGE_DIR/federated"
    fi
    exec gosu "$HOST_UID:$HOST_GID" "$0" "$@"
fi

TEMPLATE=/app/client_secrets.json.template
# NOT /app: the image builds as root but the container runs as HOST_UID, so
# /app is not writable at run time. OIDC_CLIENT_SECRETS tells the app where to
# look, so honour it and default to a path the runtime user always owns.
OUTPUT="${OIDC_CLIENT_SECRETS:-/tmp/client_secrets.json}"

if [ -f "$TEMPLATE" ]; then
    : "${OIDC_CLIENT_SECRET:?Set OIDC_GUI_CLIENT_SECRET in the root .env (Keycloak -> Clients -> flask-gui -> Credentials)}"
    : "${OIDC_ISSUER:?Set PUBLIC_HOST and PORT_KEYCLOAK in the root .env}"
    : "${OIDC_CANONICAL_REDIRECT_URI:?Set PUBLIC_HOST and PORT_PORTAL in the root .env}"

    sed -e "s|@OIDC_CLIENT_SECRET@|${OIDC_CLIENT_SECRET}|g" \
        -e "s|@OIDC_ISSUER@|${OIDC_ISSUER}|g" \
        -e "s|@OIDC_CANONICAL_REDIRECT_URI@|${OIDC_CANONICAL_REDIRECT_URI}|g" \
        "$TEMPLATE" > "$OUTPUT"

    # Holds a client secret. The container runs as a non-root uid, so this is
    # the only protection the file has.
    chmod 600 "$OUTPUT"
fi

# The directories themselves were already created and chowned in the root
# block above; the app just assumes this file exists on first write.
touch /app/uids_logs/dicom_metadata_log.csv

exec "$@"
