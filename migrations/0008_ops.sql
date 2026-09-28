DO $bootstrap$
BEGIN
    IF current_user <> 'cortex_v2_migrator' THEN
        RAISE EXCEPTION 'ops migration must run as cortex_v2_migrator';
    END IF;
END
$bootstrap$;

-- ---------------------------------------------------------------------------
-- W4 operations plane: migration-ledger visibility, retention policy/ledger,
-- typed repair ledger and backup manifests. Doctor/metrics/release status are
-- composed from catalogs plus these relations; no arbitrary-SQL repair path
-- exists, and no relation here stores content bytes.
-- ---------------------------------------------------------------------------

ALTER TABLE cortex_core.schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.schema_migrations FORCE ROW LEVEL SECURITY;
CREATE POLICY schema_migrations_authenticated_read ON cortex_core.schema_migrations
    FOR SELECT TO cortex_v2_app
    USING (NULLIF(current_setting('cortex.principal_id', true), '') IS NOT NULL);
CREATE POLICY schema_migrations_migrator_all ON cortex_core.schema_migrations
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);
GRANT SELECT ON cortex_core.schema_migrations TO cortex_v2_app;

CREATE FUNCTION cortex_core.reject_ops_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

CREATE TABLE cortex_core.retention_policies (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    content_class text NOT NULL CHECK (content_class IN (
        '*', 'decision', 'lesson', 'knowledge', 'progress', 'diary',
        'message', 'session', 'artifact', 'work_product'
    )),
    revision integer NOT NULL CHECK (revision > 0),
    min_age_days integer NOT NULL CHECK (min_age_days >= 0),
    action text NOT NULL CHECK (action IN ('archive', 'retain')),
    enacted_by uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    enacted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, content_class, revision)
);

CREATE TABLE cortex_core.retention_ledger (
    entry_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_id uuid NOT NULL,
    content_id uuid NOT NULL,
    content_class text NOT NULL,
    content_revision integer NOT NULL,
    content_hash bytea NOT NULL CHECK (octet_length(content_hash) = 32),
    action text NOT NULL CHECK (action IN ('archive', 'restore')),
    policy_revision integer NOT NULL,
    disposition text NOT NULL CHECK (disposition IN ('archived_in_place', 'restored')),
    storage_tier text NOT NULL DEFAULT 'canonical_in_place',
    recorded_by uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (scope_id, content_id)
        REFERENCES cortex_core.content_items(scope_id, content_id)
        ON DELETE RESTRICT
);

