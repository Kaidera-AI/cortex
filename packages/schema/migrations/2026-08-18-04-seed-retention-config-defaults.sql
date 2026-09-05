-- Seed retention_config on deployments that were upgraded rather than created fresh.
--
-- schema.sql creates retention_config AND seeds five default policy rows, but that seed
-- only ever runs on fresh schema creation. Any deployment whose table predates the seed
-- has it EMPTY forever, and nothing else writes those rows.
--
-- Observed on marlow 2026-08-18: retention_config existed with 0 rows. Two consequences,
-- both silent:
--   * cortex_doctor_growth_retention_check warns "messages retention policy row missing"
--     on every run -- which is accurate, just never acted on.
--   * cortex-retain reads tier2_days per table and falls back to 90 when the row is
--     absent, so the archive tiering it applies is a hardcoded default rather than the
--     configured policy. Combined with nothing scheduling cortex-retain at all, the whole
--     retention capability was inert.
--
-- Values are copied verbatim from the schema.sql seed so the two cannot disagree.
-- ON CONFLICT DO NOTHING keeps a deliberate operator override intact -- this backfills
-- absent rows, it never rewrites a tuned one.

INSERT INTO retention_config (table_name, tier2_days, description) VALUES
    ('messages', 90, 'Chat history — 90 days in pgvector, then archive'),
    ('team_events', 90, 'Team events — 90 days in pgvector, then archive'),
    ('decisions', 365, 'Decisions — 1 year in pgvector, then archive'),
    ('lessons', 365, 'Lessons — 1 year in pgvector, then archive'),
    ('handoffs', 30, 'Handoffs — 30 days in pgvector, then archive')
ON CONFLICT (table_name) DO NOTHING;
