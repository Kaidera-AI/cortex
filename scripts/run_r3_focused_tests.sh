#!/bin/sh
set -eu

[ "$#" -gt 0 ] || { echo "pass focused pytest selectors" >&2; exit 2; }
worktree=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_id="$(date +%s)-$$"
db_name="cx-r3-db-$run_id"
image=docker.io/pgvector/pgvector:0.8.6-pg18
owner_password=$(openssl rand -hex 18)
app_password=$(openssl rand -hex 18)
migrator_password=$(openssl rand -hex 18)

check_load() {
    load=$(sysctl -n vm.loadavg)
    printf 'vm.loadavg=%s\n' "$load"
    first=$(printf '%s\n' "$load" | sed -E 's/^\{ ([^ ]+).*/\1/')
    awk "BEGIN { exit !($first < 8) }" || {
        echo "load is at least 8; no container run" >&2
        exit 1
    }
}
cleanup() { podman rm -f "$db_name" >/dev/null 2>&1 || :; }
trap cleanup EXIT HUP INT TERM

check_load
podman run -d --rm --network none --name "$db_name" \
    --memory 768m --cpus 1.5 \
    --tmpfs /var/lib/postgresql:rw,size=512m \
    --tmpfs /run/secrets:rw,size=1m,mode=0755 \
    -e POSTGRES_USER=cortex_v2_owner -e POSTGRES_DB=cortex_v2 \
    -e POSTGRES_PASSWORD="$owner_password" \
    -e CORTEX_V2_APP_PASSWORD_FILE=/run/secrets/app \
    -e CORTEX_V2_MIGRATOR_PASSWORD_FILE=/run/secrets/migrator \
    -e CX_R3_APP_PASSWORD="$app_password" \
    -e CX_R3_MIGRATOR_PASSWORD="$migrator_password" \
    --volume "$worktree/deploy/postgres-init/10-create-v2-roles-env.sh:/docker-entrypoint-initdb.d/10-create-v2-roles.sh:ro,z" \
    --entrypoint /bin/sh "$image" -ec '
        printf "%s\n" "$CX_R3_APP_PASSWORD" > /run/secrets/app
        printf "%s\n" "$CX_R3_MIGRATOR_PASSWORD" > /run/secrets/migrator
        chmod 644 /run/secrets/app /run/secrets/migrator
        exec docker-entrypoint.sh postgres
    ' >/dev/null

ready=0
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if podman exec "$db_name" pg_isready -h 127.0.0.1 -U cortex_v2_owner -d cortex_v2 >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
[ "$ready" -eq 1 ] || { podman logs "$db_name"; echo "disposable DB not ready" >&2; exit 1; }

check_load
podman run --rm --network "container:$db_name" --name "cx-r3-migrate-$run_id" \
    --volume "$worktree:/work:ro" -e PGPASSWORD="$migrator_password" \
    --entrypoint /bin/sh "$image" -ec '
        psql -h 127.0.0.1 -U cortex_v2_migrator -d cortex_v2 -v ON_ERROR_STOP=1 -q \
            -c "CREATE SCHEMA cortex_auth AUTHORIZATION cortex_v2_migrator; CREATE SCHEMA cortex_core AUTHORIZATION cortex_v2_migrator;"
        for migration in /work/migrations/000*.sql; do
            psql -h 127.0.0.1 -U cortex_v2_migrator -d cortex_v2 -v ON_ERROR_STOP=1 -q -f "$migration"
            migration_id=$(basename "$migration")
            checksum=$(sha256sum "$migration" | cut -d " " -f 1)
            psql -h 127.0.0.1 -U cortex_v2_migrator -d cortex_v2 -v ON_ERROR_STOP=1 -q \
                -c "INSERT INTO cortex_core.schema_migrations (migration_id, checksum_sha256) VALUES ('\''$migration_id'\'', '\''$checksum'\'')"
        done
        psql -h 127.0.0.1 -U cortex_v2_migrator -d cortex_v2 -Atqc \
            "SELECT count(*) FROM cortex_core.schema_migrations"
    '

check_load
podman run --rm --network "container:$db_name" --name "cx-r3-tests-$run_id" \
    --volume "$worktree:/work:ro" --workdir /work \
    -e PYTHONPATH=/work/src -e PYTHONDONTWRITEBYTECODE=1 \
    -e CORTEX_V2_PROCESSING_DATABASE_URL="postgresql://cortex_v2_app:$app_password@127.0.0.1/cortex_v2" \
    -e CORTEX_V2_PROCESSING_MIGRATOR_URL="postgresql://cortex_v2_migrator:$migrator_password@127.0.0.1/cortex_v2" \
    --entrypoint /opt/venv/bin/pytest localhost/cortex-kai-test-tests:0.02.001 \
    -q -p no:cacheprovider "$@"
