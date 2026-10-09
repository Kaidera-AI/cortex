-- Derived graph only. C03 owns Core facts; installer registers this migration.
CREATE SCHEMA IF NOT EXISTS retrieval;
CREATE TABLE retrieval.graph_generations (
    tenant_id uuid NOT NULL, project_id uuid NOT NULL, id uuid NOT NULL,
    extractor_identity text NOT NULL CHECK(length(extractor_identity) BETWEEN 1 AND 512),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,project_id,id),
    FOREIGN KEY(tenant_id,project_id) REFERENCES core.projects(tenant_id,id)
);
CREATE TABLE retrieval.graph_state (
    tenant_id uuid NOT NULL, project_id uuid NOT NULL, active_generation uuid NOT NULL,
    state text NOT NULL CHECK(state IN ('ready','disabled','rebuilding')),
    PRIMARY KEY(tenant_id,project_id),
    FOREIGN KEY(tenant_id,project_id,active_generation)
        REFERENCES retrieval.graph_generations(tenant_id,project_id,id)
);
CREATE TABLE retrieval.graph_applied (
    tenant_id uuid NOT NULL, project_id uuid NOT NULL, generation uuid NOT NULL,
    record_id uuid NOT NULL, source_revision bigint NOT NULL CHECK(source_revision>0),
    source_kind text NOT NULL CHECK(length(source_kind) BETWEEN 1 AND 64),
    source_label text NOT NULL CHECK(length(source_label) BETWEEN 1 AND 180),
    source_description text NOT NULL CHECK(length(source_description)<=420),
    applied_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,project_id,generation,record_id),
    FOREIGN KEY(tenant_id,project_id,generation)
        REFERENCES retrieval.graph_generations(tenant_id,project_id,id) ON DELETE CASCADE,
    FOREIGN KEY(tenant_id,project_id,record_id,source_revision)
        REFERENCES core.record_revisions(tenant_id,project_id,record_id,revision)
);
CREATE TABLE retrieval.graph_nodes (
    tenant_id uuid NOT NULL, project_id uuid NOT NULL, generation uuid NOT NULL,
    record_id uuid NOT NULL, name text NOT NULL CHECK(length(name) BETWEEN 1 AND 256),
    entity_type text NOT NULL CHECK(entity_type IN ('concept','epic','service','project','product','work_product','file','tool','endpoint','table','branch','model','agent')),
    description text NOT NULL CHECK(length(description)<=500),
    PRIMARY KEY(tenant_id,project_id,generation,record_id,name),
    FOREIGN KEY(tenant_id,project_id,generation,record_id)
        REFERENCES retrieval.graph_applied(tenant_id,project_id,generation,record_id) ON DELETE CASCADE
);
CREATE TABLE retrieval.graph_edges (
    tenant_id uuid NOT NULL, project_id uuid NOT NULL, generation uuid NOT NULL,
    record_id uuid NOT NULL, source text NOT NULL, target text NOT NULL,
    relationship_type text NOT NULL CHECK(relationship_type ~ '^[a-z][a-z0-9_]{0,63}$'),
    description text NOT NULL CHECK(length(description)<=500),
    PRIMARY KEY(tenant_id,project_id,generation,record_id,source,target,relationship_type),
    FOREIGN KEY(tenant_id,project_id,generation,record_id,source)
        REFERENCES retrieval.graph_nodes(tenant_id,project_id,generation,record_id,name) ON DELETE CASCADE,
    FOREIGN KEY(tenant_id,project_id,generation,record_id,target)
        REFERENCES retrieval.graph_nodes(tenant_id,project_id,generation,record_id,name) ON DELETE CASCADE
);
CREATE INDEX graph_nodes_scope_name ON retrieval.graph_nodes(tenant_id,project_id,generation,name);
CREATE INDEX graph_edges_scope_source ON retrieval.graph_edges(tenant_id,project_id,generation,source);
CREATE INDEX graph_edges_scope_target ON retrieval.graph_edges(tenant_id,project_id,generation,target);
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['graph_generations','graph_state','graph_applied','graph_nodes','graph_edges'] LOOP
        EXECUTE format('ALTER TABLE retrieval.%I ENABLE ROW LEVEL SECURITY',t);
        EXECUTE format('ALTER TABLE retrieval.%I FORCE ROW LEVEL SECURITY',t);
        EXECUTE format('CREATE POLICY scope ON retrieval.%I USING
          (tenant_id=nullif(current_setting(''cortex.tenant_id'',true),'''')::uuid
           AND project_id=nullif(current_setting(''cortex.project_id'',true),'''')::uuid)
          WITH CHECK (tenant_id=nullif(current_setting(''cortex.tenant_id'',true),'''')::uuid
           AND project_id=nullif(current_setting(''cortex.project_id'',true),'''')::uuid)',t);
    END LOOP;
END $$;