CREATE TABLE cortex_core.repairs (
    repair_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    repair_kind text NOT NULL CHECK (repair_kind IN (
        'retention_restore'
    )),
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    target_reference text NOT NULL CHECK (length(target_reference) BETWEEN 1 AND 256),
    outcome text NOT NULL
        CHECK (outcome IN ('applied', 'precondition_failed', 'not_found')),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    requested_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cortex_core.backup_manifests (
    manifest_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    installation_id uuid NOT NULL
        REFERENCES cortex_auth.installations(installation_id),
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    schema_snapshot jsonb NOT NULL,
    scope_coverage uuid[] NOT NULL CHECK (array_length(scope_coverage, 1) > 0),
    table_digests jsonb NOT NULL
);

CREATE INDEX retention_ledger_scope_recorded
    ON cortex_core.retention_ledger(scope_id, recorded_at DESC, entry_id);
CREATE INDEX repairs_scope_created
    ON cortex_core.repairs(scope_id, created_at DESC, repair_id);

CREATE FUNCTION cortex_core.enact_retention_policy(
    p_caller_principal_id uuid,
    p_scope_id uuid,
    p_content_class text,
    p_min_age_days integer,
    p_action text,
    p_expected_revision integer
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, pg_temp
AS $function$
DECLARE
    caller_installation uuid;
    current_revision integer;
    next_revision integer;
BEGIN
    caller_installation := cortex_auth.caller_installation(p_caller_principal_id);
    PERFORM cortex_auth.require_installation_owner(
        p_caller_principal_id, caller_installation
    );
    IF NOT EXISTS (
        SELECT 1 FROM cortex_core.scopes WHERE scope_id = p_scope_id AND is_active
    ) THEN
        RAISE EXCEPTION 'scope is unknown or inactive' USING ERRCODE = 'P0002';
    END IF;
    IF p_content_class NOT IN (
        '*', 'decision', 'lesson', 'knowledge', 'progress', 'diary',
        'message', 'session', 'artifact', 'work_product'
    ) THEN
        RAISE EXCEPTION 'retention content class is invalid' USING ERRCODE = '23514';
    END IF;
    IF p_min_age_days IS NULL OR p_min_age_days < 0 THEN
        RAISE EXCEPTION 'retention age must be non-negative' USING ERRCODE = '23514';
    END IF;
    IF p_action NOT IN ('archive', 'retain') THEN
        RAISE EXCEPTION 'retention action is invalid' USING ERRCODE = '23514';
    END IF;

    SELECT COALESCE(max(revision), 0)
      INTO current_revision
      FROM cortex_core.retention_policies
     WHERE scope_id = p_scope_id
       AND content_class = p_content_class;
    IF p_expected_revision <> current_revision THEN
        RAISE EXCEPTION 'retention policy revision moved; recheck and retry'
            USING ERRCODE = '40001';
    END IF;
    next_revision := current_revision + 1;

    INSERT INTO cortex_core.retention_policies(
        scope_id, content_class, revision, min_age_days, action, enacted_by
    )
    VALUES (
        p_scope_id, p_content_class, next_revision, p_min_age_days, p_action,
        p_caller_principal_id
    );

    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type, target_scope_id, detail
    )
    VALUES (
        caller_installation, p_caller_principal_id, 'enact_retention_policy',
        p_scope_id,
        jsonb_build_object(
            'content_class', p_content_class,
            'revision', next_revision,
            'min_age_days', p_min_age_days,
            'action', p_action
        )
    );
    RETURN next_revision;
END
$function$;

CREATE TRIGGER retention_policies_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_core.retention_policies
    FOR EACH ROW EXECUTE FUNCTION cortex_core.reject_ops_mutation();
CREATE TRIGGER retention_ledger_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_core.retention_ledger
    FOR EACH ROW EXECUTE FUNCTION cortex_core.reject_ops_mutation();
CREATE TRIGGER repairs_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_core.repairs
    FOR EACH ROW EXECUTE FUNCTION cortex_core.reject_ops_mutation();
CREATE TRIGGER backup_manifests_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_core.backup_manifests
    FOR EACH ROW EXECUTE FUNCTION cortex_core.reject_ops_mutation();

ALTER TABLE cortex_core.retention_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.retention_policies FORCE ROW LEVEL SECURITY;
CREATE POLICY retention_policies_scope_read ON cortex_core.retention_policies
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY retention_policies_migrator_all ON cortex_core.retention_policies
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_core.retention_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.retention_ledger FORCE ROW LEVEL SECURITY;
CREATE POLICY retention_ledger_scope_read ON cortex_core.retention_ledger
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY retention_ledger_scope_insert ON cortex_core.retention_ledger
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND recorded_by = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY retention_ledger_migrator_all ON cortex_core.retention_ledger
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_core.repairs ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.repairs FORCE ROW LEVEL SECURITY;
CREATE POLICY repairs_scope_read ON cortex_core.repairs
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY repairs_scope_insert ON cortex_core.repairs
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND requested_by = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY repairs_migrator_all ON cortex_core.repairs
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_core.backup_manifests ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.backup_manifests FORCE ROW LEVEL SECURITY;
CREATE POLICY backup_manifests_installation_read ON cortex_core.backup_manifests
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.caller_installation_matches(installation_id));
CREATE POLICY backup_manifests_installation_insert ON cortex_core.backup_manifests
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.caller_installation_matches(installation_id)
        AND created_by = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY backup_manifests_migrator_all ON cortex_core.backup_manifests
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

GRANT SELECT ON cortex_core.retention_policies TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_core.retention_ledger TO cortex_v2_app;

-- Outbox delivery/redelivery is owned by 0007_feed_verification.sql: it grants
-- UPDATE on both outbox tables, creates the scope-write UPDATE policies and
-- enforces exactly-once delivery marking (cortex_feed.validate_outbox_delivery_update).
-- Ops therefore provides no outbox-redelivery repair; redelivery flows through
-- the feed dispatcher/cursor operations instead.
GRANT SELECT, INSERT ON cortex_core.repairs TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_core.backup_manifests TO cortex_v2_app;

REVOKE ALL ON FUNCTION cortex_core.reject_ops_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION
    cortex_core.enact_retention_policy(uuid, uuid, text, integer, text, integer)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    cortex_core.enact_retention_policy(uuid, uuid, text, integer, text, integer)
    TO cortex_v2_app;
