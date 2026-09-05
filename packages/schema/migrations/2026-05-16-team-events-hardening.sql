-- E75 Inc 21 - team_events durable event-stream hardening.
--
-- Runtime API handlers must not run this DDL. Apply through the admin
-- migration path before switching consumers from Redis to Postgres.
--
-- The migration is intentionally conservative about historical rows:
--   - null projects are backfilled only when row metadata carries an explicit
--     project value;
--   - any remaining null project rows abort the migration for manual audit;
--   - legacy/default 'tam' rows are counted but not rewritten blindly.

BEGIN;

UPDATE team_events
   SET project = COALESCE(
       NULLIF(detail->>'project', ''),
       NULLIF(detail->>'cortex_project', '')
   )
 WHERE project IS NULL
   AND detail IS NOT NULL
   AND (
       NULLIF(detail->>'project', '') IS NOT NULL
       OR NULLIF(detail->>'cortex_project', '') IS NOT NULL
   );

DO $$
DECLARE
    null_project_count BIGINT;
    legacy_tam_count BIGINT;
BEGIN
    SELECT COUNT(*) INTO null_project_count
      FROM team_events
     WHERE project IS NULL;

    IF null_project_count > 0 THEN
        RAISE EXCEPTION
            'team_events.project hardening blocked: % rows still have NULL project and require manual audit',
            null_project_count;
    END IF;

    SELECT COUNT(*) INTO legacy_tam_count
      FROM team_events
     WHERE project = 'tam';

    IF legacy_tam_count > 0 THEN
        RAISE NOTICE
            'team_events.project hardening found % legacy tam rows; leaving unchanged for explicit shape audit',
            legacy_tam_count;
    END IF;
END $$;

ALTER TABLE team_events
    ALTER COLUMN project DROP DEFAULT,
    ALTER COLUMN project SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_team_events_project_id
    ON team_events (project, id);

CREATE INDEX IF NOT EXISTS idx_team_events_project_ts_desc
    ON team_events (project, ts DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cortex_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE team_events TO cortex_app';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE team_events_id_seq TO cortex_app';
    END IF;
END $$;

COMMIT;
