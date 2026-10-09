CREATE SCHEMA retrieval;
CREATE FUNCTION retrieval.finite_vector(values real[]) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT coalesce(bool_and(v IS NOT NULL AND v NOT IN ('NaN'::real, 'Infinity'::real, '-Infinity'::real)), false)
    FROM unnest(values) AS v;
$$;
CREATE TABLE retrieval.embedding_models (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    id uuid NOT NULL,
    provider text NOT NULL CHECK (length(provider) BETWEEN 1 AND 256),
    model text NOT NULL CHECK (length(model) BETWEEN 1 AND 256),
    model_version text NOT NULL CHECK (length(model_version) BETWEEN 1 AND 256),
    dimensions integer NOT NULL CHECK (dimensions BETWEEN 1 AND 4096),
    preprocessing_sha256 text NOT NULL CHECK (preprocessing_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, project_id, id),
    UNIQUE (tenant_id, project_id, id, dimensions),
    UNIQUE (tenant_id, project_id, provider, model, model_version, dimensions, preprocessing_sha256),
    FOREIGN KEY (tenant_id, project_id) REFERENCES core.projects (tenant_id, id)
);
CREATE TRIGGER model_identity_immutable BEFORE UPDATE ON retrieval.embedding_models
    FOR EACH ROW EXECUTE FUNCTION core.refuse_update();
CREATE TABLE retrieval.embeddings (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    record_id uuid NOT NULL,
    source_revision bigint NOT NULL,
    model_id uuid NOT NULL,
    dimensions integer NOT NULL,
    embedding real[] NOT NULL CHECK (array_ndims(embedding) = 1 AND cardinality(embedding) = dimensions AND retrieval.finite_vector(embedding)),
    PRIMARY KEY (tenant_id, project_id, record_id, source_revision, model_id),
    FOREIGN KEY (tenant_id, project_id, record_id, source_revision) REFERENCES core.record_revisions (tenant_id, project_id, record_id, revision),
    FOREIGN KEY (tenant_id, project_id, model_id, dimensions) REFERENCES retrieval.embedding_models (tenant_id, project_id, id, dimensions)
);
CREATE TABLE retrieval.generations (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    id uuid NOT NULL,
    module_id text NOT NULL CHECK (length(module_id) BETWEEN 1 AND 128),
    model_id uuid,
    state text NOT NULL CHECK (state IN ('building','ready','active','retired','blocked')),
    source_cursor bigint NOT NULL CHECK (source_cursor >= 0),
    PRIMARY KEY (tenant_id, project_id, id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES core.projects (tenant_id, id),
    FOREIGN KEY (tenant_id, project_id, model_id) REFERENCES retrieval.embedding_models (tenant_id, project_id, id)
);
CREATE UNIQUE INDEX one_active_generation ON retrieval.generations (tenant_id, project_id, module_id) WHERE state = 'active';
-- Canonical vectors only. Nemo owns pgvector projection/index and graph tables.
