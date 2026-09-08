#!/usr/bin/env bash
# PostgreSQL's upstream finite-init functions, with certificate-only authentication.
set -Eeuo pipefail
source /usr/local/bin/docker-entrypoint.sh
if [ "${1:-}" != postgres ]; then exec "$@"; fi
export POSTGRES_USER=postgres POSTGRES_DB=platform_agent_memory
export POSTGRES_PASSWORD=''
export POSTGRES_INITDB_ARGS='--auth-local=peer --auth-host=cert'
unset POSTGRES_PASSWORD_FILE POSTGRES_HOST_AUTH_METHOD PGPASSWORD
docker_setup_env
docker_create_db_directories
if [ "$(id -u)" = 0 ]; then exec gosu postgres "$0" "$@"; fi
for name in tls.crt tls.key ca.crt; do
    [ -s "/tls/server/$name" ] || { echo "Cortex server certificate missing: $name" >&2; exit 1; }
done
if [ -z "$DATABASE_ALREADY_EXISTS" ]; then
    docker_init_database_dir
    # initdb emits generic host records; cert is only valid for hostssl. The
    # upstream temporary server listens on its local socket only, using peer.
    docker_temp_server_start "$@" -c hba_file=/etc/cortex/pg_hba.conf \
        -c ident_file=/etc/cortex/pg_ident.conf
    docker_setup_db
    docker_process_init_files /docker-entrypoint-initdb.d/*
    docker_temp_server_stop
fi
exec "$@" -c ssl=on -c ssl_cert_file=/tls/server/tls.crt \
    -c ssl_key_file=/tls/server/tls.key -c ssl_ca_file=/tls/server/ca.crt \
    -c hba_file=/etc/cortex/pg_hba.conf -c ident_file=/etc/cortex/pg_ident.conf \
    -c ssl_min_protocol_version=TLSv1.2
