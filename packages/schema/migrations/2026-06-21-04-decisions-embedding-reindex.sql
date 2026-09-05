-- memory-efficiency: rebuild the degenerate + bloated decisions vector index. It was
-- WITH (lists='1') and had bloated to ~1GB from 78k embedding-backfill updates (ivfflat
-- doesn't self-vacuum). Fresh lists=100 (~rows/1000) + probes=10 reclaims ~700MB AND
-- gives real vector recall/speed. CONCURRENTLY = no write lock on the live 1.7GB table.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_decisions_embedding_v2
    ON public.decisions USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
