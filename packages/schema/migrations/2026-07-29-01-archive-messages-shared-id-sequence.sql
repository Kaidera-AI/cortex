-- archive_messages.id must draw from the SAME sequence as messages.id.
--
-- The table has two writers with incompatible ID semantics and no agreement between
-- them:
--
--   * cortex-retain copies messages.id VERBATIM into archive_messages.id.
--   * ingest_session (.agents/api/main.py) omits id entirely and expects a default,
--     which is why its INSERT ... RETURNING id has never been able to succeed —
--     archive_messages.id is `bigint PRIMARY KEY` with no default at all.
--
-- The tempting repair is to give the column its own identity/sequence. That would
-- silently destroy messages. A fresh sequence starts at 1, while the live table
-- already spans ids 12,113 … 1,028,358 (measured 2026-07-29), so it would begin
-- issuing ids that are already taken by row 12,113 — and cortex-retain's copy of a
-- colliding id was, until this change, swallowed by ON CONFLICT DO NOTHING and then
-- deleted from `messages` anyway.
--
-- Sharing messages_id_seq removes the collision as a possibility rather than making
-- it survivable: every id in this table, whichever writer produced it, comes from one
-- monotonic source. Retain's verbatim copies keep their provenance (an archived row
-- keeps the id it had while hot, so it stays traceable), and ingest_session finally
-- gets a working default.
--
-- Idempotent and safe to re-run. Changes no existing row and no existing id.

DO $$
DECLARE
    cache_size bigint;
    prior_value bigint;
    hot_max bigint := 0;
    archive_max bigint;
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_class
        WHERE oid = to_regclass('public.messages_id_seq') AND relkind = 'S'
    ) AND EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'archive_messages' AND table_schema = 'public'
    ) THEN
        -- Keep explicit IDs and concurrent inserts out of both tables while
        -- computing the shared high-water mark. Use a fixed lock order.
        IF to_regclass('public.messages') IS NOT NULL THEN
            LOCK TABLE public.messages, public.archive_messages IN SHARE ROW EXCLUSIVE MODE;
        ELSE
            LOCK TABLE public.archive_messages IN SHARE ROW EXCLUSIVE MODE;
        END IF;
        ALTER TABLE public.archive_messages
            ALTER COLUMN id SET DEFAULT nextval('public.messages_id_seq');

        -- Reasserting the current cache size takes a transaction-held sequence
        -- DDL lock; nextval cannot race between the read and the restart.
        SELECT seqcache INTO STRICT cache_size FROM pg_sequence
            WHERE seqrelid = 'public.messages_id_seq'::regclass;
        EXECUTE format('ALTER SEQUENCE public.messages_id_seq CACHE %s', cache_size);
        SELECT last_value INTO STRICT prior_value FROM public.messages_id_seq;
        IF to_regclass('public.messages') IS NOT NULL THEN
            SELECT COALESCE(max(id), 0) INTO hot_max FROM public.messages;
        END IF;
        SELECT COALESCE(max(id), 0) INTO archive_max FROM public.archive_messages;
        EXECUTE format('ALTER SEQUENCE public.messages_id_seq RESTART WITH %s',
                       GREATEST(prior_value, hot_max, archive_max) + 1);
    END IF;
END
$$;
