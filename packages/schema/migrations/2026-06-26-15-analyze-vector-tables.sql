-- Refresh planner stats after the HNSW/IVFFLAT swap.
ANALYZE public.work_products;
ANALYZE public.lessons;
ANALYZE public.knowledge;
ANALYZE public.decisions;
ANALYZE public.messages;
