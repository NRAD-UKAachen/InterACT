#! /bin/bash -e

catalog_db_hostname=irods-catalog

echo "Waiting for iRODS catalog database to be ready"

until pg_isready -h ${catalog_db_hostname} -d ICAT -U irods -q
do
    sleep 1
done

echo "iRODS catalog database is ready"

unattended_install_file=/unattended_install.json
if [ -e "/unattended_install.json.template" ]; then
    echo "Rendering iRODS setup configuration"

    export IRODS_ZONE_NAME="${IRODS_ZONE_NAME:?IRODS_ZONE_NAME must be set}"
    export IRODS_RESOURCE_NAME="${IRODS_RESOURCE_NAME:-interactResc}"
    export IRODS_ADMIN_PASSWORD="${IRODS_ADMIN_PASSWORD:?IRODS_ADMIN_PASSWORD must be set}"
    export IRODS_DB_PASSWORD="${IRODS_DB_PASSWORD:?IRODS_DB_PASSWORD must be set}"
    # Generated fresh per deployment when not pinned via env (negotiation_key
    # must be exactly 32 bytes; zone_key has no such constraint).
    export IRODS_NEGOTIATION_KEY="${IRODS_NEGOTIATION_KEY:-$(openssl rand -hex 16)}"
    export IRODS_ZONE_KEY="${IRODS_ZONE_KEY:-$(openssl rand -hex 16)}"

    envsubst \
        '${IRODS_ZONE_NAME} ${IRODS_RESOURCE_NAME} ${IRODS_ADMIN_PASSWORD} ${IRODS_DB_PASSWORD} ${IRODS_NEGOTIATION_KEY} ${IRODS_ZONE_KEY}' \
        < /unattended_install.json.template > ${unattended_install_file}
    # No hostname substitution: docker-compose.yml pins the container hostname
    # to the service name, so the template can name the host literally. That
    # pin is load-bearing -- iRODS records $HOSTNAME as the resource host in
    # the catalogue (r_resc_main.resc_net) when the zone is created, and Docker
    # otherwise sets $HOSTNAME to the container ID, which stops resolving the
    # moment the container is recreated. iRODS 5 also requires host_resolution
    # addresses to hold at least two UNIQUE names, which is why the template
    # pairs the service name with localhost.

    echo "Running iRODS setup"
    python3 /var/lib/irods/scripts/setup_irods.py --json_configuration_file ${unattended_install_file}
    rm ${unattended_install_file}
fi

echo "Starting server"
cd /usr/sbin
su irods -c 'irodsServer --stdout'
