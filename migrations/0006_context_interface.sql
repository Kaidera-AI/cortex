DO $bootstrap$
BEGIN
    IF current_user <> 'cortex_v2_migrator' THEN
        RAISE EXCEPTION 'context/interface migration must run as cortex_v2_migrator';
    END IF;
END
$bootstrap$;

-- ---------------------------------------------------------------------------
-- W2 context & worker interface plane: persona/rule/skill revisions with
-- provenance, budgeted context preparations, deterministic harness mirrors,
-- and the connector ingestion ledger. Depends on 0001-0005 only; feed and
-- verification (0007) are referenced through the operation registry, never
-- at the schema level.
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS cortex_context AUTHORIZATION cortex_v2_migrator;

CREATE TABLE cortex_context.persona_revisions (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    persona_id uuid NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    template_version text NOT NULL CHECK (length(template_version) BETWEEN 1 AND 64),
    payload jsonb NOT NULL,
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 65536),
    created_by_principal uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, persona_id, revision),
    FOREIGN KEY (created_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE TABLE cortex_context.rule_revisions (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    rule_id uuid NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    slug text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9._-]{0,95}$'),
    obligation text NOT NULL CHECK (obligation IN ('mandatory', 'optional')),
    state text NOT NULL CHECK (state IN ('active', 'retired')),
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 65536),
    created_by_principal uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, rule_id, revision),
    FOREIGN KEY (created_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE INDEX rule_revisions_scope_slug
    ON cortex_context.rule_revisions(scope_id, slug, revision DESC);

CREATE TABLE cortex_context.skill_revisions (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    skill_id uuid NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    slug text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9._-]{0,95}$'),
    when_to_use text NOT NULL CHECK (length(when_to_use) BETWEEN 1 AND 2048),
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 65536),
    created_by_principal uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, skill_id, revision),
    FOREIGN KEY (created_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE INDEX skill_revisions_scope_slug
    ON cortex_context.skill_revisions(scope_id, slug, revision DESC);

CREATE FUNCTION cortex_context.validate_persona_revision_append()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_context, pg_temp
AS $function$
BEGIN
    IF NEW.revision = 1 THEN
        RETURN NEW;
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM cortex_context.persona_revisions
         WHERE scope_id = NEW.scope_id
           AND persona_id = NEW.persona_id
           AND revision = NEW.revision - 1
    ) THEN
        RAISE EXCEPTION 'persona revision must immediately follow its predecessor'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION cortex_context.validate_rule_revision_append()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_context, pg_temp
AS $function$
DECLARE
    predecessor cortex_context.rule_revisions%ROWTYPE;
BEGIN
    IF NEW.revision = 1 THEN
        RETURN NEW;
    END IF;
    SELECT *
      INTO predecessor
      FROM cortex_context.rule_revisions
     WHERE scope_id = NEW.scope_id
       AND rule_id = NEW.rule_id
       AND revision = NEW.revision - 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rule revision must immediately follow its predecessor'
            USING ERRCODE = '23514';
    END IF;
    IF predecessor.slug <> NEW.slug THEN
        RAISE EXCEPTION 'rule slug identity is immutable across revisions'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION cortex_context.validate_skill_revision_append()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_context, pg_temp
AS $function$
DECLARE
    predecessor cortex_context.skill_revisions%ROWTYPE;
BEGIN
    IF NEW.revision = 1 THEN
        RETURN NEW;
    END IF;
    SELECT *
      INTO predecessor
      FROM cortex_context.skill_revisions
     WHERE scope_id = NEW.scope_id
       AND skill_id = NEW.skill_id
       AND revision = NEW.revision - 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'skill revision must immediately follow its predecessor'
            USING ERRCODE = '23514';
    END IF;
    IF predecessor.slug <> NEW.slug THEN
        RAISE EXCEPTION 'skill slug identity is immutable across revisions'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION cortex_context.reject_append_only_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'this relation is append-only' USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

CREATE TRIGGER persona_revisions_validate_append
    BEFORE INSERT ON cortex_context.persona_revisions
    FOR EACH ROW EXECUTE FUNCTION cortex_context.validate_persona_revision_append();
CREATE TRIGGER persona_revisions_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_context.persona_revisions
    FOR EACH ROW EXECUTE FUNCTION cortex_context.reject_append_only_mutation();

CREATE TRIGGER rule_revisions_validate_append
    BEFORE INSERT ON cortex_context.rule_revisions
    FOR EACH ROW EXECUTE FUNCTION cortex_context.validate_rule_revision_append();
CREATE TRIGGER rule_revisions_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_context.rule_revisions
    FOR EACH ROW EXECUTE FUNCTION cortex_context.reject_append_only_mutation();

