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
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_class WHERE relname = 'messages_id_seq' AND relkind = 'S'
    ) AND EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'archive_messages' AND table_schema = 'public'
    ) THEN
        ALTER TABLE public.archive_messages
            ALTER COLUMN id SET DEFAULT nextval('public.messages_id_seq');

        -- The sequence must never hand out an id this table already holds. It is
        -- shared with `messages`, so it is normally far ahead already; this is the
        -- belt-and-braces case where archive somehow leads.
        PERFORM setval(
            'public.messages_id_seq',
            GREATEST(
                (SELECT last_value FROM public.messages_id_seq),
                COALESCE((SELECT max(id) FROM public.archive_messages), 0)
            ),
            true
        );
    END IF;
END
$$;
