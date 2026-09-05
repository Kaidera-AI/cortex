-- memory-efficiency E1: drop the degenerate lists=1 index now the v2 index exists.
DROP INDEX CONCURRENTLY IF EXISTS public.idx_messages_embedding;
