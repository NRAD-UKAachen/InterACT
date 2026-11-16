#!/bin/bash
# Adapted from "Initialization script" in documentation for official Postgres dockerhub:
#   https://hub.docker.com/_/postgres/
#
# Creates the ICAT database and the "irods" role the catalog-provider connects
# as. IRODS_DB_PASSWORD is supplied via the container's environment (see
# IRODS/docker-compose.yml) so the password isn't hardcoded in this script.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE "ICAT";
    CREATE USER irods WITH PASSWORD '${IRODS_DB_PASSWORD}';
    GRANT ALL PRIVILEGES ON DATABASE "ICAT" to irods;
    ALTER DATABASE "ICAT" OWNER TO irods;
EOSQL
