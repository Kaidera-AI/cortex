-- Additive projection. Installer migration role only; never executed by API startup.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS retrieval;
CREATE TABLE retrieval.search_state (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    identity text NOT NULL,
    state text NOT NULL CHECK (state IN ('ready', 'disabled', 'rebuilding')),
    PRIMARY KEY (tenant_id, project_id)
);
CREATE TABLE retrieval.search_sources (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    record_id text NOT NULL CHECK (length(record_id) BETWEEN 1 AND 256),
    kind text NOT NULL CHECK (length(kind) BETWEEN 1 AND 64),
    source_revision bigint NOT NULL CHECK (source_revision > 0),
    PRIMARY KEY (tenant_id, project_id, record_id)
);
CREATE TABLE retrieval.search_vectors (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    record_id text NOT NULL,
    source_revision bigint NOT NULL CHECK (source_revision > 0),
    identity text NOT NULL,
    embedding vector(768) NOT NULL,
    PRIMARY KEY (tenant_id, project_id, record_id),
    FOREIGN KEY (tenant_id, project_id, record_id)
        REFERENCES retrieval.search_sources ON DELETE CASCADE
);
CREATE INDEX search_vectors_hnsw ON retrieval.search_vectors
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX search_vectors_scope ON retrieval.search_vectors (tenant_id, project_id, identity);
ALTER TABLE retrieval.search_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE retrieval.search_state FORCE ROW LEVEL SECURITY;
CREATE POLICY search_state_scope ON retrieval.search_state
    USING (tenant_id = nullif(current_setting('cortex.tenant_id', true), '')::uuid
       AND project_id = nullif(current_setting('cortex.project_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('cortex.tenant_id', true), '')::uuid
       AND project_id = nullif(current_setting('cortex.project_id', true), '')::uuid);
ALTER TABLE retrieval.search_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE retrieval.search_sources FORCE ROW LEVEL SECURITY;
CREATE POLICY search_sources_scope ON retrieval.search_sources
    USING (tenant_id = nullif(current_setting('cortex.tenant_id', true), '')::uuid
       AND project_id = nullif(current_setting('cortex.project_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('cortex.tenant_id', true), '')::uuid
       AND project_id = nullif(current_setting('cortex.project_id', true), '')::uuid);
ALTER TABLE retrieval.search_vectors ENABLE ROW LEVEL SECURITY;
ALTER TABLE retrieval.search_vectors FORCE ROW LEVEL SECURITY;
CREATE POLICY search_vectors_scope ON retrieval.search_vectors
    USING (tenant_id = nullif(current_setting('cortex.tenant_id', true), '')::uuid
       AND project_id = nullif(current_setting('cortex.project_id', true), '')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('cortex.tenant_id', true), '')::uuid
       AND project_id = nullif(current_setting('cortex.project_id', true), '')::uuid);
