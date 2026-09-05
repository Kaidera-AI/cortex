#!/usr/bin/env bash
# Cortex DB bootstrap — runs ONCE on a fresh cortex-pg (empty data volume), via the
# Postgres docker-entrypoint-initdb.d hook. On an EXISTING volume (this Mac) it never
# runs, so it can't disturb live agent memory.
#
# WHY THIS EXISTS: Kaidera OS can be installed from a fresh archive, and the
# pre-2026-05-08 migrations that built the Cortex base schema (the `project` columns,
# agent_profiles, agent_diaries, …) did NOT come across — so the repo's schema.sql +
# remaining migrations can't rebuild a working DB from scratch. cortex-api opens its app
# pool AS cortex_app at startup, so without provisioning the role + a COMPLETE schema the
# API crashes on boot ("role cortex_app does not exist" / missing relations) and the
# console reports "can't find Cortex".
#
# cortex-schema-full.sql is the exact, complete current schema captured (schema-only, zero
# rows) from the running cortex-pg: Identity v2 actor tables, work products, migrations,
# indexes, grants, and RLS. It is the authoritative source of truth for a fresh deploy.
# Regenerate it whenever the live schema changes:
#   podman exec cortex-pg pg_dump -U postgres -d platform_agent_memory \
#       --schema-only --no-owner > .agents/data/cortex-schema-full.sql
#
# ON_ERROR_STOP=1 + set -e: fail LOUD. A half-provisioned Cortex DB must not come up
# looking healthy — better the container refuses to start so the gap is obvious.
set -euo pipefail

psql_run() {
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" "$@"
}

# 1. Roles FIRST — the schema's GRANTs + RLS policies reference them, and cortex-api's
#    RLS-enforced app pool (CORTEX_PG_DSN_APP) connects as cortex_app. cortex_reader (read-
#    only) + cortex_app_test (test fixtures) are referenced by the dump's grants and must
#    exist for it to apply. Only cortex_app may log in, and it receives its generated
#    appliance credential in the immediately-following 10-cortex-role-credentials step.
echo "cortex-bootstrap: provisioning roles (cortex_app, cortex_reader, cortex_app_test)"
psql_run <<'SQL'
DO $$
DECLARE r TEXT;
BEGIN
    FOREACH r IN ARRAY ARRAY['cortex_app', 'cortex_reader', 'cortex_app_test'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            IF r = 'cortex_app' THEN
                EXECUTE format('CREATE ROLE %I LOGIN', r);
            ELSE
                EXECUTE format('CREATE ROLE %I NOLOGIN', r);
            END IF;
        END IF;
    END LOOP;
END
$$;
SQL

# 2. Apply the COMPLETE captured schema (tables + project columns + agent_profiles/
#    agent_diaries + indexes + RLS policies + grants to cortex_app). Self-contained — no
#    Identity v2 actor tables + constraints). Self-contained — no separate schema.sql /
#    migration sequence needed; this IS the fully-migrated DDL.
echo "cortex-bootstrap: applying complete Cortex schema (cortex-schema-full.sql)"
psql_run -f /cortex-bootstrap/cortex-schema-full.sql

echo "cortex-bootstrap: seeding singleton Cortex platform config"
psql_run <<'SQL'
INSERT INTO cortex_platform_config (id)
VALUES (TRUE)
ON CONFLICT (id) DO NOTHING;
SQL

# Graph extraction performs small idempotent DDL checks at request time through the
# scoped cortex_app pool. The schema dump is written with --no-owner, so explicitly
# hand graph-table ownership to cortex_app and FORCE RLS so the owner remains scoped
# by project isolation.
echo "cortex-bootstrap: assigning graph-table ownership to cortex_app with FORCE RLS"
psql_run <<'SQL'
ALTER TABLE public.cortex_entities OWNER TO cortex_app;
ALTER TABLE public.cortex_entities FORCE ROW LEVEL SECURITY;
ALTER TABLE public.cortex_relationships OWNER TO cortex_app;
ALTER TABLE public.cortex_relationships FORCE ROW LEVEL SECURITY;
SQL

# Catch-all grants — the captured dump's per-table grants are inconsistent (the live DB's
# cortex_app was granted ad-hoc over time), so cortex_app would be missing DML on most
# tables and cortex-api's app pool would hit "permission denied". This matches the intent
# of 2026-05-08-phase-c-cortex-app-role.sql (GRANT ON ALL TABLES). cortex_reader gets
# read-only. RLS still constrains rows per cortex.project — grants are orthogonal to RLS.
echo "cortex-bootstrap: granting cortex_app (DML) + cortex_reader (read) on all objects"
psql_run <<'SQL'
GRANT USAGE ON SCHEMA public TO cortex_app, cortex_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO cortex_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO cortex_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO cortex_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cortex_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO cortex_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO cortex_reader;
SQL

# Legacy vector-search recall default. Current fresh schemas use HNSW, but keeping
# this harmless setting preserves good recall if a deployment still has IVFFLAT
# indexes during an upgrade window.
echo "cortex-bootstrap: setting legacy ivfflat.probes default"
psql_run -c "ALTER DATABASE \"$POSTGRES_DB\" SET ivfflat.probes = 10;"

echo "cortex-bootstrap: done — fresh Cortex DB provisioned (roles + complete schema + grants)"
