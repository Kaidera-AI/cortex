-- Normalise JSONB `metadata` scalars that block the embedding backfill entirely.
--
-- `COALESCE(metadata, '{}'::jsonb)` only rescues a SQL NULL. A JSONB *scalar* passes
-- straight through it into the `-` and `||` operators, which raise:
--     cannot delete from scalar
--     invalid concatenation of jsonb objects
-- The backfill has no per-row rescue around those UPDATEs, so the first such row aborts
-- the request with a 500 and every remaining row in the backlog is left unembedded.
--
-- Observed on marlow 2026-08-18: 33 rows (14 decisions, 11 lessons, 8 knowledge) held the
-- double-encoded string "{}" — a value produced by serialising an already-serialised
-- empty object. They carried no recoverable data, yet they had blocked a 225,108-row
-- backlog indefinitely, leaving decisions at 11.4% embedded and messages at 0%.
--
-- Any non-object metadata is therefore replaced with an empty object. Rows whose metadata
-- is SQL NULL are left alone: COALESCE already handles those correctly, and rewriting them
-- would churn tables for no behavioural gain.
--
-- The API-side guard (METADATA_AS_OBJECT_SQL in .agents/api/main.py) makes the backfill
-- self-healing if a scalar is ever written again; this migration clears the rows that
-- already exist so the backlog can drain without waiting for that path to touch them.

DO $$
DECLARE
    target text;
    affected bigint;
BEGIN
    FOREACH target IN ARRAY ARRAY['decisions', 'lessons', 'knowledge', 'messages', 'work_products']
    LOOP
        -- Older deployments do not carry every table; skip rather than fail the chain.
        IF to_regclass('public.' || target) IS NULL THEN
            RAISE NOTICE 'skip %: table not present', target;
            CONTINUE;
        END IF;

        EXECUTE format(
            'UPDATE %I SET metadata = ''{}''::jsonb
              WHERE metadata IS NOT NULL AND jsonb_typeof(metadata) <> ''object''',
            target
        );
        GET DIAGNOSTICS affected = ROW_COUNT;
        IF affected > 0 THEN
            RAISE NOTICE 'normalised % scalar metadata row(s) on %', affected, target;
        END IF;
    END LOOP;
END
$$;
