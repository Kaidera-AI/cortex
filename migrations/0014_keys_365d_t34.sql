-- T34: only credentials issued after this migration receive 365 days.
-- Keep every existing credential's true expiry (including 180-day and NULL).
-- Redefine only the four 0013 issuers with their existing public signatures,
-- guards, search_path, grants and audit shape. No row backfill or new authority.
-- Rotation overlap is S10; historical-NULL grace is S14, not this migration.
-- Recovery calls remain restricted to approved synthetic disposable tests or
-- a separately authorised private recovery operation. No live recovery here.
CREATE OR REPLACE FUNCTION cortex_auth.bootstrap_installation(
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
    recovery_generation integer,
    expires_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
DECLARE
    owner_id uuid := gen_random_uuid();
    actor_id uuid := gen_random_uuid();
    issued_credential_id uuid := gen_random_uuid();
    issued_expires_at timestamptz;
BEGIN
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
    INSERT INTO cortex_auth.credentials AS issued_credential(
        credential_id, principal_id, token_hash, generation, expires_at
    ) VALUES (
        issued_credential_id, owner_id, p_owner_hash, 1, now() + interval '365 days'
    ) RETURNING issued_credential.expires_at INTO issued_expires_at;
    INSERT INTO cortex_auth.bootstrap_markers(installation_id)
    VALUES (p_installation_id);
    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type, target_principal_id, detail
    ) VALUES (
        p_installation_id, NULL, 'bootstrap_installation', owner_id,
        jsonb_build_object(
            'credential_id', issued_credential_id,
            'recovery_generation', 1,
            'expires_at', issued_expires_at
        )
    );
    RETURN QUERY SELECT p_installation_id, owner_id, issued_credential_id, 1, issued_expires_at;
END
$function$;
REVOKE ALL ON FUNCTION cortex_auth.bootstrap_installation(uuid, text, text, bytea, bytea)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_auth.bootstrap_installation(uuid, text, text, bytea, bytea)
    TO cortex_v2_app;

CREATE OR REPLACE FUNCTION cortex_auth.enroll_principal(
    p_caller_principal_id uuid,
    p_principal_name text,
    p_actor_kind text,
    p_token_hash bytea
)
RETURNS TABLE (
    principal_id uuid,
    actor_id uuid,
    credential_id uuid,
    generation integer,
    expires_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
DECLARE
    caller_installation uuid;
    new_principal_id uuid := gen_random_uuid();
    new_actor_id uuid := gen_random_uuid();
    new_credential_id uuid := gen_random_uuid();
    issued_expires_at timestamptz;
BEGIN
    caller_installation := cortex_auth.caller_installation(p_caller_principal_id);
    PERFORM cortex_auth.require_installation_owner(
        p_caller_principal_id, caller_installation
    );
    IF length(p_principal_name) NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'principal name length is invalid' USING ERRCODE = '23514';
    END IF;
    IF p_actor_kind NOT IN ('human', 'agent', 'service') THEN
        RAISE EXCEPTION 'actor kind is invalid' USING ERRCODE = '23514';
    END IF;
    IF octet_length(p_token_hash) <> 32 THEN
        RAISE EXCEPTION 'credential hash must be 32 bytes' USING ERRCODE = '23514';
    END IF;

    INSERT INTO cortex_auth.principals(
        principal_id, installation_id, principal_name, status
    )
    VALUES (new_principal_id, caller_installation, p_principal_name, 'active');
    INSERT INTO cortex_auth.actors(
        actor_id, installation_id, actor_kind, display_name
    )
    VALUES (new_actor_id, caller_installation, p_actor_kind, p_principal_name);
    INSERT INTO cortex_auth.actor_bindings(
        actor_id, principal_id, bound_by_principal_id
    )
    VALUES (new_actor_id, new_principal_id, p_caller_principal_id);
    INSERT INTO cortex_auth.credentials AS issued_credential(
        credential_id, principal_id, token_hash, generation, expires_at
    )
    VALUES (
        new_credential_id, new_principal_id, p_token_hash, 1,
        now() + interval '365 days'
    )
    RETURNING issued_credential.expires_at INTO issued_expires_at;
    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type, target_principal_id, detail
    ) VALUES (
        caller_installation, p_caller_principal_id, 'enroll_principal', new_principal_id,
        jsonb_build_object(
            'principal_name', p_principal_name,
            'actor_kind', p_actor_kind,
            'credential_id', new_credential_id,
            'generation', 1,
            'expires_at', issued_expires_at
        )
    );
    RETURN QUERY SELECT new_principal_id, new_actor_id, new_credential_id, 1, issued_expires_at;
END
$function$;
REVOKE ALL ON FUNCTION cortex_auth.enroll_principal(uuid, text, text, bytea)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_auth.enroll_principal(uuid, text, text, bytea)
    TO cortex_v2_app;

