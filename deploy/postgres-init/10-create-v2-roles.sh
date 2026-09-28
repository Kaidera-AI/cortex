#!/bin/sh
set -eu

app_password_file=/run/secrets/cortex-v2-v0-02-001-sandbox-db-app-password
migrator_password_file=/run/secrets/cortex-v2-v0-02-001-sandbox-db-migrator-password
[ -r "$app_password_file" ] || { echo "v2-init: app password unavailable" >&2; exit 1; }
[ -r "$migrator_password_file" ] || { echo "v2-init: migrator password unavailable" >&2; exit 1; }

CORTEX_V2_APP_PASSWORD=$(cat "$app_password_file")
CORTEX_V2_MIGRATOR_PASSWORD=$(cat "$migrator_password_file")
export CORTEX_V2_APP_PASSWORD CORTEX_V2_MIGRATOR_PASSWORD
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v ON_ERROR_STOP=1 <<'SQL'
\getenv app_password CORTEX_V2_APP_PASSWORD
\getenv migrator_password CORTEX_V2_MIGRATOR_PASSWORD
CREATE ROLE cortex_v2_app
    LOGIN PASSWORD :'app_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE cortex_v2_migrator
    LOGIN PASSWORD :'migrator_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT, CREATE ON DATABASE cortex_v2 TO cortex_v2_migrator;
SQL
unset CORTEX_V2_APP_PASSWORD CORTEX_V2_MIGRATOR_PASSWORD app_password migrator_password
