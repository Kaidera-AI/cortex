#!/usr/bin/env bash
# Fresh standalone installations work without a cloud provider key. This runs
# only during initdb; upgrades never overwrite an operator's platform settings.
set -euo pipefail
psql --no-psqlrc --set=ON_ERROR_STOP=1 \
    --username="${POSTGRES_USER:?}" --dbname="${POSTGRES_DB:?}" <<'SQL'
UPDATE cortex_platform_config SET
    embedding_provider = 'local',
    embedding_model = 'sentence-transformers/all-mpnet-base-v2',
    embedding_dims = 768,
    rerank_enabled = TRUE,
    rerank_provider = 'local',
    rerank_model = 'cross-encoder/ms-marco-MiniLM-L6-v2'
WHERE id IS TRUE;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON cortex_schema_migrations FROM cortex_app;
SQL
