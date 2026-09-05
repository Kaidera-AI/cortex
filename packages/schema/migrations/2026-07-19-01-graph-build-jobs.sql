CREATE TABLE IF NOT EXISTS graph_build_jobs (
    id UUID PRIMARY KEY,
    project TEXT NOT NULL,
    repo TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    full_rebuild BOOLEAN NOT NULL DEFAULT FALSE,
    embed BOOLEAN NOT NULL DEFAULT TRUE,
    request JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_graph_build_jobs_project_created
    ON graph_build_jobs (project, created_at DESC);
