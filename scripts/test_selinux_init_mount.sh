#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: $0 SOURCE_FILE ro|ro,z" >&2
    exit 2
fi
source_file=$1
mount_options=$2
case "$mount_options" in
    ro|ro,z) ;;
    *) echo "unsupported mount options" >&2; exit 2 ;;
esac

owner_password=$(openssl rand -hex 18)
app_password=$(openssl rand -hex 18)
migrator_password=$(openssl rand -hex 18)

podman run --rm --network none --name "cx-r3-init-$(date +%s)-$$" \
    --memory 384m --cpus 1 \
    --tmpfs /var/lib/postgresql:rw,size=256m \
    --tmpfs /run/secrets:rw,size=1m,mode=0755 \
    -e POSTGRES_USER=cortex_v2_owner -e POSTGRES_DB=cortex_v2 \
    -e POSTGRES_PASSWORD="$owner_password" -e PGPASSWORD="$owner_password" \
    -e CORTEX_V2_APP_PASSWORD_FILE=/run/secrets/app \
    -e CORTEX_V2_MIGRATOR_PASSWORD_FILE=/run/secrets/migrator \
    -e CX_R3_APP_PASSWORD="$app_password" \
    -e CX_R3_MIGRATOR_PASSWORD="$migrator_password" \
    --volume "$source_file:/docker-entrypoint-initdb.d/10-create-v2-roles.sh:$mount_options" \
    --entrypoint /bin/sh docker.io/pgvector/pgvector:0.8.6-pg18 -ec '
        printf "%s\n" "$CX_R3_APP_PASSWORD" > /run/secrets/app
        printf "%s\n" "$CX_R3_MIGRATOR_PASSWORD" > /run/secrets/migrator
        chmod 644 /run/secrets/app /run/secrets/migrator
        docker-entrypoint.sh postgres &
        postgres_pid=$!
        trap "kill $postgres_pid 2>/dev/null || :; wait $postgres_pid 2>/dev/null || :" EXIT
        ready=0
        for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
            if ! kill -0 "$postgres_pid" 2>/dev/null; then
                wait "$postgres_pid"
                exit $?
            fi
            if pg_isready -h 127.0.0.1 -U cortex_v2_owner -d cortex_v2 >/dev/null 2>&1; then
                ready=1
                break
            fi
            sleep 1
        done
        [ "$ready" -eq 1 ] || { echo "database never became ready" >&2; exit 1; }
        roles=$(psql -h 127.0.0.1 -U cortex_v2_owner -d cortex_v2 -Atqc \
            "SELECT count(*) FROM pg_roles WHERE rolname IN ('\''cortex_v2_app'\'', '\''cortex_v2_migrator'\'')")
        [ "$roles" -eq 2 ] || { echo "expected two initialized roles, got $roles" >&2; exit 1; }
        echo "role bootstrap succeeded: roles=$roles"
    '
