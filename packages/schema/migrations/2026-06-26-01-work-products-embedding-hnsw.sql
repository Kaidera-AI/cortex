-- Vector search performance: replace degenerate float32 IVFFLAT with HNSW.
-- One CONCURRENTLY statement per migration file: the migration API executes a
-- file as one statement batch, and PostgreSQL rejects CREATE INDEX CONCURRENTLY
-- inside an implicit multi-statement transaction block.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_work_products_embedding_hnsw
    ON public.work_products USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
