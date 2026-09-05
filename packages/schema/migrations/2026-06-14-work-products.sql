CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS work_products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    handoff_id UUID NULL,
    agent_name TEXT,
    activity_type TEXT NOT NULL DEFAULT 'task-completed',
    status TEXT NOT NULL DEFAULT 'current',
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    behavior_summary TEXT,
    architecture_notes TEXT,
    files_changed TEXT[] DEFAULT '{}'::text[],
    symbols_changed TEXT[] DEFAULT '{}'::text[],
    subject_entities TEXT[] DEFAULT '{}'::text[],
    artifact_refs TEXT[] DEFAULT '{}'::text[],
    tests_run JSONB DEFAULT '[]'::jsonb,
    risks TEXT[] DEFAULT '{}'::text[],
    followups TEXT[] DEFAULT '{}'::text[],
    approval_status TEXT,
    content_hash TEXT,
    commit_sha TEXT,
    file_hashes JSONB DEFAULT '{}'::jsonb,
    symbol_hashes JSONB DEFAULT '{}'::jsonb,
    freshness_status TEXT NOT NULL DEFAULT 'unknown',
    freshness_reason TEXT,
    freshness_checked_at TIMESTAMPTZ NULL,
    projection_status TEXT NOT NULL DEFAULT 'pending',
    projection_error TEXT,
    projected_at TIMESTAMPTZ NULL,
    source_event_id BIGINT NULL,
    supersedes_id UUID NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    embedding VECTOR(768),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    valid_from TIMESTAMPTZ DEFAULT NOW(),
    valid_to TIMESTAMPTZ NULL,
    invalidated_at TIMESTAMPTZ NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_work_products_project_handoff_current
    ON work_products (project, handoff_id)
    WHERE handoff_id IS NOT NULL AND invalidated_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_work_products_project_status
    ON work_products (project, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_work_products_handoff
    ON work_products (handoff_id);
CREATE INDEX IF NOT EXISTS idx_work_products_files
    ON work_products USING GIN (files_changed);
CREATE INDEX IF NOT EXISTS idx_work_products_symbols
    ON work_products USING GIN (symbols_changed);
CREATE INDEX IF NOT EXISTS idx_work_products_subjects
    ON work_products USING GIN (subject_entities);
CREATE INDEX IF NOT EXISTS idx_work_products_metadata
    ON work_products USING GIN (metadata);
CREATE INDEX IF NOT EXISTS idx_work_products_file_hashes
    ON work_products USING GIN (file_hashes);
CREATE INDEX IF NOT EXISTS idx_work_products_freshness
    ON work_products (project, freshness_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_work_products_projection
    ON work_products (project, projection_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_work_products_embedding
    ON work_products USING ivfflat (embedding vector_cosine_ops) WITH (lists = 1);
CREATE INDEX IF NOT EXISTS idx_work_products_fts
    ON work_products USING GIN (
        to_tsvector(
            'english',
            COALESCE(title, '') || ' ' ||
            COALESCE(summary, '') || ' ' ||
            COALESCE(behavior_summary, '') || ' ' ||
            COALESCE(architecture_notes, '')
        )
    );
CREATE INDEX IF NOT EXISTS idx_work_products_title_trgm
    ON work_products USING GIN (LOWER(COALESCE(title, '')) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_work_products_summary_trgm
    ON work_products USING GIN (LOWER(COALESCE(summary, '')) gin_trgm_ops);
