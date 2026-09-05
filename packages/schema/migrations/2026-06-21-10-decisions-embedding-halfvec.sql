-- E3: halfvec (float16) index for decisions — see 09 for rationale.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_decisions_embedding_h
    ON public.decisions USING ivfflat ((embedding::halfvec(768)) halfvec_cosine_ops) WITH (lists = 100);
