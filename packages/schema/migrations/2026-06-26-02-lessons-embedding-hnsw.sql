-- Vector search performance: HNSW over full-precision lesson embeddings.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lessons_embedding_hnsw
    ON public.lessons USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
