-- E002 Inc05 goal ancestry metadata-first schema support
-- Adds nullable parent_goal_id columns and indexes. No backfill.

ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS parent_goal_id TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS parent_goal_id TEXT;

CREATE INDEX IF NOT EXISTS idx_handoffs_parent_goal_id ON handoffs(parent_goal_id);
CREATE INDEX IF NOT EXISTS idx_decisions_parent_goal_id ON decisions(parent_goal_id);
