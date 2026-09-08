#!/usr/bin/env bash
# The builder validates archive inventory and renders this exact transaction.
set -euo pipefail
export LC_ALL=C

baseline_sql=/schema-baseline/baseline.sql
schema=/cortex-bootstrap/cortex-schema-full.sql
expected=$(sed -n 's/^-- cortex-schema-sha256: //p' "$baseline_sql")
[[ "$expected" =~ ^[0-9a-f]{64}$ ]] || {
    echo "cortex-baseline: missing or invalid schema checksum" >&2
    exit 1
}
actual=$(sha256sum "$schema")
[[ "${actual%% *}" = "$expected" ]] || {
    echo "cortex-baseline: bootstrap schema checksum mismatch" >&2
    exit 1
}
psql --no-psqlrc --set=ON_ERROR_STOP=1 \
    --username="${POSTGRES_USER:?}" --dbname="${POSTGRES_DB:?}" \
    --file="$baseline_sql"
echo "cortex-baseline: exact bootstrap inventory receipted"
