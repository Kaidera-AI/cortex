-- Embedding backfill jobs: pollable progress for large API-owned L2 backfills.

CREATE TABLE IF NOT EXISTS embedding_backfill_jobs (
    id UUID PRIMARY KEY,
    project TEXT NOT NULL,
    table_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    limit_requested INTEGER NOT NULL DEFAULT 100,
    chunk_size INTEGER NOT NULL DEFAULT 100,
    dry_run BOOLEAN NOT NULL DEFAULT FALSE,
    max_errors INTEGER NOT NULL DEFAULT 10,
    error_threshold INTEGER NOT NULL DEFAULT 3,
    provider_configured BOOLEAN NOT NULL DEFAULT FALSE,
    processed INTEGER NOT NULL DEFAULT 0,
    embedded INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    stopped TEXT NOT NULL DEFAULT '',
    tables JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_embedding_backfill_jobs_project_created
    ON embedding_backfill_jobs (project, created_at DESC);
