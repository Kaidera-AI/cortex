CREATE SCHEMA IF NOT EXISTS core;

CREATE FUNCTION core.refuse_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'canonical history is immutable' USING ERRCODE = '55000';
END;
$$;

CREATE TABLE core.installations (
    id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE core.tenants (
    id uuid PRIMARY KEY,
    installation_id uuid NOT NULL REFERENCES core.installations,
    UNIQUE (id, installation_id)
);
CREATE TABLE core.projects (
    tenant_id uuid NOT NULL REFERENCES core.tenants,
    id uuid NOT NULL,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, name)
);
CREATE TABLE core.payloads (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    id uuid NOT NULL,
    body bytea NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, id),
    UNIQUE (tenant_id, project_id, id, sha256),
    FOREIGN KEY (tenant_id, project_id) REFERENCES core.projects (tenant_id, id),
    CONSTRAINT payload_digest_matches CHECK (sha256 = encode(sha256(body), 'hex'))
);
CREATE TRIGGER payload_immutable BEFORE UPDATE ON core.payloads
    FOR EACH ROW EXECUTE FUNCTION core.refuse_update();

CREATE TABLE core.records (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    id uuid NOT NULL,
    kind text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_.-]{0,63}$'),
    current_revision bigint NOT NULL CHECK (current_revision > 0),
    tombstone boolean NOT NULL,
    PRIMARY KEY (tenant_id, project_id, id),
    UNIQUE (tenant_id, project_id, id, kind),
    FOREIGN KEY (tenant_id, project_id) REFERENCES core.projects (tenant_id, id)
);
CREATE TABLE core.record_revisions (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    record_id uuid NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    payload_ref uuid NOT NULL,
    tombstone boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, record_id, revision),
    UNIQUE (tenant_id, project_id, record_id, revision, tombstone),
    FOREIGN KEY (tenant_id, project_id, record_id) REFERENCES core.records (tenant_id, project_id, id),
    FOREIGN KEY (tenant_id, project_id, payload_ref) REFERENCES core.payloads (tenant_id, project_id, id)
);
ALTER TABLE core.records ADD CONSTRAINT record_head_has_history
    FOREIGN KEY (tenant_id, project_id, id, current_revision, tombstone)
    REFERENCES core.record_revisions (tenant_id, project_id, record_id, revision, tombstone)
    DEFERRABLE INITIALLY DEFERRED;
CREATE TRIGGER revision_immutable BEFORE UPDATE ON core.record_revisions
    FOR EACH ROW EXECUTE FUNCTION core.refuse_update();
CREATE TABLE core.record_aliases (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    source_namespace text NOT NULL CHECK (length(source_namespace) BETWEEN 1 AND 256),
    external_id text NOT NULL CHECK (length(external_id) BETWEEN 1 AND 512),
    record_id uuid NOT NULL,
    PRIMARY KEY (tenant_id, project_id, source_namespace, external_id),
    FOREIGN KEY (tenant_id, project_id, record_id) REFERENCES core.records (tenant_id, project_id, id)
);
CREATE TABLE core.blob_manifests (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    id uuid NOT NULL,
    record_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('original', 'generated')),
    object_key text NOT NULL CHECK (length(object_key) BETWEEN 1 AND 512 AND object_key !~ '(^/|(^|/)\.\.(/|$))'),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    byte_length bigint NOT NULL CHECK (byte_length >= 0),
    mime_type text NOT NULL CHECK (length(mime_type) BETWEEN 1 AND 128),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, id),
    UNIQUE (tenant_id, project_id, object_key),
    FOREIGN KEY (tenant_id, project_id, record_id) REFERENCES core.records (tenant_id, project_id, id)
);
CREATE TRIGGER blob_manifest_immutable BEFORE UPDATE ON core.blob_manifests
    FOR EACH ROW EXECUTE FUNCTION core.refuse_update();
CREATE TABLE core.extraction_facts (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    id uuid NOT NULL,
    record_id uuid NOT NULL,
    source_revision bigint NOT NULL,
    extractor_identity text NOT NULL CHECK (length(extractor_identity) BETWEEN 1 AND 512),
    payload_ref uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, id),
    FOREIGN KEY (tenant_id, project_id, record_id, source_revision) REFERENCES core.record_revisions (tenant_id, project_id, record_id, revision),
    FOREIGN KEY (tenant_id, project_id, payload_ref) REFERENCES core.payloads (tenant_id, project_id, id)
);
CREATE TRIGGER extraction_fact_immutable BEFORE UPDATE ON core.extraction_facts
    FOR EACH ROW EXECUTE FUNCTION core.refuse_update();
CREATE TABLE core.analytics_facts (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    id uuid NOT NULL,
    kind text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_.-]{0,63}$'),
    payload_ref uuid NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, id),
    FOREIGN KEY (tenant_id, project_id, payload_ref) REFERENCES core.payloads (tenant_id, project_id, id)
);
CREATE TRIGGER analytics_fact_immutable BEFORE UPDATE ON core.analytics_facts
    FOR EACH ROW EXECUTE FUNCTION core.refuse_update();
