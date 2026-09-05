-- Align L5 artifact embeddings with the 768-dimensional Cortex embedding contract.
-- Artifact ingestion has never written embeddings; fail closed rather than discard any
-- unexpected 2048-dimensional data from an out-of-band writer.
DO $$
DECLARE
    embedding_type text;
BEGIN
    IF to_regclass('public.artifacts') IS NULL THEN
        RETURN;
    END IF;

    SELECT format_type(attribute.atttypid, attribute.atttypmod)
      INTO embedding_type
      FROM pg_attribute attribute
     WHERE attribute.attrelid = 'public.artifacts'::regclass
       AND attribute.attname = 'embedding'
       AND NOT attribute.attisdropped;

    IF embedding_type = 'vector(768)' THEN
        RETURN;
    END IF;

    IF EXISTS (SELECT 1 FROM public.artifacts WHERE embedding IS NOT NULL) THEN
        RAISE EXCEPTION
            'artifacts.embedding is % with non-NULL data; migrate those vectors explicitly before converting to vector(768)',
            embedding_type;
    END IF;

    ALTER TABLE public.artifacts
        ALTER COLUMN embedding TYPE vector(768);
END
$$;

CREATE INDEX IF NOT EXISTS idx_artifacts_embedding_hnsw
    ON public.artifacts USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
