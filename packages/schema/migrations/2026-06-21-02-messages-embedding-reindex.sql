-- memory-efficiency E1: rebuild the degenerate messages vector index. It was built
-- WITH (lists='1') = ONE list = a full scan of all 81k 768-d vectors on every /search.
-- lists=100 (~rows/1000) + probes=10 scans ~10% of vectors → ~10x less vector work at
-- ~95%+ recall. CONCURRENTLY = no write lock on the live 4.8GB table; single statement so
-- it runs outside a transaction (a CONCURRENTLY requirement). IF NOT EXISTS = idempotent.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_messages_embedding_v2
    ON public.messages USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
