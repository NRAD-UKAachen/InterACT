#!/usr/bin/env bash
# Creates the confidential Keycloak client the iRODS HTTP API uses to introspect
# access tokens, and prints its secret for the root .env (IRODS_OIDC_CLIENT_SECRET).
#
# The client never performs a browser login of its own - users authenticate to
# docker-context-gui with the existing "flask-gui" client, and that access token
# is forwarded to the HTTP API. This client exists purely so the HTTP API can
# call Keycloak's token introspection endpoint with credentials of its own.
# Hence: no standard flow, no direct access grants.
set -euo pipefail

REALM="interact-realm"
# Same STACK_NAME as the root .env -- override it in the environment if this
# isn't the single default instance, e.g.:
#   STACK_NAME=staging KEYCLOAK_ADMIN_PASSWORD=... ./create-irods-client.sh
STACK_NAME="${STACK_NAME:-$(grep -m1 '^STACK_NAME=' ../.env 2>/dev/null | cut -d= -f2- || true)}"
CONTAINER="${STACK_NAME:-interact}-keycloak-dev"
CLIENT_ID="irods-http-api"
KC_ADMIN_USER="${KEYCLOAK_ADMIN:-admin}"
KC_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:?Set KEYCLOAK_ADMIN_PASSWORD in the environment before running this script}"

kcadm() {
  docker exec "$CONTAINER" /opt/keycloak/bin/kcadm.sh "$@"
}

kcadm config credentials --server http://localhost:8080 --realm master \
  --user "$KC_ADMIN_USER" --password "$KC_ADMIN_PASSWORD" >/dev/null

if kcadm get clients -r "$REALM" -q "clientId=$CLIENT_ID" --fields id --format csv 2>/dev/null | grep -q .; then
  echo "client '$CLIENT_ID' already exists - reusing it"
else
  kcadm create clients -r "$REALM" \
    -s "clientId=$CLIENT_ID" \
    -s enabled=true \
    -s publicClient=false \
    -s standardFlowEnabled=false \
    -s directAccessGrantsEnabled=false \
    -s serviceAccountsEnabled=true \
    -s "description=Token introspection for the iRODS HTTP API (protected resource)" -i >/dev/null
  echo "created client '$CLIENT_ID'"
fi

uuid=$(kcadm get clients -r "$REALM" -q "clientId=$CLIENT_ID" --fields id --format csv --noquotes | tail -1)
secret=$(kcadm get "clients/$uuid/client-secret" -r "$REALM" --fields value --format csv --noquotes | tail -1)

echo
echo "Put this in the root .env:"
echo "  IRODS_OIDC_CLIENT_ID=$CLIENT_ID"
echo "  IRODS_OIDC_CLIENT_SECRET=$secret"
