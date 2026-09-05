-- E3 feasibility probe: build a halfvec index on the SMALL lessons table (1.3k rows, fast).
-- If this succeeds, halfvec (pgvector >=0.7) is available and the cast-expression approach
-- works; if it errors ("type halfvec does not exist"), E3 is not feasible on this image.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lessons_embedding_h
    ON public.lessons USING ivfflat ((embedding::halfvec(768)) halfvec_cosine_ops) WITH (lists = 16);
