-- E3 halfvec cutover: drop the float32 vector index now that the halfvec index exists.
-- WARNING: only apply when CORTEX_VECTOR_PRECISION=halfvec (else the float32 default path
-- loses its index -> seq scan). This is the halfvec-cutover step.
DROP INDEX CONCURRENTLY IF EXISTS public.idx_knowledge_embedding;
