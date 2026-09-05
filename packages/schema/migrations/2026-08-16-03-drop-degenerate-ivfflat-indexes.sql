-- Drop the degenerate `WITH (lists = 1)` ivfflat indexes. Again.
--
-- This is the THIRD attempt. What is KNOWN about why the first two did not hold:
--   * 2026-06-26-01..05 added HNSW replacements, 06..10 dropped the ivfflat ones.
--     All ten are recorded applied in cortex_schema_migrations, yet all five indexes
--     were present again on 2026-08-16.
--   * schema.sql's own header records the earlier cause: `cortex-sync-workspace`
--     re-applied that file unconditionally and recreated what the migrations removed.
--     schema.sql has since been corrected to HNSW-only.
--   * The 2026-08-15 PG18 logical restore would then have carried whatever indexes
--     existed at dump time onto the new volume, and because 06..10 are already marked
--     applied, nothing re-dropped them.
--
-- NOT PROVEN, stated so the next engineer does not inherit a guess: the exact
-- recreation event was not reproduced on 2026-08-16. Dropping all five and then both
-- restarting cortex-api AND exercising a work-products read left them absent. So the
-- `CREATE INDEX ... USING ivfflat` still sitting in ensure_work_products_schema is
-- removed in this commit as hygiene for fresh deploys - it is NOT demonstrated to be
-- the mechanism that resurrected them here.
--
-- Measured on the local Apple Container deployment, 2026-08-16, stats never reset:
--   idx_decisions_embedding      587 MB   0 lifetime scans   (hnsw sibling: 18 scans)
--   idx_messages_embedding        25 MB   0 lifetime scans
--   idx_knowledge_embedding       22 MB   0 lifetime scans   (hnsw sibling: 59 scans)
--   idx_lessons_embedding       7032 kB   0 lifetime scans
--   idx_work_products_embedding  344 kB   0 lifetime scans
--   total 641 MB, zero scans between them.
--
-- `lists = 1` is a single cluster, which is degenerate: the planner correctly ignores
-- it. Every one of these five tables has an HNSW index on the same column and opclass,
-- so ANN coverage is unchanged. The cost of keeping them is not only disk - each one is
-- maintained on every insert, and messages grows ~356k rows/month.
--
-- The remaining `CREATE INDEX IF NOT EXISTS ... USING ivfflat` in main.py's
-- ensure_work_products_schema is switched to HNSW in the same commit, so no code path
-- can reintroduce a degenerate index on a fresh deploy.

DROP INDEX IF EXISTS public.idx_decisions_embedding;
DROP INDEX IF EXISTS public.idx_messages_embedding;
DROP INDEX IF EXISTS public.idx_knowledge_embedding;
DROP INDEX IF EXISTS public.idx_lessons_embedding;
DROP INDEX IF EXISTS public.idx_work_products_embedding;

-- Halfvec variants, in case a deployment enabled CORTEX_VECTOR_PRECISION=halfvec and
-- picked up the 2026-06-21-08..11 ivfflat casts. Harmless where they never existed.
DROP INDEX IF EXISTS public.idx_lessons_embedding_halfvec;
DROP INDEX IF EXISTS public.idx_knowledge_embedding_halfvec;
DROP INDEX IF EXISTS public.idx_decisions_embedding_halfvec;
DROP INDEX IF EXISTS public.idx_messages_embedding_halfvec;