CREATE TRIGGER skill_revisions_validate_append
    BEFORE INSERT ON cortex_context.skill_revisions
    FOR EACH ROW EXECUTE FUNCTION cortex_context.validate_skill_revision_append();
CREATE TRIGGER skill_revisions_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_context.skill_revisions
    FOR EACH ROW EXECUTE FUNCTION cortex_context.reject_append_only_mutation();

CREATE TABLE cortex_context.skill_binding_sets (
    scope_id uuid PRIMARY KEY REFERENCES cortex_core.scopes(scope_id),
    revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cortex_context.skill_bindings (
    scope_id uuid NOT NULL,
    skill_id uuid NOT NULL,
    bound_revision integer NOT NULL CHECK (bound_revision > 0),
    precedence integer NOT NULL CHECK (precedence BETWEEN 1 AND 1000),
    bound_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, skill_id),
    UNIQUE (scope_id, precedence),
    FOREIGN KEY (scope_id, skill_id, bound_revision)
        REFERENCES cortex_context.skill_revisions(scope_id, skill_id, revision)
        ON DELETE RESTRICT
);

CREATE FUNCTION cortex_context.validate_binding_set_update()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.scope_id <> OLD.scope_id THEN
        RAISE EXCEPTION 'binding set scope is immutable'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.revision <> OLD.revision + 1 THEN
        RAISE EXCEPTION 'binding set revision must advance by exactly one'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION cortex_context.reject_binding_set_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'binding sets are never deleted' USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

CREATE TRIGGER skill_binding_sets_validate_update
    BEFORE UPDATE ON cortex_context.skill_binding_sets
    FOR EACH ROW EXECUTE FUNCTION cortex_context.validate_binding_set_update();
CREATE TRIGGER skill_binding_sets_reject_delete
    BEFORE DELETE ON cortex_context.skill_binding_sets
    FOR EACH ROW EXECUTE FUNCTION cortex_context.reject_binding_set_delete();

