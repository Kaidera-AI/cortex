-- Vector search performance: HNSW over full-precision message embeddings.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_messages_embedding_hnsw
    ON public.messages USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
