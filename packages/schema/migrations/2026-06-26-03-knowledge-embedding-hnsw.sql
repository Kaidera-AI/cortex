-- Vector search performance: HNSW over full-precision knowledge embeddings.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_knowledge_embedding_hnsw
    ON public.knowledge USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
