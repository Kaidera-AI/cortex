-- Additive upgrade convergence for the E2 distiller and E4 compactor.
-- The original 2026-06-24 migration is immutable and is not guaranteed to be
-- present in every supported upgrade ledger.
ALTER TABLE public.messages
    ADD COLUMN IF NOT EXISTS distilled boolean NOT NULL DEFAULT false;

ALTER TABLE public.archive_messages
    ADD COLUMN IF NOT EXISTS content_zstd bytea,
    ADD COLUMN IF NOT EXISTS retained_until timestamptz,
    ADD COLUMN IF NOT EXISTS raw_session_id uuid;

ALTER TABLE public.decisions
    ADD COLUMN IF NOT EXISTS compacted boolean NOT NULL DEFAULT false;
