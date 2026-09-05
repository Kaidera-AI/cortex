-- Cortex memory-efficiency E2+E4 storage columns (write-side).
-- Adds the distilled/compacted/cold-tier columns the E2 distiller + E4 compactor
-- write to. Fully additive (IF NOT EXISTS); existing rows are untouched and read
-- identically while CORTEX_E2_DISTILL / CORTEX_E4_COMPACT are disabled.
--
-- Apply through the sanctioned migration runner, for example:
--   .agents/scripts/cortex-apply-migrations --apply --target 2026-06-24-03-e2e4-storage-columns.sql
-- Idempotent.

-- Hot tier: messages may be a distilled commitment (E2) rather than a verbatim turn.
ALTER TABLE public.messages ADD COLUMN IF NOT EXISTS distilled boolean NOT NULL DEFAULT false;

-- Cold tier: archive_messages gains a zstd/zlib-compressed raw original + a TTL/supersede.
ALTER TABLE public.archive_messages
    ADD COLUMN IF NOT EXISTS content_zstd bytea,
    ADD COLUMN IF NOT EXISTS retained_until timestamptz,
    ADD COLUMN IF NOT EXISTS raw_session_id uuid;

-- Warm tier: decisions may be a compacted version of the original summary text (E4).
ALTER TABLE public.decisions ADD COLUMN IF NOT EXISTS compacted boolean NOT NULL DEFAULT false;

-- Index the cold-tier linkage so a messages row can expand its raw original cheaply.
CREATE INDEX IF NOT EXISTS idx_archive_messages_raw_session
    ON public.archive_messages (raw_session_id);
