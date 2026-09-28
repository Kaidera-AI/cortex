DO $bootstrap$
BEGIN
    IF current_user <> 'cortex_v2_migrator' THEN
        RAISE EXCEPTION 'core migration must run as cortex_v2_migrator';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM pg_roles
         WHERE rolname = 'cortex_v2_app'
           AND rolcanlogin
           AND NOT rolbypassrls
           AND NOT rolsuper
           AND NOT rolcreaterole
           AND NOT rolcreatedb
    ) THEN
        RAISE EXCEPTION 'cortex_v2_app must be a non-privileged NOBYPASSRLS login role';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM pg_roles
         WHERE rolname = 'cortex_v2_migrator'
           AND rolcanlogin
           AND NOT rolbypassrls
           AND NOT rolsuper
    ) THEN
        RAISE EXCEPTION 'cortex_v2_migrator must be a separate NOBYPASSRLS login role';
    END IF;
END
$bootstrap$;

CREATE TABLE IF NOT EXISTS cortex_core.schema_migrations (
    migration_id text PRIMARY KEY,
    checksum_sha256 text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cortex_auth.principals (
    principal_id uuid PRIMARY KEY,
    installation_id uuid NOT NULL,
    principal_name text NOT NULL,
    status text NOT NULL CHECK (status IN ('active', 'revoked')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cortex_auth.credentials (
    credential_id uuid PRIMARY KEY,
    principal_id uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
    generation integer NOT NULL CHECK (generation > 0),
    expires_at timestamptz,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION cortex_auth.authenticate(p_token_hash bytea)
RETURNS TABLE (principal_id uuid, installation_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
    SELECT p.principal_id, p.installation_id
      FROM cortex_auth.credentials AS c
      JOIN cortex_auth.principals AS p ON p.principal_id = c.principal_id
     WHERE c.token_hash = p_token_hash
       AND c.revoked_at IS NULL
       AND (c.expires_at IS NULL OR c.expires_at > pg_catalog.now())
       AND p.status = 'active'
$function$;

REVOKE ALL ON FUNCTION cortex_auth.authenticate(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_auth.authenticate(bytea) TO cortex_v2_app;

CREATE TABLE cortex_core.scopes (
    scope_id uuid PRIMARY KEY,
    scope_kind text NOT NULL CHECK (scope_kind IN ('project', 'shared', 'local')),
    display_name text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cortex_auth.scope_grants (
    principal_id uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    can_read boolean NOT NULL DEFAULT false,
    can_write boolean NOT NULL DEFAULT false,
    can_publish boolean NOT NULL DEFAULT false,
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    PRIMARY KEY (principal_id, scope_id),
    CHECK (NOT can_write OR can_read),
    CHECK (NOT can_publish OR can_write)
);

CREATE TABLE cortex_core.scope_aliases (
    alias text PRIMARY KEY CHECK (length(alias) BETWEEN 1 AND 96),
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    is_primary boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    retired_at timestamptz
);

CREATE UNIQUE INDEX scope_aliases_one_primary_per_scope
    ON cortex_core.scope_aliases(scope_id)
    WHERE is_primary AND retired_at IS NULL;

CREATE INDEX scope_grants_active_scope
    ON cortex_auth.scope_grants(principal_id, scope_id)
    WHERE revoked_at IS NULL;

ALTER TABLE cortex_auth.scope_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_auth.scope_grants FORCE ROW LEVEL SECURITY;
CREATE POLICY scope_grants_principal_read ON cortex_auth.scope_grants
    FOR SELECT TO cortex_v2_app
    USING (
        principal_id = NULLIF(current_setting('cortex.principal_id', true), '')::uuid
    );
CREATE POLICY scope_grants_migrator_all ON cortex_auth.scope_grants
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_core.scopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.scopes FORCE ROW LEVEL SECURITY;
CREATE POLICY scopes_granted_read ON cortex_core.scopes
    FOR SELECT TO cortex_v2_app
    USING (
        scope_id IN (
            SELECT g.scope_id
              FROM cortex_auth.scope_grants AS g
             WHERE g.revoked_at IS NULL
               AND (g.can_read OR g.can_write)
        )
    );
CREATE POLICY scopes_migrator_all ON cortex_core.scopes
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_core.scope_aliases ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.scope_aliases FORCE ROW LEVEL SECURITY;
CREATE POLICY aliases_granted_read ON cortex_core.scope_aliases
    FOR SELECT TO cortex_v2_app
    USING (
        retired_at IS NULL
        AND scope_id IN (
            SELECT g.scope_id
              FROM cortex_auth.scope_grants AS g
             WHERE g.revoked_at IS NULL
               AND (g.can_read OR g.can_write)
        )
    );
CREATE POLICY aliases_migrator_all ON cortex_core.scope_aliases
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

CREATE TABLE cortex_core.memory_records (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    record_id uuid NOT NULL,
    logical_record_id uuid NOT NULL,
    record_type text NOT NULL
        CHECK (record_type IN ('decision', 'lesson', 'knowledge', 'note')),
    revision integer NOT NULL CHECK (revision > 0),
    author_principal_id uuid NOT NULL,
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 65536),
    created_at timestamptz NOT NULL DEFAULT now(),
    supersedes_id uuid,
    PRIMARY KEY (scope_id, record_id),
    UNIQUE (scope_id, logical_record_id, revision),
    UNIQUE (scope_id, record_id, revision),
    FOREIGN KEY (scope_id, supersedes_id)
        REFERENCES cortex_core.memory_records(scope_id, record_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (author_principal_id, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE FUNCTION cortex_core.validate_memory_record_append()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_core, pg_temp
AS $function$
DECLARE
    predecessor cortex_core.memory_records%ROWTYPE;
BEGIN
    IF NEW.revision = 1 THEN
        IF NEW.logical_record_id <> NEW.record_id OR NEW.supersedes_id IS NOT NULL THEN
            RAISE EXCEPTION 'initial memory records must identify themselves and have no predecessor'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.supersedes_id IS NULL THEN
        RAISE EXCEPTION 'memory revisions require a predecessor'
            USING ERRCODE = '23514';
    END IF;

    SELECT *
      INTO predecessor
      FROM cortex_core.memory_records
     WHERE scope_id = NEW.scope_id
       AND record_id = NEW.supersedes_id;

    IF NOT FOUND
       OR predecessor.logical_record_id <> NEW.logical_record_id
       OR predecessor.revision + 1 <> NEW.revision THEN
        RAISE EXCEPTION 'memory revision must immediately follow its same-scope logical predecessor'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END
$function$;

CREATE FUNCTION cortex_core.reject_memory_record_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'memory records are append-only' USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

CREATE TRIGGER memory_records_validate_append
    BEFORE INSERT ON cortex_core.memory_records
    FOR EACH ROW EXECUTE FUNCTION cortex_core.validate_memory_record_append();

CREATE TRIGGER memory_records_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_core.memory_records
    FOR EACH ROW EXECUTE FUNCTION cortex_core.reject_memory_record_mutation();


CREATE TABLE cortex_core.lexical_documents (
    scope_id uuid NOT NULL,
    record_id uuid NOT NULL,
    revision integer NOT NULL,
    source_text text NOT NULL,
    search_document tsvector GENERATED ALWAYS AS (
        to_tsvector('simple'::regconfig, source_text)
    ) STORED,
    PRIMARY KEY (scope_id, record_id),
    FOREIGN KEY (scope_id, record_id, revision)
        REFERENCES cortex_core.memory_records(scope_id, record_id, revision)
        ON DELETE RESTRICT
);

CREATE FUNCTION cortex_core.project_canonical_lexical_document()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_core, pg_temp
AS $function$
DECLARE
    canonical_body text;
BEGIN
    SELECT body
      INTO canonical_body
      FROM cortex_core.memory_records
     WHERE scope_id = NEW.scope_id
       AND record_id = NEW.record_id
       AND revision = NEW.revision;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'lexical projection requires its canonical memory record'
            USING ERRCODE = '23503';
    END IF;

    NEW.source_text := canonical_body;
    RETURN NEW;
END
$function$;

CREATE FUNCTION cortex_core.reject_lexical_document_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'lexical documents are immutable projections' USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

CREATE TRIGGER lexical_documents_project_canonical
    BEFORE INSERT ON cortex_core.lexical_documents
    FOR EACH ROW EXECUTE FUNCTION cortex_core.project_canonical_lexical_document();

CREATE TRIGGER lexical_documents_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_core.lexical_documents
    FOR EACH ROW EXECUTE FUNCTION cortex_core.reject_lexical_document_mutation();


CREATE INDEX lexical_documents_search_gin
    ON cortex_core.lexical_documents USING gin(search_document);
CREATE INDEX memory_records_scope_created
    ON cortex_core.memory_records(scope_id, created_at DESC, record_id);

CREATE TABLE cortex_core.outbox_events (
    event_id uuid PRIMARY KEY,
    scope_id uuid NOT NULL,
    aggregate_id uuid NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz,
    FOREIGN KEY (scope_id, aggregate_id)
        REFERENCES cortex_core.memory_records(scope_id, record_id)
        ON DELETE RESTRICT
);

CREATE INDEX outbox_events_scope_created
    ON cortex_core.outbox_events(scope_id, created_at, event_id);

CREATE TABLE cortex_core.idempotency_receipts (
    principal_id uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    operation text NOT NULL,
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
    request_hash bytea NOT NULL,
    response_body jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal_id, scope_id, operation, idempotency_key)
);

CREATE FUNCTION cortex_core.scope_read_policy(row_scope_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
AS $function$
    SELECT row_scope_id = ANY(
        string_to_array(
            NULLIF(current_setting('cortex.read_scope_ids', true), ''), ','
        )::uuid[]
    )
    AND EXISTS (
        SELECT 1
          FROM cortex_auth.scope_grants AS scope_grant
         WHERE scope_grant.principal_id = NULLIF(
                   current_setting('cortex.principal_id', true), ''
               )::uuid
           AND scope_grant.scope_id = row_scope_id
           AND scope_grant.revoked_at IS NULL
           AND scope_grant.can_read
    )
$function$;

CREATE FUNCTION cortex_core.scope_write_policy(row_scope_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
AS $function$
    SELECT row_scope_id = NULLIF(
        current_setting('cortex.write_scope_id', true), ''
    )::uuid
    AND EXISTS (
        SELECT 1
          FROM cortex_auth.scope_grants AS scope_grant
          JOIN cortex_core.scopes AS scope_registry
            ON scope_registry.scope_id = scope_grant.scope_id
         WHERE scope_grant.principal_id = NULLIF(
                   current_setting('cortex.principal_id', true), ''
               )::uuid
           AND scope_grant.scope_id = row_scope_id
           AND scope_grant.revoked_at IS NULL
           AND scope_grant.can_write
           AND (
               scope_registry.scope_kind <> 'shared' OR scope_grant.can_publish
           )
    )
$function$;

ALTER TABLE cortex_core.memory_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.memory_records FORCE ROW LEVEL SECURITY;
CREATE POLICY memory_records_scope_read ON cortex_core.memory_records
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY memory_records_scope_insert ON cortex_core.memory_records
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND author_principal_id = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY memory_records_migrator_all ON cortex_core.memory_records
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_core.lexical_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.lexical_documents FORCE ROW LEVEL SECURITY;
CREATE POLICY lexical_documents_scope_read ON cortex_core.lexical_documents
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY lexical_documents_scope_insert ON cortex_core.lexical_documents
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY lexical_documents_migrator_all ON cortex_core.lexical_documents
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_core.outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.outbox_events FORCE ROW LEVEL SECURITY;
CREATE POLICY outbox_events_scope_read ON cortex_core.outbox_events
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY outbox_events_scope_insert ON cortex_core.outbox_events
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY outbox_events_migrator_all ON cortex_core.outbox_events
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_core.idempotency_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.idempotency_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY idempotency_receipts_scope_read ON cortex_core.idempotency_receipts
    FOR SELECT TO cortex_v2_app
    USING (
        principal_id = NULLIF(current_setting('cortex.principal_id', true), '')::uuid
        AND cortex_core.scope_read_policy(scope_id)
    );
CREATE POLICY idempotency_receipts_scope_insert ON cortex_core.idempotency_receipts
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        principal_id = NULLIF(current_setting('cortex.principal_id', true), '')::uuid
        AND cortex_core.scope_write_policy(scope_id)
    );
CREATE POLICY idempotency_receipts_migrator_all ON cortex_core.idempotency_receipts
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

GRANT USAGE ON SCHEMA cortex_auth, cortex_core TO cortex_v2_app;
GRANT SELECT ON cortex_auth.scope_grants TO cortex_v2_app;
GRANT SELECT ON cortex_core.scopes, cortex_core.scope_aliases TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_core.memory_records TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_core.lexical_documents TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_core.outbox_events TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_core.idempotency_receipts TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_core.scope_read_policy(uuid) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_core.scope_write_policy(uuid) TO cortex_v2_app;
REVOKE ALL ON cortex_auth.principals, cortex_auth.credentials FROM PUBLIC, cortex_v2_app;
REVOKE ALL ON FUNCTION cortex_auth.authenticate(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_auth.authenticate(bytea) TO cortex_v2_app;
REVOKE ALL ON FUNCTION cortex_core.scope_read_policy(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_core.scope_write_policy(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_core.validate_memory_record_append() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_core.reject_memory_record_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_core.project_canonical_lexical_document() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_core.reject_lexical_document_mutation() FROM PUBLIC;

