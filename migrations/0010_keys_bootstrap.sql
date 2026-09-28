-- A first owner may be installed exactly once per empty Cortex database.
-- The API has EXECUTE on this function, never direct identity-table INSERT.
CREATE UNIQUE INDEX installation_owners_one_active_owner
    ON cortex_auth.installation_owners(installation_id)
    WHERE revoked_at IS NULL;

CREATE TABLE cortex_auth.bootstrap_markers (
    installation_id uuid PRIMARY KEY REFERENCES cortex_auth.installations(installation_id),
    consumed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER bootstrap_markers_no_update_or_delete
    BEFORE UPDATE OR DELETE ON cortex_auth.bootstrap_markers
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.reject_immutable_delete();

ALTER TABLE cortex_auth.bootstrap_markers ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_auth.bootstrap_markers FORCE ROW LEVEL SECURITY;
CREATE POLICY bootstrap_markers_migrator_all ON cortex_auth.bootstrap_markers
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

CREATE FUNCTION cortex_auth.bootstrap_installation(
    p_installation_id uuid,
    p_installation_name text,
    p_owner_name text,
    p_owner_hash bytea,
    p_recovery_hash bytea
)
RETURNS TABLE (
    installation_id uuid,
    owner_principal_id uuid,
    credential_id uuid,
    recovery_generation integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
DECLARE
    owner_id uuid := gen_random_uuid();
    actor_id uuid := gen_random_uuid();
    issued_credential_id uuid := gen_random_uuid();
BEGIN
    -- Lock the entire empty-install transition, including concurrent requests
    -- that present different installation UUIDs against the same database.
    PERFORM pg_catalog.pg_advisory_xact_lock(2026, 53);
    IF p_installation_id IS NULL
       OR EXISTS (SELECT 1 FROM cortex_auth.installations)
       OR EXISTS (SELECT 1 FROM cortex_auth.installation_owners)
       OR EXISTS (SELECT 1 FROM cortex_auth.bootstrap_markers) THEN
        RAISE EXCEPTION 'installation bootstrap already consumed'
            USING ERRCODE = '42501';
    END IF;
    IF p_installation_name IS NULL OR length(p_installation_name) NOT BETWEEN 1 AND 128
       OR p_owner_name IS NULL OR length(p_owner_name) NOT BETWEEN 1 AND 128
       OR p_owner_name = 'console'
       OR octet_length(p_owner_hash) <> 32
       OR octet_length(p_recovery_hash) <> 32
       OR p_owner_hash = p_recovery_hash THEN
        RAISE EXCEPTION 'invalid first-install identity or credential digest'
            USING ERRCODE = '23514';
    END IF;

    INSERT INTO cortex_auth.installations(installation_id, display_name)
    VALUES (p_installation_id, p_installation_name);
    INSERT INTO cortex_auth.principals(principal_id, installation_id, principal_name, status)
    VALUES (owner_id, p_installation_id, p_owner_name, 'active');
    INSERT INTO cortex_auth.actors(actor_id, installation_id, actor_kind, display_name)
    VALUES (actor_id, p_installation_id, 'human', p_owner_name);
    INSERT INTO cortex_auth.actor_bindings(actor_id, principal_id, bound_by_principal_id)
    VALUES (actor_id, owner_id, owner_id);
    INSERT INTO cortex_auth.installation_owners(installation_id, principal_id)
    VALUES (p_installation_id, owner_id);
    INSERT INTO cortex_auth.installation_recovery(
        installation_id, recovery_token_hash, generation
    ) VALUES (p_installation_id, p_recovery_hash, 1);
    INSERT INTO cortex_auth.credentials(
        credential_id, principal_id, token_hash, generation, expires_at
    ) VALUES (issued_credential_id, owner_id, p_owner_hash, 1, NULL);
    INSERT INTO cortex_auth.bootstrap_markers(installation_id)
    VALUES (p_installation_id);
    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type, target_principal_id, detail
    ) VALUES (
        p_installation_id, NULL, 'bootstrap_installation', owner_id,
        jsonb_build_object('credential_id', issued_credential_id, 'recovery_generation', 1)
    );
    RETURN QUERY SELECT p_installation_id, owner_id, issued_credential_id, 1;
END
$function$;

REVOKE ALL ON FUNCTION cortex_auth.bootstrap_installation(uuid, text, text, bytea, bytea)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_auth.bootstrap_installation(uuid, text, text, bytea, bytea)
    TO cortex_v2_app;
