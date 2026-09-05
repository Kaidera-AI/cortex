-- memory-efficiency: drop the degenerate/bloated lists=1 decisions index now v2 exists.
DROP INDEX CONCURRENTLY IF EXISTS public.idx_decisions_embedding;
