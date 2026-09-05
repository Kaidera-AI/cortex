ALTER TABLE handoffs
    ADD COLUMN IF NOT EXISTS retry_count integer NOT NULL DEFAULT 0;
