-- TAM-DEV Agent Memory Schema
-- LEGACY MINIMAL TEST SCHEMA.
--
-- Do not use this file to bootstrap Cortex or the redistributable stack.
-- Fresh Cortex installs load .agents/data/cortex-schema-full.sql through
-- .agents/data/initdb/00-cortex-bootstrap.sh. That full schema is the
-- authoritative redist/fresh-install contract, including Identity v2
-- project_id/actor identity constraints.
--
-- PostgreSQL + pgvector
-- Embedding: core text memory uses 768-dim vectors.
-- Artifact embeddings remain 2048-dim until the artifact vector migration is run.
-- Index: ivfflat
--
-- Usage: podman cp schema.sql mem-postgres:/tmp/ && podman exec mem-postgres psql -U postgres -d tam_agent_memory -f /tmp/schema.sql

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Agents
CREATE TABLE IF NOT EXISTS agents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    role TEXT,
    model TEXT,
    capabilities JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Sprints
CREATE TABLE IF NOT EXISTS sprints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sprint_number INTEGER UNIQUE NOT NULL,
    goal TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    started_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ,
    retrospective JSONB
);

-- Decisions (shared consciousness — all agents see these)
CREATE TABLE IF NOT EXISTS decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sprint_id UUID REFERENCES sprints(id),
    agent_id UUID REFERENCES agents(id),
    summary TEXT NOT NULL,
    rationale TEXT,
    outcome TEXT,
    category TEXT,
    files_affected TEXT[],
    tags TEXT[],
    embedding VECTOR(768),
    created_at TIMESTAMPTZ DEFAULT now(),
    superseded_by UUID REFERENCES decisions(id)
);

-- Lessons learned (shared consciousness — propagated to all agents)
CREATE TABLE IF NOT EXISTS lessons (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id UUID REFERENCES decisions(id),
    agent_id UUID REFERENCES agents(id),
    category TEXT,
    summary TEXT NOT NULL,
    detail TEXT,
    code_right TEXT,
    code_wrong TEXT,
    times_referenced INTEGER DEFAULT 0,
    embedding VECTOR(768),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Team events (TELEPATHIC LINK — append-only event log)
CREATE TABLE IF NOT EXISTS team_events (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ DEFAULT now(),
    agent_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail JSONB,
    files TEXT[],
    sprint_id UUID REFERENCES sprints(id),
    related_decision_id UUID REFERENCES decisions(id)
);

-- Agent sessions (per-agent work log)
CREATE TABLE IF NOT EXISTS agent_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id UUID REFERENCES agents(id),
    sprint_id UUID REFERENCES sprints(id),
    task TEXT,
    started_at TIMESTAMPTZ DEFAULT now(),
    ended_at TIMESTAMPTZ,
    files_modified TEXT[],
    outcome TEXT,
    handed_off_to UUID REFERENCES agents(id),
    notes JSONB
);