-- Retain the existing rotation transition while binding only the replacement
-- credential's lifetime; C3 overlap policy remains separately gated.
CREATE OR REPLACE FUNCTION cortex_auth.rotate_credential(
    p_caller_principal_id uuid,
    p_principal_id uuid,
    p_new_token_hash bytea
)
RETURNS TABLE (
    credential_id uuid,
    generation integer,
    revoked_count integer,
    expires_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
DECLARE
    caller_installation uuid;
    target_installation uuid;
    target_status text;
    new_credential_id uuid := gen_random_uuid();
    next_generation integer;
    revoked integer;
    issued_expires_at timestamptz;
BEGIN
    caller_installation := cortex_auth.caller_installation(p_caller_principal_id);
    IF caller_installation IS NULL THEN
        RAISE EXCEPTION 'operation requires an authenticated principal'
            USING ERRCODE = '42501';
    END IF;
    SELECT installation_id, status
      INTO target_installation, target_status
      FROM cortex_auth.principals
     WHERE principal_id = p_principal_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'principal is unknown' USING ERRCODE = 'P0002';
    END IF;
    IF target_installation <> caller_installation THEN
        RAISE EXCEPTION 'principal belongs to another installation'
            USING ERRCODE = '42501';
    END IF;
    IF p_caller_principal_id <> p_principal_id THEN
        PERFORM cortex_auth.require_installation_owner(
            p_caller_principal_id, caller_installation
        );
    END IF;
    IF target_status <> 'active' THEN
        RAISE EXCEPTION 'principal is revoked; recover or re-enroll instead'
            USING ERRCODE = '42501';
    END IF;
    IF octet_length(p_new_token_hash) <> 32 THEN
        RAISE EXCEPTION 'credential hash must be 32 bytes' USING ERRCODE = '23514';
    END IF;

    UPDATE cortex_auth.credentials
       SET revoked_at = now()
     WHERE principal_id = p_principal_id
       AND revoked_at IS NULL;
    GET DIAGNOSTICS revoked = ROW_COUNT;

    SELECT COALESCE(max(active_credential.generation), 0) + 1
      INTO next_generation
      FROM cortex_auth.credentials AS active_credential
     WHERE active_credential.principal_id = p_principal_id;

    INSERT INTO cortex_auth.credentials AS issued_credential(
        credential_id, principal_id, token_hash, generation, expires_at
    )
    VALUES (
        new_credential_id, p_principal_id, p_new_token_hash, next_generation,
        now() + interval '365 days'
    )
    RETURNING issued_credential.expires_at INTO issued_expires_at;

    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type, target_principal_id, detail
    )
    VALUES (
        caller_installation, p_caller_principal_id, 'rotate_credential', p_principal_id,
        jsonb_build_object(
            'credential_id', new_credential_id,
            'generation', next_generation,
            'revoked_count', revoked,
            'expires_at', issued_expires_at
        )
    );

    RETURN QUERY SELECT new_credential_id, next_generation, revoked, issued_expires_at;
END
$function$;
REVOKE ALL ON FUNCTION cortex_auth.rotate_credential(uuid, uuid, bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_auth.rotate_credential(uuid, uuid, bytea)
    TO cortex_v2_app;

CREATE OR REPLACE FUNCTION cortex_auth.recover_owner(
    p_recovery_token_hash bytea,
    p_new_token_hash bytea,
    p_new_recovery_token_hash bytea
)
RETURNS TABLE (
    installation_id uuid,
    principal_id uuid,
    credential_id uuid,
    generation integer,
    recovery_generation integer,
    expires_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
DECLARE
    recovery_record cortex_auth.installation_recovery%ROWTYPE;
    owner_principal_id uuid;
    new_credential_id uuid := gen_random_uuid();
    next_generation integer;
    issued_expires_at timestamptz;
BEGIN
    SELECT *
      INTO recovery_record
      FROM cortex_auth.installation_recovery
     WHERE recovery_token_hash = p_recovery_token_hash;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'recovery credential is invalid' USING ERRCODE = '42501';
    END IF;
    IF octet_length(p_new_token_hash) <> 32
       OR octet_length(p_new_recovery_token_hash) <> 32 THEN
        RAISE EXCEPTION 'credential hash must be 32 bytes' USING ERRCODE = '23514';
    END IF;

    SELECT o.principal_id
      INTO owner_principal_id
      FROM cortex_auth.installation_owners AS o
     WHERE o.installation_id = recovery_record.installation_id
       AND o.revoked_at IS NULL
     ORDER BY o.granted_at
     LIMIT 1;
    IF owner_principal_id IS NULL THEN
        RAISE EXCEPTION 'installation has no recorded owner to recover'
            USING ERRCODE = '42501';
    END IF;

    UPDATE cortex_auth.principals AS recovery_target
       SET status = 'active'
     WHERE recovery_target.principal_id = owner_principal_id
       AND recovery_target.status = 'revoked';

    UPDATE cortex_auth.credentials AS stale_credential
       SET revoked_at = now()
     WHERE stale_credential.principal_id = owner_principal_id
       AND stale_credential.revoked_at IS NULL;

    SELECT COALESCE(max(owner_credential.generation), 0) + 1
      INTO next_generation
      FROM cortex_auth.credentials AS owner_credential
     WHERE owner_credential.principal_id = owner_principal_id;

    INSERT INTO cortex_auth.credentials AS issued_credential(
        credential_id, principal_id, token_hash, generation, expires_at
    )
    VALUES (
        new_credential_id, owner_principal_id, p_new_token_hash, next_generation,
        now() + interval '365 days'
    )
    RETURNING issued_credential.expires_at INTO issued_expires_at;

    UPDATE cortex_auth.installation_recovery
       SET recovery_token_hash = p_new_recovery_token_hash,
           generation = installation_recovery.generation + 1,
           rotated_at = now()
     WHERE installation_recovery.installation_id = recovery_record.installation_id;

    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type, target_principal_id, detail
    )
    VALUES (
        recovery_record.installation_id, NULL, 'owner_recovery', owner_principal_id,
        jsonb_build_object(
            'credential_id', new_credential_id,
            'generation', next_generation,
            'recovery_generation', recovery_record.generation + 1,
            'expires_at', issued_expires_at
        )
    );

    RETURN QUERY SELECT
        recovery_record.installation_id,
        owner_principal_id,
        new_credential_id,
        next_generation,
        recovery_record.generation + 1,
        issued_expires_at;
END
$function$;
REVOKE ALL ON FUNCTION cortex_auth.recover_owner(bytea, bytea, bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_auth.recover_owner(bytea, bytea, bytea)
    TO cortex_v2_app;
