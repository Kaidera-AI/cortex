-- E3: halfvec (float16) index for messages — ~2x smaller than the float32 ivfflat, and
-- faster vector scans. Built on a CAST expression so the embedding column stays vector(768)
-- (NO table rewrite). Activated only when CORTEX_VECTOR_PRECISION=halfvec makes the query
-- cast identically; otherwise this index is simply unused (the float32 _v2 index stays).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_messages_embedding_h
    ON public.messages USING ivfflat ((embedding::halfvec(768)) halfvec_cosine_ops) WITH (lists = 100);