-- Knowledge chunks (replaces Milvus — embedded docs, patterns, skills)
CREATE TABLE IF NOT EXISTS knowledge (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content TEXT NOT NULL,
    source_file TEXT,
    category TEXT,
    section TEXT,
    embedding VECTOR(768),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Indexes. Vector columns use HNSW, NOT ivfflat.
--
-- The previous comment here said "ivfflat for vectors - pgvector hnsw has 2000-dim
-- limit". Every embedding column in this schema is vector(768), so that limit never
-- applied and the justification was stale. What it produced was five
-- `WITH (lists = 1)` ivfflat indexes - one cluster is degenerate, so the planner
-- correctly ignores them. Measured on a live deployment: all five had ZERO scans and
-- 1,277 MB between them, the largest 1,172 MB on `decisions`.
--
-- Migrations 2026-06-26-01..05 added the HNSW replacements and 2026-06-26-06..10
-- dropped these. The drops did not hold: `cortex-sync-workspace` re-applies this file
-- unconditionally, so every workspace sync recreated what the migrations removed.
-- Creating the working index HERE is what makes the removal stick - a migration
-- cannot win against a base schema that is re-applied after it.
CREATE INDEX IF NOT EXISTS idx_decisions_embedding_hnsw ON decisions USING hnsw(embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_lessons_embedding_hnsw ON lessons USING hnsw(embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_knowledge_embedding_hnsw ON knowledge USING hnsw(embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_decisions_tags ON decisions USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_decisions_files ON decisions USING GIN(files_affected);
CREATE INDEX IF NOT EXISTS idx_events_ts ON team_events(ts);
CREATE INDEX IF NOT EXISTS idx_events_agent ON team_events(agent_name);
CREATE INDEX IF NOT EXISTS idx_events_type ON team_events(event_type);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON agent_sessions(agent_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_source ON knowledge(source_file);
CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge(category);

-- Work Product Memory (canonical completed-work receipts)
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
CREATE INDEX IF NOT EXISTS idx_work_products_project_status ON work_products(project, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_work_products_files ON work_products USING GIN(files_changed);
CREATE INDEX IF NOT EXISTS idx_work_products_symbols ON work_products USING GIN(symbols_changed);
CREATE INDEX IF NOT EXISTS idx_work_products_subjects ON work_products USING GIN(subject_entities);
CREATE INDEX IF NOT EXISTS idx_work_products_file_hashes ON work_products USING GIN(file_hashes);
CREATE INDEX IF NOT EXISTS idx_work_products_freshness ON work_products(project, freshness_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_work_products_projection ON work_products(project, projection_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_work_products_embedding_hnsw ON work_products USING hnsw(embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- Embedding backfill jobs (pollable progress for API-owned L2 backfills)
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
    ON embedding_backfill_jobs(project, created_at DESC);

-- Cortex platform config (central ingestion/search model settings)
CREATE TABLE IF NOT EXISTS cortex_platform_config (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    embedding_provider TEXT NOT NULL DEFAULT 'openrouter',
    embedding_model TEXT NOT NULL DEFAULT 'nvidia/llama-nemotron-embed-vl-1b-v2:free',
    embedding_dims INTEGER NOT NULL DEFAULT 768 CHECK (embedding_dims > 0),
    rerank_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    rerank_provider TEXT NOT NULL DEFAULT 'nvidia',
    rerank_model TEXT NOT NULL DEFAULT 'nv-rerank-qa-mistral-4b:1',
    analysis_provider TEXT NOT NULL DEFAULT 'openrouter',
    analysis_model TEXT NOT NULL DEFAULT 'google/gemma-4-31b-it:free',
    cortex_api_url TEXT NOT NULL DEFAULT 'http://localhost:8501',
    boot_context_version TEXT NOT NULL DEFAULT 'v2',
    max_boot_tokens INTEGER NOT NULL DEFAULT 250 CHECK (max_boot_tokens > 0),
    search_confidence_threshold DOUBLE PRECISION NOT NULL DEFAULT 0.015,
    rrf_k INTEGER NOT NULL DEFAULT 60 CHECK (rrf_k > 0),
    embed_input_max_chars INTEGER NOT NULL DEFAULT 500 CHECK (embed_input_max_chars > 0),
    rerank_input_max_chars INTEGER NOT NULL DEFAULT 500 CHECK (rerank_input_max_chars > 0),
    embed_timeout_ms INTEGER NOT NULL DEFAULT 15000 CHECK (embed_timeout_ms > 0),
    rerank_timeout_ms INTEGER NOT NULL DEFAULT 15000 CHECK (rerank_timeout_ms > 0),
    analysis_timeout_ms INTEGER NOT NULL DEFAULT 90000 CHECK (analysis_timeout_ms > 0),
    embedding_provider_config_id UUID,
    rerank_provider_config_id UUID,
    analysis_provider_config_id UUID,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO cortex_platform_config (id)
VALUES (TRUE)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- Agent Cortex v2 — DB-Primary Collaboration Tables
-- ============================================================

-- Messages (full chat history — every agent/human exchange)
CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID REFERENCES agent_sessions(id),
    project TEXT NOT NULL DEFAULT 'tam',
    agent_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('human', 'agent', 'system')),
    content TEXT NOT NULL,
    metadata JSONB,
    embedding VECTOR(768),
    ts TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_agent ON messages(agent_name);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project);
CREATE INDEX IF NOT EXISTS idx_messages_embedding_hnsw ON messages USING hnsw(embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- Handoffs (replaces handoffs/*.md — DB-native work transfers)
CREATE TABLE IF NOT EXISTS handoffs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind TEXT NOT NULL DEFAULT 'task'
        CHECK (kind IN ('task', 'completion_handback')),
    reply_to_handoff_id UUID REFERENCES handoffs(id),
    project TEXT NOT NULL DEFAULT 'tam',
    from_agent TEXT NOT NULL,
    from_role TEXT,
    to_role TEXT NOT NULL,
    to_agent TEXT,
    priority TEXT DEFAULT 'medium' CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
    sprint_id UUID REFERENCES sprints(id),
    branch TEXT,
    summary TEXT NOT NULL,
    files_changed TEXT[],
    verification TEXT,
    next_steps TEXT,
    context TEXT,
    acceptance JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    completion_report JSONB NOT NULL DEFAULT '{}'::jsonb,
    retry JSONB NOT NULL DEFAULT '{}'::jsonb,
    retry_count INTEGER NOT NULL DEFAULT 0,
    escalation JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'returned', 'completed', 'released', 'abandoned', 'failed', 'archived')),
    parent_goal_id TEXT,
    claimed_by TEXT,
    claimed_at TIMESTAMPTZ,
    returned_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    invalidated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

COMMENT ON COLUMN handoffs.kind IS
    'Handoff record type: delegated task or completion handback.';
COMMENT ON COLUMN handoffs.reply_to_handoff_id IS
    'Original task addressed by a completion handback.';
COMMENT ON COLUMN handoffs.returned_at IS
    'Timestamp the worker returned the task for delegator review.';
COMMENT ON COLUMN handoffs.completion_report IS
    'Structured worker outcome, evidence, risks, and follow-up actions.';

CREATE INDEX IF NOT EXISTS idx_handoffs_status ON handoffs(status);
CREATE INDEX IF NOT EXISTS idx_handoffs_to_role ON handoffs(to_role);
CREATE INDEX IF NOT EXISTS idx_handoffs_to_agent ON handoffs(to_agent);
CREATE INDEX IF NOT EXISTS idx_handoffs_project ON handoffs(project);
CREATE INDEX IF NOT EXISTS idx_handoffs_parent_goal_id ON handoffs(parent_goal_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_reply_to_handoff_id ON handoffs(reply_to_handoff_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_handoffs_completion_handback_active
    ON handoffs(project, reply_to_handoff_id)
    WHERE kind = 'completion_handback'
      AND invalidated_at IS NULL
      AND status IN ('pending', 'claimed');

-- Tasks (replaces board/tasks.yaml — DB-native task board)
CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL DEFAULT 'tam',
    sprint_id UUID REFERENCES sprints(id),
    title TEXT NOT NULL,
    description TEXT,
    assigned_role TEXT,
    assigned_agent TEXT,
    status TEXT DEFAULT 'todo' CHECK (status IN ('todo', 'in_progress', 'review', 'done', 'blocked')),
    priority INTEGER DEFAULT 50,
    tags TEXT[],
    blocked_by UUID REFERENCES tasks(id),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON tasks(assigned_agent);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);
CREATE INDEX IF NOT EXISTS idx_tasks_sprint ON tasks(sprint_id);

-- Add project column to existing tables for multi-project isolation
DO $$ BEGIN
    ALTER TABLE decisions ADD COLUMN IF NOT EXISTS agent_name TEXT;
    ALTER TABLE decisions ADD COLUMN IF NOT EXISTS project TEXT DEFAULT 'tam';
    ALTER TABLE decisions ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ;
    ALTER TABLE decisions ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb;
    ALTER TABLE decisions ADD COLUMN IF NOT EXISTS parent_goal_id TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS parent_goal_id TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_handoffs_parent_goal_id ON handoffs(parent_goal_id);
CREATE INDEX IF NOT EXISTS idx_decisions_parent_goal_id ON decisions(parent_goal_id);

DO $$ BEGIN
    ALTER TABLE lessons ADD COLUMN IF NOT EXISTS agent_name TEXT;
    ALTER TABLE lessons ADD COLUMN IF NOT EXISTS project TEXT DEFAULT 'tam';
    ALTER TABLE lessons ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ;
    ALTER TABLE lessons ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE team_events ADD COLUMN IF NOT EXISTS project TEXT DEFAULT 'tam';
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS project TEXT DEFAULT 'tam';
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS project TEXT DEFAULT 'tam';
    ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();
    ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- ============================================================
-- Agent Cortex — Archive Tables (Tier 3: Cold Storage)
-- No embeddings — full text only, forever retention
-- ============================================================

CREATE TABLE IF NOT EXISTS archive_messages (
    id BIGINT PRIMARY KEY,
    session_id UUID,
    project TEXT,
    agent_name TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata JSONB,
    ts TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_archive_messages_ts ON archive_messages(ts);
CREATE INDEX IF NOT EXISTS idx_archive_messages_agent ON archive_messages(agent_name);
CREATE INDEX IF NOT EXISTS idx_archive_messages_project ON archive_messages(project);

CREATE TABLE IF NOT EXISTS archive_events (
    id BIGINT PRIMARY KEY,
    ts TIMESTAMPTZ,
    agent_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail JSONB,
    files TEXT[],
    project TEXT,
    sprint_id UUID
);
CREATE INDEX IF NOT EXISTS idx_archive_events_ts ON archive_events(ts);
CREATE INDEX IF NOT EXISTS idx_archive_events_agent ON archive_events(agent_name);

CREATE TABLE IF NOT EXISTS archive_decisions (
    id UUID PRIMARY KEY,
    sprint_id UUID,
    agent_name TEXT,
    summary TEXT NOT NULL,
    rationale TEXT,
    outcome TEXT,
    category TEXT,
    files_affected TEXT[],
    tags TEXT[],
    project TEXT,
    created_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS archive_lessons (
    id UUID PRIMARY KEY,
    agent_name TEXT,
    category TEXT,
    summary TEXT NOT NULL,
    detail TEXT,
    code_right TEXT,
    code_wrong TEXT,
    project TEXT,
    created_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS archive_handoffs (
    id UUID PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'task'
        CHECK (kind IN ('task', 'completion_handback')),
    reply_to_handoff_id UUID,
    project TEXT,
    from_agent TEXT NOT NULL,
    to_role TEXT NOT NULL,
    priority TEXT,
    summary TEXT NOT NULL,
    files_changed TEXT[],
    acceptance JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    completion_report JSONB NOT NULL DEFAULT '{}'::jsonb,
    retry JSONB NOT NULL DEFAULT '{}'::jsonb,
    escalation JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT,
    created_at TIMESTAMPTZ,
    returned_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

-- Retention config table
CREATE TABLE IF NOT EXISTS retention_config (
    table_name TEXT PRIMARY KEY,
    tier2_days INTEGER NOT NULL DEFAULT 90,
    description TEXT
);

-- Seed default retention periods
INSERT INTO retention_config (table_name, tier2_days, description) VALUES
    ('messages', 90, 'Chat history — 90 days in pgvector, then archive'),
    ('team_events', 90, 'Team events — 90 days in pgvector, then archive'),
    ('decisions', 365, 'Decisions — 1 year in pgvector, then archive'),
    ('lessons', 365, 'Lessons — 1 year in pgvector, then archive'),
    ('handoffs', 30, 'Handoffs — 30 days in pgvector, then archive')
ON CONFLICT (table_name) DO NOTHING;

-- ============================================================
-- Artifacts — non-chat content ingested via cortex-ingest-artifact
-- Added 2026-04-24: the ingest tool was present but tables were
-- never migrated locally. Parent/child via parent_artifact_id;
-- cross-reference via artifact_edges.
-- ============================================================

CREATE TABLE IF NOT EXISTS artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    customer_id UUID,
    org_id UUID,
    parent_artifact_id UUID REFERENCES artifacts(id) ON DELETE SET NULL,
    modality TEXT,
    source_type TEXT,
    source_file TEXT NOT NULL,
    extraction_method TEXT,
    content_hash TEXT NOT NULL,
    raw_content TEXT,
    section_context TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    embedding VECTOR(768),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (project, source_file, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts (project);
CREATE INDEX IF NOT EXISTS idx_artifacts_source_file ON artifacts (source_file);
CREATE INDEX IF NOT EXISTS idx_artifacts_modality ON artifacts (modality);
CREATE INDEX IF NOT EXISTS idx_artifacts_parent ON artifacts (parent_artifact_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_embedding_hnsw
    ON artifacts USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

DO $$ BEGIN
    ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS caption TEXT;
    ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS neighborhood_text TEXT;
    ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS source_doc_metadata JSONB DEFAULT '{}'::jsonb;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_artifacts_caption_trgm
    ON artifacts USING GIN (LOWER(COALESCE(caption, '')) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_artifacts_neighborhood_trgm
    ON artifacts USING GIN (LOWER(COALESCE(neighborhood_text, '')) gin_trgm_ops);

CREATE TABLE IF NOT EXISTS artifact_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    source_id UUID NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (project, source_id, target_type, target_ref, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_artifact_edges_source ON artifact_edges (source_id);
CREATE INDEX IF NOT EXISTS idx_artifact_edges_target ON artifact_edges (target_type, target_ref);
CREATE INDEX IF NOT EXISTS idx_artifact_edges_project ON artifact_edges (project);

-- ============================================================
-- Cortex Knowledge Graph (Layer 4)
-- Local dogfood schema: project-scoped entities and relationships
-- extracted from decisions, lessons, knowledge, and artifacts.
-- ============================================================

CREATE TABLE IF NOT EXISTS cortex_entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    properties JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'cortex_entities_natural_key'
    ) THEN
        ALTER TABLE cortex_entities
            ADD CONSTRAINT cortex_entities_natural_key UNIQUE (project, name, entity_type);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_cortex_entities_project ON cortex_entities (project);
CREATE INDEX IF NOT EXISTS idx_cortex_entities_type ON cortex_entities (entity_type);
CREATE INDEX IF NOT EXISTS idx_cortex_entities_name_trgm
    ON cortex_entities USING GIN (LOWER(name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_cortex_entities_description_trgm
    ON cortex_entities USING GIN (LOWER(COALESCE(properties->>'description', '')) gin_trgm_ops);

CREATE TABLE IF NOT EXISTS cortex_relationships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    source_entity_id UUID NOT NULL REFERENCES cortex_entities(id) ON DELETE CASCADE,
    target_entity_id UUID NOT NULL REFERENCES cortex_entities(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,
    properties JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'cortex_relationships_natural_key'
    ) THEN
        ALTER TABLE cortex_relationships
            ADD CONSTRAINT cortex_relationships_natural_key
            UNIQUE (project, source_entity_id, target_entity_id, relationship_type);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_cortex_relationships_project ON cortex_relationships (project);
CREATE INDEX IF NOT EXISTS idx_cortex_relationships_source ON cortex_relationships (source_entity_id);
CREATE INDEX IF NOT EXISTS idx_cortex_relationships_target ON cortex_relationships (target_entity_id);
CREATE INDEX IF NOT EXISTS idx_cortex_relationships_type ON cortex_relationships (relationship_type);
