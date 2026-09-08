
-- Additive service credentials, deliberately outside public's broad app grants.
-- Canonical people/projects remain public.cortex_actors/agents/cortex_projects.
CREATE SCHEMA IF NOT EXISTS cortex_auth;
REVOKE ALL ON SCHEMA cortex_auth FROM PUBLIC;

CREATE FUNCTION cortex_auth.valid_scopes(value text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT value IS NOT NULL AND array_position(value, NULL) IS NULL
       AND value <@ ARRAY['memory:read','memory:write','runtime:read',
           'coordination:read','coordination:write','ingest:write',
           'registry:read','registry:write','instance:admin','tokens:manage']::text[]
$$;

CREATE TABLE cortex_auth.state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    instance_id uuid NOT NULL DEFAULT gen_random_uuid(),
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    initialized_at timestamptz
);
INSERT INTO cortex_auth.state(singleton) VALUES (true);

CREATE TABLE cortex_auth.principals (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES public.cortex_projects(id),
    agent_id uuid NOT NULL REFERENCES public.agents(id),
    actor_id uuid NOT NULL REFERENCES public.cortex_actors(id),
    installation_id text NOT NULL CHECK (length(installation_id) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL,
    disabled_at timestamptz,
    UNIQUE (installation_id, project_id, agent_id)
);
CREATE TABLE cortex_auth.grants (
    principal_id uuid PRIMARY KEY REFERENCES cortex_auth.principals(id),
    scopes text[] NOT NULL CHECK (cortex_auth.valid_scopes(scopes)),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    disabled_at timestamptz
);
CREATE TABLE cortex_auth.tokens (
    id uuid PRIMARY KEY,
    principal_id uuid NOT NULL REFERENCES cortex_auth.principals(id),
    family_id uuid NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    token_digest bytea NOT NULL CHECK (octet_length(token_digest) = 32),
    scopes text[] NOT NULL CHECK (cortex_auth.valid_scopes(scopes) AND cardinality(scopes) > 0),
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    rotate_after timestamptz NOT NULL,
    revoked_at timestamptz,
    predecessor_id uuid REFERENCES cortex_auth.tokens(id),
    rotation_started_at timestamptz,
    overlap_deadline timestamptz,
    consumer_used_at timestamptz,
    acknowledged_at timestamptz,
    CHECK (expires_at = issued_at + interval '720 hours'),
    CHECK (rotate_after = issued_at + interval '504 hours'),
    CHECK (predecessor_id IS DISTINCT FROM id),
    CHECK ((rotation_started_at IS NULL AND overlap_deadline IS NULL) OR
           (rotation_started_at IS NOT NULL AND overlap_deadline IS NOT NULL
            AND overlap_deadline <= rotation_started_at + interval '24 hours'
            AND overlap_deadline <= expires_at))
);
CREATE INDEX service_tokens_principal ON cortex_auth.tokens(principal_id);
CREATE INDEX service_tokens_due ON cortex_auth.tokens(rotate_after) WHERE revoked_at IS NULL;
-- Orphan candidates may be revoked and replaced, without extending the parent's
-- original overlap deadline. At most one unrevoked candidate per predecessor.
CREATE UNIQUE INDEX service_tokens_one_candidate ON cortex_auth.tokens(predecessor_id)
    WHERE predecessor_id IS NOT NULL AND revoked_at IS NULL;

CREATE TABLE cortex_auth.setup_grants (
    id uuid PRIMARY KEY,
    purpose text NOT NULL CHECK (purpose IN ('bootstrap', 'recovery')),
    token_digest bytea NOT NULL CHECK (octet_length(token_digest) = 32),
    generation bigint NOT NULL CHECK (generation > 0),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at = created_at + interval '10 minutes'),
    consumed_at timestamptz,
    revoked_at timestamptz
);
CREATE TABLE cortex_auth.audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at timestamptz NOT NULL,
    action text NOT NULL,
    principal_id uuid,
    token_id uuid,
    generation bigint NOT NULL
);
COMMENT ON TABLE cortex_auth.audit IS
    'Security lifecycle metadata only: never plaintext credentials, digests, headers or request payloads.';

CREATE FUNCTION cortex_auth.keep_principal_identity() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.id, NEW.project_id, NEW.agent_id, NEW.actor_id, NEW.installation_id)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.project_id, OLD.agent_id, OLD.actor_id, OLD.installation_id) THEN
        RAISE EXCEPTION 'Service principal identity is immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER service_principal_identity BEFORE UPDATE ON cortex_auth.principals
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.keep_principal_identity();

REVOKE ALL ON ALL TABLES IN SCHEMA cortex_auth FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA cortex_auth FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA cortex_auth FROM PUBLIC;
DO $$
DECLARE runtime_role text;
BEGIN
    FOREACH runtime_role IN ARRAY ARRAY['cortex_app','cortex_reader'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = runtime_role) THEN
            EXECUTE format('REVOKE ALL ON SCHEMA cortex_auth FROM %I', runtime_role);
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA cortex_auth FROM %I', runtime_role);
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA cortex_auth FROM %I', runtime_role);
            EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA cortex_auth FROM %I', runtime_role);
        END IF;
    END LOOP;
END $$;
