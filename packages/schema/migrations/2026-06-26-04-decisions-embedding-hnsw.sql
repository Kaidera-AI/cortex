-- Vector search performance: HNSW over full-precision decision embeddings.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_decisions_embedding_hnsw
    ON public.decisions USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
