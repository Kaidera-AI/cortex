-- Drop unused halfvec IVFFLAT path while CORTEX_VECTOR_PRECISION=float32.
DROP INDEX CONCURRENTLY IF EXISTS public.idx_decisions_embedding_h;
