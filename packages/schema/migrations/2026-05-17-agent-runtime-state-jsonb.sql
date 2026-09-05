-- E75 Inc 23 - Postgres-backed agent runtime state.
--
-- Moves registration-time roster availability out of Redis hset state and out
-- of agents.capabilities. Runtime state is local-lane operational metadata and
-- remains project-scoped through the existing agents RLS policy.

BEGIN;

ALTER TABLE agents
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'available';

ALTER TABLE agents
    ADD COLUMN IF NOT EXISTS runtime_state JSONB NOT NULL DEFAULT '{}'::jsonb;

UPDATE agents
   SET status = 'available'
 WHERE status IS NULL
    OR btrim(status) = '';

UPDATE agents
   SET runtime_state = '{}'::jsonb
 WHERE runtime_state IS NULL;

CREATE INDEX IF NOT EXISTS idx_agents_project_status
    ON agents (project, status);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cortex_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE agents TO cortex_app';
    END IF;
END $$;

COMMIT;
