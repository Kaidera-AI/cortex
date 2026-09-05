BEGIN;

ALTER TABLE work_products
    ADD COLUMN IF NOT EXISTS commit_sha TEXT,
    ADD COLUMN IF NOT EXISTS file_hashes JSONB DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS symbol_hashes JSONB DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS freshness_status TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS freshness_reason TEXT,
    ADD COLUMN IF NOT EXISTS freshness_checked_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS projection_status TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS projection_error TEXT,
    ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ NULL;

UPDATE work_products
   SET freshness_status = COALESCE(NULLIF(freshness_status, ''), 'unknown'),
       projection_status = COALESCE(NULLIF(projection_status, ''), 'pending'),
       valid_from = COALESCE(valid_from, created_at, NOW())
 WHERE freshness_status IS NULL
    OR projection_status IS NULL
    OR valid_from IS NULL;

CREATE INDEX IF NOT EXISTS idx_work_products_file_hashes
    ON work_products USING GIN (file_hashes);
CREATE INDEX IF NOT EXISTS idx_work_products_freshness
    ON work_products (project, freshness_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_work_products_projection
    ON work_products (project, projection_status, updated_at DESC);

COMMIT;
