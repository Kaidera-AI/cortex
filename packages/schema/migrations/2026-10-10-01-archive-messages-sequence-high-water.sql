-- Forward repair for the shared messages/archive_messages ID sequence.
-- The shipped 2026-07-29 migration keeps its original ID and checksum.
-- Run this successor after it on fresh installs and once on upgraded ledgers.
-- Lock both present tables, then the sequence, before reading the high-water mark.

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
