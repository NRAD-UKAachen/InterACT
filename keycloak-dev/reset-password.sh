#!/usr/bin/env bash
# Resets a user's password via kcadm, for admins to run from the command line
# instead of the Keycloak Admin Console UI. Generates a random temporary
# password and forces the user to change it on next login.
set -euo pipefail

REALM="interact-realm"
# Same STACK_NAME as the root .env -- override it in the environment if this
# isn't the single default instance, e.g.:
#   STACK_NAME=staging KEYCLOAK_ADMIN_PASSWORD=... ./reset-password.sh <username>
STACK_NAME="${STACK_NAME:-$(grep -m1 '^STACK_NAME=' ../.env 2>/dev/null | cut -d= -f2- || true)}"
CONTAINER="${STACK_NAME:-interact}-keycloak-dev"
KC_ADMIN_USER="${KEYCLOAK_ADMIN:-admin}"
KC_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:?Set KEYCLOAK_ADMIN_PASSWORD in the environment before running this script}"

USERNAME="${1:?Usage: ./reset-password.sh <username> [new-password]}"

kcadm() {
  docker exec "$CONTAINER" /opt/keycloak/bin/kcadm.sh "$@"
}

# Satisfies the realm passwordPolicy (length(12), upperCase(1), lowerCase(1),
# digits(1), specialChars(1)): draw one char from each required class, pad
# with a mixed pool, then shuffle so the classes aren't in fixed positions.
gen_password() {
  local rand_from
  rand_from() { LC_ALL=C tr -dc "$1" < /dev/urandom | head -c "$2" || true; }

  local special='!@#$%^&*()_=+-'
  local pool="A-Za-z0-9${special}"
  {
    rand_from 'A-Z' 1
    rand_from 'a-z' 1
    rand_from '0-9' 1
    rand_from "$special" 1
    rand_from "$pool" 16
  } | fold -w1 | shuf | tr -d '\n'
}

NEW_PASSWORD="${2:-$(gen_password)}"

kcadm config credentials --server http://localhost:8080 --realm master \
  --user "$KC_ADMIN_USER" --password "$KC_ADMIN_PASSWORD" >/dev/null

user_id=$(kcadm get "users" -r "$REALM" -q "username=$USERNAME" --format csv --fields id --noquotes)

if [[ -z "$user_id" ]]; then
  echo "No user found with username '$USERNAME' in realm '$REALM'" >&2
  exit 1
fi

kcadm update "users/$user_id" -r "$REALM" -s 'requiredActions=["UPDATE_PASSWORD"]'
kcadm set-password -r "$REALM" --userid "$user_id" --new-password "$NEW_PASSWORD" --temporary

echo "Password for '$USERNAME' reset. Temporary password: $NEW_PASSWORD"
echo "User must change it on next login."
