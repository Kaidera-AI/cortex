-- Forward repair for adopted handback receipts whose live status constraint
-- still has the older six-state definition. Preserve all historical receipts,
-- rows, handback links and indexes; do not edit or re-stamp an applied migration.
-- The product migration engine owns the transaction. A short lock timeout makes
-- a busy database fail safely so the same migration can be retried later.
SET LOCAL lock_timeout = '5s';

ALTER TABLE public.handoffs
    DROP CONSTRAINT IF EXISTS handoffs_status_check;
ALTER TABLE public.handoffs
    ADD CONSTRAINT handoffs_status_check CHECK (
        status IN (
            'pending', 'claimed', 'returned', 'completed',
            'released', 'abandoned', 'failed', 'archived'
        )
    );
