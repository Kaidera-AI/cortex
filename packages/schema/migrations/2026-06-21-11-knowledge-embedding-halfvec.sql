-- E3: halfvec index for knowledge (so all 4 tables in the multi-table vector query have one).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_knowledge_embedding_h
    ON public.knowledge USING ivfflat ((embedding::halfvec(768)) halfvec_cosine_ops) WITH (lists = 50);
