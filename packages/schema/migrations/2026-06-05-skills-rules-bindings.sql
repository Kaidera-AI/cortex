-- 2026-06-05-skills-rules-bindings.sql
-- Cortex-canonical harness — Phase 1 foundation.
--
-- Adds the agent-skill registry, skill bindings, and rules tables that the
-- cortex-sync-generate-harness generator + the cortex.persona.v2 boot contract read.
-- All tables are project-scoped. Purely ADDITIVE — no behaviour change until a
-- consumer reads them (the /boot persona queries are empty-safe / try-except).
--
-- Idempotent (CREATE TABLE/INDEX IF NOT EXISTS — safe to re-run).
-- Applied via the sanctioned runner:
--   cortex-apply-migrations --apply --target 2026-06-05-skills-rules-bindings.sql
-- (admin-gated /admin/migrations/apply; never psql — per cortex.md).
--
-- DOWN (manual rollback; additive so safe to simply leave in place):
--   DROP TABLE IF EXISTS agent_skill_bindings;
--   DROP TABLE IF EXISTS agent_skills;
--   DROP TABLE IF EXISTS rules;

CREATE TABLE IF NOT EXISTS agent_skills (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    skill_slug TEXT NOT NULL,
    name TEXT,
    description TEXT,
    skill_type TEXT NOT NULL DEFAULT 'capability',
    scope TEXT NOT NULL DEFAULT 'project'
        CHECK (scope IN ('global', 'project', 'agent')),
    permission TEXT,
    body_ref TEXT,
    body_hash TEXT,
    version TEXT NOT NULL DEFAULT '1',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'deprecated', 'draft')),
    trust_tier TEXT NOT NULL DEFAULT 'standard',
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (project, skill_slug, version)
);

CREATE INDEX IF NOT EXISTS idx_agent_skills_project
    ON agent_skills (project, lower(skill_slug));
CREATE INDEX IF NOT EXISTS idx_agent_skills_scope
    ON agent_skills (project, scope);
CREATE INDEX IF NOT EXISTS idx_agent_skills_status
    ON agent_skills (project, status);

CREATE TABLE IF NOT EXISTS agent_skill_bindings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    subject_kind TEXT NOT NULL DEFAULT 'role'
        CHECK (subject_kind IN ('role', 'agent')),
    subject TEXT NOT NULL,
    skill_slug TEXT NOT NULL,
    binding_type TEXT NOT NULL DEFAULT 'include',
    priority INTEGER NOT NULL DEFAULT 50,
    conditions JSONB DEFAULT '{}'::jsonb,
    version_pin TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (project, subject_kind, subject, skill_slug)
);

CREATE INDEX IF NOT EXISTS idx_agent_skill_bindings_project_subject
    ON agent_skill_bindings (project, subject_kind, lower(subject));
CREATE INDEX IF NOT EXISTS idx_agent_skill_bindings_skill
    ON agent_skill_bindings (project, lower(skill_slug));

CREATE TABLE IF NOT EXISTS rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    rule_slug TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    source_file TEXT,
    version TEXT NOT NULL DEFAULT '1',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'deprecated', 'draft')),
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (project, rule_slug, version)
);

CREATE INDEX IF NOT EXISTS idx_rules_project
    ON rules (project, lower(rule_slug));
CREATE INDEX IF NOT EXISTS idx_rules_status
    ON rules (project, status);
