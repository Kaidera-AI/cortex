-- E010 Phase 2: first-class handoff execution policy fields.
--
-- These fields are JSONB by design: acceptance/evidence/retry/escalation policies
-- vary by project pack and should evolve without adding a column per subfield.

ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS acceptance jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS evidence jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS retry jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS escalation jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE archive_handoffs ADD COLUMN IF NOT EXISTS acceptance jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE archive_handoffs ADD COLUMN IF NOT EXISTS evidence jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE archive_handoffs ADD COLUMN IF NOT EXISTS retry jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE archive_handoffs ADD COLUMN IF NOT EXISTS escalation jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN handoffs.acceptance IS 'Structured acceptance contract for the handoff, for example criteria, required checks, or approver expectations.';
COMMENT ON COLUMN handoffs.evidence IS 'Structured evidence requirements/results for completing the handoff.';
COMMENT ON COLUMN handoffs.retry IS 'Structured retry policy for failed or rejected execution attempts.';
COMMENT ON COLUMN handoffs.escalation IS 'Structured escalation policy for blocked, stale, or failed handoffs.';