CREATE TABLE cortex_context.context_preparations (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    preparation_id uuid NOT NULL,
    prepared_by_principal uuid NOT NULL,
    intent text NOT NULL CHECK (intent IN (
        'code_change', 'debug_symbol', 'architecture', 'research',
        'record_or_return', 'prose_edit'
    )),
    budget_bytes integer NOT NULL CHECK (budget_bytes > 0),
    mandatory_bytes integer NOT NULL CHECK (mandatory_bytes >= 0),
    composed_bytes integer NOT NULL CHECK (composed_bytes >= 0),
    policy_revision integer NOT NULL CHECK (policy_revision >= 0),
    sections jsonb NOT NULL,
    provenance jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, preparation_id),
    FOREIGN KEY (prepared_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE INDEX context_preparations_scope_created
    ON cortex_context.context_preparations(scope_id, created_at DESC, preparation_id);

CREATE TRIGGER context_preparations_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_context.context_preparations
    FOR EACH ROW EXECUTE FUNCTION cortex_context.reject_append_only_mutation();

CREATE TABLE cortex_context.harness_mirrors (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    mirror_id uuid NOT NULL,
    label text NOT NULL CHECK (length(label) BETWEEN 1 AND 96),
    active_generation integer CHECK (active_generation IS NULL OR active_generation > 0),
    created_by_principal uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, mirror_id),
    UNIQUE (scope_id, label),
    FOREIGN KEY (created_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE FUNCTION cortex_context.validate_mirror_update()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.scope_id <> OLD.scope_id
       OR NEW.mirror_id <> OLD.mirror_id
       OR NEW.label <> OLD.label
       OR NEW.created_by_principal <> OLD.created_by_principal
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'only the active generation pointer may move'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.active_generation IS NOT NULL
       AND OLD.active_generation IS NOT NULL
       AND NEW.active_generation <> OLD.active_generation + 1 THEN
        RAISE EXCEPTION 'the active generation advances by exactly one'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION cortex_context.reject_mirror_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'mirrors are never deleted; roll them back'
        USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

CREATE TRIGGER harness_mirrors_validate_update
    BEFORE UPDATE ON cortex_context.harness_mirrors
    FOR EACH ROW EXECUTE FUNCTION cortex_context.validate_mirror_update();
CREATE TRIGGER harness_mirrors_reject_delete
    BEFORE DELETE ON cortex_context.harness_mirrors
    FOR EACH ROW EXECUTE FUNCTION cortex_context.reject_mirror_delete();

CREATE TABLE cortex_context.harness_generations (
    scope_id uuid NOT NULL,
    mirror_id uuid NOT NULL,
    generation integer NOT NULL CHECK (generation > 0),
    generator_version text NOT NULL CHECK (length(generator_version) BETWEEN 1 AND 64),
    input_provenance jsonb NOT NULL,
    manifest_sha256 bytea NOT NULL CHECK (octet_length(manifest_sha256) = 32),
    file_count integer NOT NULL CHECK (file_count >= 0),
    created_by_principal uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, mirror_id, generation),
    FOREIGN KEY (scope_id, mirror_id)
        REFERENCES cortex_context.harness_mirrors(scope_id, mirror_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (created_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE FUNCTION cortex_context.validate_generation_append()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_context, pg_temp
AS $function$
BEGIN
    IF NEW.generation = 1 THEN
        RETURN NEW;
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM cortex_context.harness_generations
         WHERE scope_id = NEW.scope_id
           AND mirror_id = NEW.mirror_id
           AND generation = NEW.generation - 1
    ) THEN
        RAISE EXCEPTION 'mirror generation must immediately follow its predecessor'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER harness_generations_validate_append
    BEFORE INSERT ON cortex_context.harness_generations
    FOR EACH ROW EXECUTE FUNCTION cortex_context.validate_generation_append();
CREATE TRIGGER harness_generations_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_context.harness_generations
    FOR EACH ROW EXECUTE FUNCTION cortex_context.reject_append_only_mutation();

CREATE TABLE cortex_context.harness_generation_files (
    scope_id uuid NOT NULL,
    mirror_id uuid NOT NULL,
    generation integer NOT NULL,
    path text NOT NULL CHECK (
        length(path) BETWEEN 1 AND 512
        AND position('..' in path) = 0
        AND left(path, 1) <> '/'
        AND position('//' in path) = 0
    ),
    body text NOT NULL CHECK (length(body) <= 65536),
    content_sha256 bytea NOT NULL CHECK (octet_length(content_sha256) = 32),
    PRIMARY KEY (scope_id, mirror_id, generation, path),
    FOREIGN KEY (scope_id, mirror_id, generation)
        REFERENCES cortex_context.harness_generations(scope_id, mirror_id, generation)
        ON DELETE RESTRICT
);

CREATE TRIGGER harness_generation_files_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_context.harness_generation_files
    FOR EACH ROW EXECUTE FUNCTION cortex_context.reject_append_only_mutation();

CREATE TABLE cortex_context.ingest_runs (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    run_id uuid NOT NULL,
    connector_namespace text NOT NULL CHECK (length(connector_namespace) BETWEEN 1 AND 64),
    source_key text NOT NULL CHECK (length(source_key) BETWEEN 1 AND 256),
    ingest_kind text NOT NULL CHECK (ingest_kind IN (
        'session', 'message', 'local_state', 'diary', 'save_chat'
    )),
    generation integer NOT NULL CHECK (generation > 0),
    item_count integer NOT NULL CHECK (item_count >= 0),
    parse_warnings jsonb NOT NULL,
    quarantine_payload jsonb,
    quarantine_reason jsonb,
    replaced_run_id uuid,
    status text NOT NULL CHECK (status IN ('committed', 'quarantined')),
    created_by_principal uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, run_id),
    UNIQUE (scope_id, connector_namespace, source_key, generation),
    CHECK (status <> 'quarantined' OR quarantine_payload IS NOT NULL),
    CHECK (status <> 'committed' OR quarantine_payload IS NULL),
    FOREIGN KEY (scope_id, replaced_run_id)
        REFERENCES cortex_context.ingest_runs(scope_id, run_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (created_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE INDEX ingest_runs_scope_source
    ON cortex_context.ingest_runs(scope_id, connector_namespace, source_key, generation DESC);

CREATE TRIGGER ingest_runs_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_context.ingest_runs
    FOR EACH ROW EXECUTE FUNCTION cortex_context.reject_append_only_mutation();

CREATE TABLE cortex_context.ingest_run_contents (
    scope_id uuid NOT NULL,
    run_id uuid NOT NULL,
    content_id uuid NOT NULL,
    item_role text NOT NULL CHECK (item_role IN (
        'header', 'message', 'entry', 'state', 'chat'
    )),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (scope_id, run_id, content_id),
    UNIQUE (scope_id, run_id, ordinal),
    FOREIGN KEY (scope_id, run_id)
        REFERENCES cortex_context.ingest_runs(scope_id, run_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (scope_id, content_id)
        REFERENCES cortex_core.content_items(scope_id, content_id)
        ON DELETE RESTRICT
);

CREATE TRIGGER ingest_run_contents_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_context.ingest_run_contents
    FOR EACH ROW EXECUTE FUNCTION cortex_context.reject_append_only_mutation();

-- ---------------------------------------------------------------------------
-- Row level security: same scope-policy helper functions as 0001/0002, with
-- the transaction-local GUCs set by the authenticated application context.
-- ---------------------------------------------------------------------------

ALTER TABLE cortex_context.persona_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_context.persona_revisions FORCE ROW LEVEL SECURITY;
CREATE POLICY persona_revisions_scope_read ON cortex_context.persona_revisions
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY persona_revisions_scope_insert ON cortex_context.persona_revisions
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND created_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY persona_revisions_migrator_all ON cortex_context.persona_revisions
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_context.rule_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_context.rule_revisions FORCE ROW LEVEL SECURITY;
CREATE POLICY rule_revisions_scope_read ON cortex_context.rule_revisions
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY rule_revisions_scope_insert ON cortex_context.rule_revisions
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND created_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY rule_revisions_migrator_all ON cortex_context.rule_revisions
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_context.skill_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_context.skill_revisions FORCE ROW LEVEL SECURITY;
CREATE POLICY skill_revisions_scope_read ON cortex_context.skill_revisions
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY skill_revisions_scope_insert ON cortex_context.skill_revisions
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND created_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY skill_revisions_migrator_all ON cortex_context.skill_revisions
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_context.skill_binding_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_context.skill_binding_sets FORCE ROW LEVEL SECURITY;
CREATE POLICY skill_binding_sets_scope_read ON cortex_context.skill_binding_sets
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY skill_binding_sets_scope_insert ON cortex_context.skill_binding_sets
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY skill_binding_sets_scope_update ON cortex_context.skill_binding_sets
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY skill_binding_sets_migrator_all ON cortex_context.skill_binding_sets
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_context.skill_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_context.skill_bindings FORCE ROW LEVEL SECURITY;
CREATE POLICY skill_bindings_scope_read ON cortex_context.skill_bindings
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY skill_bindings_scope_insert ON cortex_context.skill_bindings
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY skill_bindings_scope_update ON cortex_context.skill_bindings
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY skill_bindings_scope_delete ON cortex_context.skill_bindings
    FOR DELETE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id));
CREATE POLICY skill_bindings_migrator_all ON cortex_context.skill_bindings
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_context.context_preparations ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_context.context_preparations FORCE ROW LEVEL SECURITY;
CREATE POLICY context_preparations_scope_read ON cortex_context.context_preparations
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY context_preparations_scope_insert ON cortex_context.context_preparations
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND prepared_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY context_preparations_migrator_all ON cortex_context.context_preparations
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_context.harness_mirrors ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_context.harness_mirrors FORCE ROW LEVEL SECURITY;
CREATE POLICY harness_mirrors_scope_read ON cortex_context.harness_mirrors
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY harness_mirrors_scope_insert ON cortex_context.harness_mirrors
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND created_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY harness_mirrors_scope_update ON cortex_context.harness_mirrors
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY harness_mirrors_migrator_all ON cortex_context.harness_mirrors
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_context.harness_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_context.harness_generations FORCE ROW LEVEL SECURITY;
CREATE POLICY harness_generations_scope_read ON cortex_context.harness_generations
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY harness_generations_scope_insert ON cortex_context.harness_generations
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND created_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY harness_generations_migrator_all ON cortex_context.harness_generations
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_context.harness_generation_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_context.harness_generation_files FORCE ROW LEVEL SECURITY;
CREATE POLICY harness_generation_files_scope_read ON cortex_context.harness_generation_files
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY harness_generation_files_scope_insert ON cortex_context.harness_generation_files
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY harness_generation_files_migrator_all ON cortex_context.harness_generation_files
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_context.ingest_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_context.ingest_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY ingest_runs_scope_read ON cortex_context.ingest_runs
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY ingest_runs_scope_insert ON cortex_context.ingest_runs
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND created_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY ingest_runs_migrator_all ON cortex_context.ingest_runs
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_context.ingest_run_contents ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_context.ingest_run_contents FORCE ROW LEVEL SECURITY;
CREATE POLICY ingest_run_contents_scope_read ON cortex_context.ingest_run_contents
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY ingest_run_contents_scope_insert ON cortex_context.ingest_run_contents
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY ingest_run_contents_migrator_all ON cortex_context.ingest_run_contents
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

-- Grants: the app role receives the least DML required; nothing is granted
-- to PUBLIC and there are no default privileges.

GRANT USAGE ON SCHEMA cortex_context TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_context.persona_revisions TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_context.rule_revisions TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_context.skill_revisions TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_context.skill_binding_sets TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON cortex_context.skill_bindings TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_context.context_preparations TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_context.harness_mirrors TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_context.harness_generations TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_context.harness_generation_files TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_context.ingest_runs TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_context.ingest_run_contents TO cortex_v2_app;

REVOKE ALL ON FUNCTION cortex_context.validate_persona_revision_append() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_context.validate_rule_revision_append() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_context.validate_skill_revision_append() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_context.reject_append_only_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_context.validate_binding_set_update() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_context.reject_binding_set_delete() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_context.validate_mirror_update() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_context.reject_mirror_delete() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_context.validate_generation_append() FROM PUBLIC;
