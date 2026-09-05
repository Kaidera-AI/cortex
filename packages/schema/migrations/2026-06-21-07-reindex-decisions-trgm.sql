-- memory-efficiency: REINDEX the bloated decisions summary trigram index (CONCURRENTLY).
REINDEX INDEX CONCURRENTLY public.idx_decisions_summary_trgm;
