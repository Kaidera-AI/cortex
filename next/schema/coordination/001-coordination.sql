CREATE SCHEMA coordination;
CREATE TABLE coordination.jobs (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    id uuid NOT NULL,
    kind text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_.-]{0,63}$'),
    payload_ref uuid NOT NULL,
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 256),
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','running','succeeded','failed','canceled','unresolved')),
    cancel_requested boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, id),
    UNIQUE (tenant_id, project_id, kind, idempotency_key),
    FOREIGN KEY (tenant_id, project_id, payload_ref) REFERENCES core.payloads (tenant_id, project_id, id)
);
CREATE TABLE coordination.job_attempts (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    id uuid NOT NULL,
    job_id uuid NOT NULL,
    attempt_number integer NOT NULL CHECK (attempt_number > 0),
    fence bigint NOT NULL CHECK (fence > 0),
    worker_id text NOT NULL CHECK (length(worker_id) BETWEEN 1 AND 256),
    started_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, id),
    UNIQUE (tenant_id, project_id, job_id, attempt_number),
    FOREIGN KEY (tenant_id, project_id, job_id) REFERENCES coordination.jobs (tenant_id, project_id, id)
);
CREATE TABLE coordination.job_results (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('succeeded','failed','canceled','unresolved')),
    payload_ref uuid NOT NULL,
    completed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, attempt_id),
    FOREIGN KEY (tenant_id, project_id, attempt_id) REFERENCES coordination.job_attempts (tenant_id, project_id, id),
    FOREIGN KEY (tenant_id, project_id, payload_ref) REFERENCES core.payloads (tenant_id, project_id, id)
);
CREATE TRIGGER job_result_immutable BEFORE UPDATE ON coordination.job_results
    FOR EACH ROW EXECUTE FUNCTION core.refuse_update();
CREATE TABLE coordination.leases (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_.-]{0,63}$'),
    resource_id uuid NOT NULL,
    holder text NOT NULL CHECK (length(holder) BETWEEN 1 AND 256),
    fence bigint NOT NULL CHECK (fence > 0),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, project_id, kind, resource_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES core.projects (tenant_id, id)
);
CREATE FUNCTION coordination.refuse_fence_regression() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.fence < OLD.fence THEN
        RAISE EXCEPTION 'fence regression' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER lease_fence_monotonic BEFORE UPDATE ON coordination.leases
    FOR EACH ROW EXECUTE FUNCTION coordination.refuse_fence_regression();
CREATE TABLE coordination.idempotency (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    request_key text NOT NULL CHECK (length(request_key) BETWEEN 1 AND 256),
    request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    outcome text NOT NULL CHECK (outcome IN ('committed','unresolved','refused')),
    receipt jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, principal_id, request_key),
    FOREIGN KEY (tenant_id, project_id) REFERENCES core.projects (tenant_id, id),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES auth.principals (tenant_id, id),
    CHECK (outcome <> 'committed' OR (receipt IS NOT NULL AND jsonb_typeof(receipt) = 'object'))
);
CREATE TABLE coordination.outbox (
    event_id uuid NOT NULL UNIQUE,
    installation_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    aggregate_id uuid NOT NULL,
    aggregate_kind text NOT NULL CHECK (aggregate_kind ~ '^[a-z][a-z0-9_.-]{0,63}$'),
    aggregate_revision bigint NOT NULL CHECK (aggregate_revision > 0),
    operation text NOT NULL CHECK (operation IN ('upsert','delete')),
    tombstone boolean NOT NULL,
    schema_version integer NOT NULL CHECK (schema_version = 1),
    payload_ref uuid NOT NULL,
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, event_id),
    UNIQUE (tenant_id, project_id, aggregate_id, aggregate_revision),
    FOREIGN KEY (tenant_id, installation_id) REFERENCES core.tenants (id, installation_id),
    FOREIGN KEY (tenant_id, project_id, aggregate_id, aggregate_kind) REFERENCES core.records (tenant_id, project_id, id, kind),
    FOREIGN KEY (tenant_id, project_id, aggregate_id, aggregate_revision, tombstone) REFERENCES core.record_revisions (tenant_id, project_id, record_id, revision, tombstone),
    FOREIGN KEY (tenant_id, project_id, payload_ref, payload_sha256) REFERENCES core.payloads (tenant_id, project_id, id, sha256),
    CHECK ((operation = 'delete') = tombstone)
);
CREATE TRIGGER outbox_immutable BEFORE UPDATE ON coordination.outbox
    FOR EACH ROW EXECUTE FUNCTION core.refuse_update();
CREATE TABLE coordination.published_events (
    installation_id uuid NOT NULL REFERENCES core.installations,
    cursor bigint NOT NULL CHECK (cursor > 0),
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    event_id uuid NOT NULL UNIQUE,
    published_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (installation_id, cursor),
    FOREIGN KEY (tenant_id, project_id, event_id) REFERENCES coordination.outbox (tenant_id, project_id, event_id)
);
CREATE TABLE coordination.quarantine (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    event_id uuid NOT NULL,
    module_id text NOT NULL CHECK (length(module_id) BETWEEN 1 AND 128),
    error_code text NOT NULL CHECK (error_code ~ '^[a-z][a-z0-9_]{0,63}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    PRIMARY KEY (tenant_id, project_id, event_id, module_id),
    FOREIGN KEY (tenant_id, project_id, event_id) REFERENCES coordination.outbox (tenant_id, project_id, event_id)
);
CREATE TABLE coordination.feed_state (
    installation_id uuid PRIMARY KEY REFERENCES core.installations,
    last_published_cursor bigint NOT NULL DEFAULT 0 CHECK (last_published_cursor >= 0),
    retained_floor bigint NOT NULL DEFAULT 0 CHECK (retained_floor >= 0 AND retained_floor <= last_published_cursor),
    retention_seconds integer NOT NULL DEFAULT 604800 CHECK (retention_seconds > 0)
);
CREATE TABLE coordination.consumer_checkpoints (
    installation_id uuid NOT NULL REFERENCES core.installations,
    module_id text NOT NULL CHECK (length(module_id) BETWEEN 1 AND 128),
    applied_cursor bigint NOT NULL DEFAULT 0 CHECK (applied_cursor >= 0),
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','expired','rebuilding','blocked')),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '7 days',
    PRIMARY KEY (installation_id, module_id),
    CHECK (expires_at >= last_seen_at)
);
CREATE TABLE coordination.snapshot_floors (
    installation_id uuid NOT NULL REFERENCES core.installations,
    id uuid NOT NULL,
    purpose text NOT NULL CHECK (purpose IN ('rebuild','backup','recovery')),
    cursor bigint NOT NULL CHECK (cursor >= 0),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (installation_id, id)
);
-- Cursor allocation, pruning and consumer expiry are explicit C06/C07 ports.
