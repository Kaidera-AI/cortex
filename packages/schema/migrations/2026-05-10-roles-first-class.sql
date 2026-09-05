-- Roles as first-class records (handoff d0a27288 / E75 Inc 09).
--
-- Roles are intentionally loose data, not an agents.role foreign key.
-- Customer/local projects can register arbitrary slugs such as designer,
-- sales-marketing, or security-analyst without code changes.

BEGIN;

CREATE TABLE IF NOT EXISTS roles (
    project TEXT NOT NULL,
    name TEXT NOT NULL,
    default_capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
    description TEXT,
    is_builtin BOOLEAN NOT NULL DEFAULT FALSE,
    source_file TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project, name)
);

ALTER TABLE roles OWNER TO postgres;

ALTER TABLE roles ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS roles_project_isolation ON roles;

CREATE POLICY roles_project_isolation ON roles
  USING (
      project = current_setting('cortex.project', TRUE)
      OR project = '_global'
  )
  WITH CHECK (
      project = current_setting('cortex.project', TRUE)
      OR project = '_global'
  );

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cortex_app') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA public TO cortex_app';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE roles TO cortex_app';
    END IF;
END $$;

COMMIT;
