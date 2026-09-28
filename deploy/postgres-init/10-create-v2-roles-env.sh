#!/bin/sh
# Instance-agnostic v2 role bootstrap: secret paths come from the compose
# service environment so one script serves every uniquely named candidate.
set -eu

: "${CORTEX_V2_APP_PASSWORD_FILE:?v2-init: app password file env is required}"
: "${CORTEX_V2_MIGRATOR_PASSWORD_FILE:?v2-init: migrator password file env is required}"
[ -r "$CORTEX_V2_APP_PASSWORD_FILE" ] || { echo "v2-init: app password unavailable" >&2; exit 1; }
[ -r "$CORTEX_V2_MIGRATOR_PASSWORD_FILE" ] || { echo "v2-init: migrator password unavailable" >&2; exit 1; }

IFS= read -r CORTEX_V2_APP_PASSWORD < "$CORTEX_V2_APP_PASSWORD_FILE" || [ -n "$CORTEX_V2_APP_PASSWORD" ]
IFS= read -r CORTEX_V2_MIGRATOR_PASSWORD < "$CORTEX_V2_MIGRATOR_PASSWORD_FILE" || [ -n "$CORTEX_V2_MIGRATOR_PASSWORD" ]
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
CREATE EXTENSION IF NOT EXISTS vector SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public;
SQL
unset CORTEX_V2_APP_PASSWORD CORTEX_V2_MIGRATOR_PASSWORD app_password migrator_password
