-- 2026-06-01-roster-as-data.sql
-- Historical migration slot retained as an intentionally empty migration.
--
-- Earlier builds used this file to seed the internal dogfood roster. That was
-- wrong for a redistributable harness: a fresh EnGen OS install must start with
-- no project-specific workers and let the startup wizard / Add Project flow
-- register the first project and roster as deployment data.

BEGIN;
COMMIT;
