BEGIN;

-- Completion handbacks are typed, linked, and carry a durable return receipt.
ALTER TABLE handoffs
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'task',
    ADD COLUMN IF NOT EXISTS reply_to_handoff_id UUID,
    ADD COLUMN IF NOT EXISTS returned_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS completion_report JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE archive_handoffs
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'task',
    ADD COLUMN IF NOT EXISTS reply_to_handoff_id UUID,
    ADD COLUMN IF NOT EXISTS returned_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS completion_report JSONB NOT NULL DEFAULT '{}'::jsonb;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'handoffs_kind_check'
           AND conrelid = 'handoffs'::regclass
    ) THEN
        ALTER TABLE handoffs
            ADD CONSTRAINT handoffs_kind_check
            CHECK (kind IN ('task', 'completion_handback'));
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'archive_handoffs_kind_check'
           AND conrelid = 'archive_handoffs'::regclass
    ) THEN
        ALTER TABLE archive_handoffs
            ADD CONSTRAINT archive_handoffs_kind_check
            CHECK (kind IN ('task', 'completion_handback'));
    END IF;

END
$$;

-- A parent must never disappear while a newer or active review receipt exists.
ALTER TABLE handoffs
    DROP CONSTRAINT IF EXISTS handoffs_reply_to_handoff_id_fkey;
ALTER TABLE handoffs
    ADD CONSTRAINT handoffs_reply_to_handoff_id_fkey
    FOREIGN KEY (reply_to_handoff_id)
    REFERENCES handoffs(id);

-- Replace the historical check atomically so every live lifecycle state remains valid.
ALTER TABLE handoffs
    DROP CONSTRAINT IF EXISTS handoffs_status_check;

ALTER TABLE handoffs
    ADD CONSTRAINT handoffs_status_check
    CHECK (
        status IN (
            'pending',
            'claimed',
            'returned',
            'completed',
            'released',
            'abandoned',
            'failed',
            'archived'
        )
    );

CREATE INDEX IF NOT EXISTS idx_handoffs_reply_to_handoff_id
    ON handoffs (reply_to_handoff_id);

DROP INDEX IF EXISTS idx_handoffs_completion_handback_active;

CREATE UNIQUE INDEX idx_handoffs_completion_handback_active
    ON handoffs (project, reply_to_handoff_id)
    WHERE kind = 'completion_handback'
      AND invalidated_at IS NULL
      AND status IN ('pending', 'claimed');

COMMENT ON COLUMN handoffs.kind IS
    'Handoff record type: delegated task or completion handback.';
COMMENT ON COLUMN handoffs.reply_to_handoff_id IS
    'Original task addressed by a completion handback.';
COMMENT ON COLUMN handoffs.returned_at IS
    'Timestamp the worker returned the task for delegator review.';
COMMENT ON COLUMN handoffs.completion_report IS
    'Structured worker outcome, evidence, risks, and follow-up actions.';

COMMIT;
