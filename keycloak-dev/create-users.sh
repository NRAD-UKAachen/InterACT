#!/usr/bin/env bash
# Provisions Keycloak users via kcadm instead of hardcoding them in interact-realm.json.
# Reads username/email/role from users.csv (gitignored, not committed), generates a
# random temporary password per user, and writes the one-time credentials to a
# gitignored output file for handout. Existing users are left untouched — this only
# creates users that don't exist yet.
set -euo pipefail

REALM="interact-realm"
# Same STACK_NAME as the root .env -- override it in the environment if this
# isn't the single default instance, e.g.:
#   STACK_NAME=staging KEYCLOAK_ADMIN_PASSWORD=... ./create-users.sh
STACK_NAME="${STACK_NAME:-$(grep -m1 '^STACK_NAME=' ../.env 2>/dev/null | cut -d= -f2- || true)}"
CONTAINER="${STACK_NAME:-interact}-keycloak-dev"
KC_ADMIN_USER="${KEYCLOAK_ADMIN:-admin}"
KC_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:?Set KEYCLOAK_ADMIN_PASSWORD in the environment before running this script}"

CSV_FILE="${1:-users.csv}"
OUTPUT_FILE="credentials-$(date +%Y%m%d-%H%M%S).txt"

if [[ ! -f "$CSV_FILE" ]]; then
  echo "Missing $CSV_FILE. Copy users.csv.example to users.csv and fill in username,email,firstName,lastName,role" >&2
  exit 1
fi

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

kcadm config credentials --server http://localhost:8080 --realm master \
  --user "$KC_ADMIN_USER" --password "$KC_ADMIN_PASSWORD" >/dev/null

: > "$OUTPUT_FILE"
chmod 600 "$OUTPUT_FILE"

while IFS=, read -r username email firstName lastName role || [[ -n "$username" ]]; do
  role="${role%$'\r'}"
  [[ -z "$username" ]] && continue

  if kcadm get "users" -r "$REALM" -q "username=$username" --format csv --fields id 2>/dev/null | grep -q .; then
    echo "skip: $username already exists"
    continue
  fi

  user_id=$(kcadm create users -r "$REALM" -s "username=$username" -s "email=$email" \
    -s "firstName=$firstName" -s "lastName=$lastName" \
    -s enabled=true -s emailVerified=true -s "groups=[\"$role\"]" -i)

  temp_password=$(gen_password)

  kcadm update "users/$user_id" -r "$REALM" -s 'requiredActions=["UPDATE_PASSWORD"]'
  kcadm set-password -r "$REALM" --userid "$user_id" --new-password "$temp_password" --temporary

  echo "$username: $temp_password" >> "$OUTPUT_FILE"
  echo "created: $username"
done < <(tail -n +2 "$CSV_FILE")

echo
echo "One-time credentials written to $OUTPUT_FILE — hand them out over a secure channel and delete the file afterwards."
