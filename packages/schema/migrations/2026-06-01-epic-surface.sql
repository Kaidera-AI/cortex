-- 2026-06-01-epic-surface.sql
-- E006 (Cortex Canonicalization) addition — Epics as first-class Cortex data.
--
-- WHY: Today epics live only in Program/*/PROGRESS.md and the console renders an
-- "epics · TBD" placeholder (local-cortex/console/app/main.py :_epic_view, see its
-- TODO(epics): "When the real epic source lands (a /epics endpoint ...) replace
-- ..."). This migration adds the durable `epics` table so the API can expose a real
-- GET /epics surface and the console reads data, not parsed markdown.
--
-- ADDITIVE ONLY: creates ONE new table (`epics`) + its RLS policy + grants. It
-- does not seed project epics; project plans are deployment/project data.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS; the RLS policy is DROP-then-CREATE
-- under its canonical name (matches 2026-05-08-phase-c-rls.sql). Reversible:
-- see DOWN at the bottom.
--
-- Applied via the sanctioned runner (NEVER psql — per cortex.md):
--   cortex-apply-migrations --apply --target 2026-06-01-epic-surface.sql
-- (admin-gated /admin/migrations/apply; checksum-ledgered in cortex_schema_migrations).
--
-- RLS contract (identical to every other project-scoped table): a connection only
-- sees rows where project = current_setting('cortex.project') OR project = '_global'.
-- The `postgres` superuser bypasses RLS today; the policy is the final guard once
-- cortex-api connects as the non-superuser `cortex_app` role (Phase C cutover).

BEGIN;

-- ─── 1) Table ────────────────────────────────────────────────────────────────
-- project        : tenant scope (RLS key), matches every other scoped table.
-- epic_id         : human epic key within a project (E006/E007/…), unique per project.
-- title           : epic title.
-- status          : free-text lifecycle label (active / build / done / paused / …),
--                   kept as text (not an enum) so projects can use their own words —
--                   same choice the rest of the schema makes for status columns.
-- overall_pct     : authoritative overall completion 0–100 (the PROGRESS header %).
-- increments      : JSONB array of {num,title,status,pct} — the increment table.
-- updated_at      : last write.
CREATE TABLE IF NOT EXISTS epics (
    project      TEXT        NOT NULL,
    epic_id      TEXT        NOT NULL,
    title        TEXT        NOT NULL DEFAULT '',
    status       TEXT        NOT NULL DEFAULT 'active',
    overall_pct  INTEGER     NOT NULL DEFAULT 0
                 CHECK (overall_pct >= 0 AND overall_pct <= 100),
    increments   JSONB       NOT NULL DEFAULT '[]'::jsonb,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project, epic_id)
);

-- Owned by postgres (the migration/admin role), like cortex_schema_migrations.
ALTER TABLE epics OWNER TO postgres;

-- Helpful index for the project-scoped list ordering.
CREATE INDEX IF NOT EXISTS epics_project_idx ON epics (project, epic_id);

-- ─── 2) Grants ───────────────────────────────────────────────────────────────
-- ALTER DEFAULT PRIVILEGES (2026-05-08-phase-c-cortex-app-role.sql) only covers
-- tables created by the role that ran it, so grant explicitly here too — same
-- belt-and-braces pattern ensure_schema_migrations_table() uses for its own table.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cortex_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE epics TO cortex_app';
    END IF;
END $$;

-- ─── 3) Row-Level Security (canonical project-isolation policy) ───────────────
-- Byte-for-byte the policy shape used by 2026-05-08-phase-c-rls.sql for every
-- other scoped table (agents/handoffs/decisions/…): visible iff the row's project
-- equals the cortex.project GUC, or the row is _global.
ALTER TABLE epics ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS epics_project_isolation ON epics;
CREATE POLICY epics_project_isolation ON epics
    USING (
        project = current_setting('cortex.project', TRUE)
        OR project = '_global'
    )
    WITH CHECK (
        project = current_setting('cortex.project', TRUE)
        OR project = '_global'
    );

-- No seed rows. Epics are created through project/import APIs.

COMMIT;

-- ─── VERIFY (after --apply) ────────────────────────────────────────────────────
--   SELECT count(*) FROM epics; -- 0 on a fresh install before project import
--   -- RLS smoke (as cortex_app, non-superuser):
--   --   SET ROLE cortex_app; SELECT count(*) FROM epics; -- 0 without cortex.project
--   --   RESET ROLE;

-- ─── DOWN (reversible) ──────────────────────────────────────────────────────────
--   BEGIN;
--   DROP POLICY IF EXISTS epics_project_isolation ON epics;
--   DROP TABLE IF EXISTS epics;   -- drops the table, its index, RLS policy, and seed rows
--   COMMIT;
