#!/usr/bin/env python3
"""
Fills in the blank secrets from section 4 of .env.example.

    ./generate-secrets.py                # fill only the blanks in ./.env
    ./generate-secrets.py --force        # regenerate every secret, even set ones
    ./generate-secrets.py --length 32    # override the default secret length
    ./generate-secrets.py --dry-run      # show what would change, write nothing

--length only adjusts the secrets that don't have a fixed size or a hard cap --
see the per-variable notes printed after a run for which ones that applies to.
"""
import argparse
import base64
import os
import re
import secrets
import string
import sys

# Kept free of '$', '#', '&', '\', quotes and whitespace so a generated value can
# never be misread as a docker compose interpolation, a comment, or need escaping
# wherever it ends up (an env file, a shell command, an admin CLI flag).
SAFE_ALPHABET = string.ascii_letters + string.digits + "-_"

DEFAULT_LENGTH = 44
IRODS_MAX_LENGTH = 42  # password_obfuscation.py hard-caps obfuscated passwords here


def random_string(length, alphabet=SAFE_ALPHABET):
    return "".join(secrets.choice(alphabet) for _ in range(length))


def gen_generic(length):
    return random_string(length)


def gen_irods(length):
    return random_string(min(length, IRODS_MAX_LENGTH))


def gen_cookie_secret(_length):
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


# name -> (generator, note shown next to it when generated)
SECRETS = {
    "KEYCLOAK_ADMIN_PASSWORD": (gen_generic, None),
    "KC_DB_PASSWORD": (gen_generic, None),
    "OIDC_GUI_CLIENT_SECRET": (gen_generic, None),
    "OIDC_OHIF_CLIENT_SECRET": (gen_generic, None),
    "OAUTH2_PROXY_COOKIE_SECRET": (
        gen_cookie_secret,
        "fixed: base64 of 32 random bytes, ignores --length",
    ),
    "OAUTH2_PROXY_REDIS_PASSWORD": (gen_generic, None),
    "GUI_FLASK_SECRET_KEY": (gen_generic, None),
    "SAMINRAD_FLASK_SECRET_KEY": (gen_generic, None),
    "DCM4CHEE_POSTGRES_PASSWORD": (gen_generic, None),
    "DCM4CHEE_LDAP_ROOTPASS": (
        gen_generic,
        "existing directory: also change olcRootPW via ldapmodify -- see .env.example",
    ),
    "DCM4CHEE_LDAP_CONFIGPASS": (
        gen_generic,
        "existing directory: also change olcRootPW via ldapmodify -- see .env.example",
    ),
    "AIRFLOW_DB_PASSWORD": (gen_generic, None),
    "AIRFLOW_API_PASSWORD": (gen_generic, None),
    "IRODS_ADMIN_PASSWORD": (
        gen_irods,
        f"capped at {IRODS_MAX_LENGTH} chars -- iRODS password obfuscation limit",
    ),
    "IRODS_DB_PASSWORD": (
        gen_irods,
        f"capped at {IRODS_MAX_LENGTH} chars -- iRODS password obfuscation limit",
    ),
    "IRODS_POSTGRES_PASSWORD": (
        gen_irods,
        f"capped at {IRODS_MAX_LENGTH} chars -- iRODS password obfuscation limit",
    ),
    "IRODS_OIDC_CLIENT_SECRET": (gen_generic, None),
}

# Credentials for an external account -- nothing to generate, never touched.
SKIPPED = {"KC_SMTP_PASSWORD": "external SMTP account password -- set it yourself"}

ASSIGNMENT = re.compile(r"^([A-Z0-9_]+)=(.*)$")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env-file", default=".env", help="file to edit (default: .env)")
    parser.add_argument(
        "--length",
        type=int,
        default=DEFAULT_LENGTH,
        help=f"length for secrets without a fixed/capped size (default: {DEFAULT_LENGTH})",
    )
    parser.add_argument(
        "--force", action="store_true", help="regenerate secrets that already have a value"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would change without writing"
    )
    args = parser.parse_args()

    if args.length < 1:
        parser.error("--length must be a positive integer")

    if os.path.basename(args.env_file) == ".env.example":
        parser.error(
            "refusing to write secrets into a tracked .env.example -- "
            "point --env-file at your own .env"
        )

    if not os.path.isfile(args.env_file):
        parser.error(f"{args.env_file} not found -- run: cp .env.example {args.env_file}")

    with open(args.env_file, encoding="utf-8") as f:
        lines = f.readlines()

    seen = set()
    changed = []

    for i, line in enumerate(lines):
        m = ASSIGNMENT.match(line.rstrip("\n"))
        if not m:
            continue
        name, value = m.group(1), m.group(2)
        if name not in SECRETS:
            continue
        seen.add(name)
        if value and not args.force:
            continue
        generator, _ = SECRETS[name]
        lines[i] = f"{name}={generator(args.length)}\n"
        changed.append(name)

    missing = [name for name in SECRETS if name not in seen]
    if missing:
        print(
            f"warning: {', '.join(missing)} not found in {args.env_file} "
            "-- is it up to date with .env.example?",
            file=sys.stderr,
        )

    if not changed:
        print(f"Nothing to do -- every secret in {args.env_file} already has a value. "
              "Use --force to regenerate.")
        return

    print(f"{'Would generate' if args.dry_run else 'Generated'} {len(changed)} secret(s) "
          f"in {args.env_file}:")
    for name in changed:
        note = SECRETS[name][1]
        print(f"  {name}" + (f"  ({note})" if note else ""))

    if not args.dry_run:
        with open(args.env_file, "w", encoding="utf-8") as f:
            f.writelines(lines)

    skipped_present = [name for name in SKIPPED if name in seen]
    if skipped_present:
        print("\nLeft untouched (not auto-generated):")
        for name in skipped_present:
            print(f"  {name}  ({SKIPPED[name]})")


if __name__ == "__main__":
    main()
