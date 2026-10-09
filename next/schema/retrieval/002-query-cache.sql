-- Disposable query embeddings only. No text, credentials or canonical records.
CREATE SEQUENCE retrieval.query_embedding_fences;
CREATE TABLE retrieval.query_embeddings (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    permission_generation text NOT NULL CHECK (length(permission_generation) BETWEEN 1 AND 256),
    identity text NOT NULL,
    query_digest text NOT NULL CHECK (query_digest ~ '^[0-9a-f]{64}$'),
    embedding vector(768),
    expires_at timestamptz NOT NULL DEFAULT '-infinity',
    owner uuid NOT NULL,
    fence bigint NOT NULL DEFAULT nextval('retrieval.query_embedding_fences'),
    lease_until timestamptz NOT NULL,
    PRIMARY KEY (tenant_id,project_id,permission_generation,identity,query_digest)
);
ALTER TABLE retrieval.query_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE retrieval.query_embeddings FORCE ROW LEVEL SECURITY;
CREATE POLICY query_embeddings_scope ON retrieval.query_embeddings
    USING (tenant_id = nullif(current_setting('cortex.tenant_id',true),'')::uuid
        AND project_id = nullif(current_setting('cortex.project_id',true),'')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('cortex.tenant_id',true),'')::uuid
        AND project_id = nullif(current_setting('cortex.project_id',true),'')::uuid);
