DO $bootstrap$
BEGIN
    IF current_user <> 'cortex_v2_migrator' THEN
        RAISE EXCEPTION 'w1 identity/memory migration must run as cortex_v2_migrator';
    END IF;
END
$bootstrap$;

-- ---------------------------------------------------------------------------
-- W1 identity plane: installations, actors, bindings, memberships, roster and
-- writer-policy revisions, ServiceAuth lifecycle relations, privileged audit.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_auth.installations (
    installation_id uuid PRIMARY KEY,
    display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 128),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'decommissioned')),
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE cortex_auth.principals
    ADD CONSTRAINT principals_installation_fk
    FOREIGN KEY (installation_id)
    REFERENCES cortex_auth.installations(installation_id);

CREATE FUNCTION cortex_auth.reject_installation_identity_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.installation_id <> OLD.installation_id
       OR NEW.display_name <> OLD.display_name
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'installation identity is immutable' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION cortex_auth.reject_immutable_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION '% rows are append-only; deletes are rejected', TG_TABLE_NAME
        USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

CREATE FUNCTION cortex_auth.reject_binding_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'actor bindings are immutable' USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

CREATE TRIGGER installations_reject_identity_mutation
    BEFORE UPDATE ON cortex_auth.installations
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.reject_installation_identity_mutation();

CREATE TRIGGER installations_reject_delete
    BEFORE DELETE ON cortex_auth.installations
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.reject_immutable_delete();

CREATE TABLE cortex_auth.installation_owners (
    installation_id uuid NOT NULL
        REFERENCES cortex_auth.installations(installation_id),
    principal_id uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    PRIMARY KEY (installation_id, principal_id)
);

CREATE TABLE cortex_auth.installation_recovery (
    installation_id uuid PRIMARY KEY
        REFERENCES cortex_auth.installations(installation_id),
    recovery_token_hash bytea NOT NULL UNIQUE CHECK (octet_length(recovery_token_hash) = 32),
    generation integer NOT NULL CHECK (generation > 0),
    rotated_at timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION cortex_auth.reject_principal_identity_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.principal_id <> OLD.principal_id
       OR NEW.installation_id <> OLD.installation_id
       OR NEW.principal_name <> OLD.principal_name
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'principal identity is immutable' USING ERRCODE = '55000';
    END IF;
    IF NEW.status NOT IN ('active', 'revoked') THEN
        RAISE EXCEPTION 'principal status transition is invalid' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER principals_reject_identity_mutation
    BEFORE UPDATE ON cortex_auth.principals
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.reject_principal_identity_mutation();

CREATE TRIGGER principals_reject_delete
    BEFORE DELETE ON cortex_auth.principals
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.reject_immutable_delete();

CREATE FUNCTION cortex_auth.reject_credential_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL THEN
        RAISE EXCEPTION 'credentials are immutable apart from revocation'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.credential_id <> OLD.credential_id
       OR NEW.principal_id <> OLD.principal_id
       OR NEW.token_hash <> OLD.token_hash
       OR NEW.generation <> OLD.generation
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'credentials are immutable apart from revocation'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER credentials_reject_mutation
    BEFORE UPDATE ON cortex_auth.credentials
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.reject_credential_mutation();

CREATE TRIGGER credentials_reject_delete
    BEFORE DELETE ON cortex_auth.credentials
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.reject_immutable_delete();

CREATE TABLE cortex_auth.actors (
    actor_id uuid PRIMARY KEY,
    installation_id uuid NOT NULL
        REFERENCES cortex_auth.installations(installation_id),
    actor_kind text NOT NULL CHECK (actor_kind IN ('human', 'agent', 'service')),
    display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 128),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cortex_auth.actor_bindings (
    actor_id uuid PRIMARY KEY REFERENCES cortex_auth.actors(actor_id),
    principal_id uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    bound_by_principal_id uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    bound_at timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION cortex_auth.reject_actor_identity_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.actor_id <> OLD.actor_id
       OR NEW.installation_id <> OLD.installation_id
       OR NEW.actor_kind <> OLD.actor_kind
       OR NEW.display_name <> OLD.display_name
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'actor identity is immutable' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER actors_reject_identity_mutation
    BEFORE UPDATE ON cortex_auth.actors
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.reject_actor_identity_mutation();

CREATE TRIGGER actors_reject_delete
    BEFORE DELETE ON cortex_auth.actors
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.reject_immutable_delete();

CREATE TRIGGER actor_bindings_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_auth.actor_bindings
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.reject_binding_mutation();

CREATE FUNCTION cortex_core.reject_scope_alias_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'scope aliases are append-only; retire instead of deleting'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.alias <> OLD.alias
       OR NEW.scope_id <> OLD.scope_id
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'alias identity is immutable' USING ERRCODE = '55000';
    END IF;
    IF OLD.retired_at IS NOT NULL
       AND NEW.retired_at IS DISTINCT FROM OLD.retired_at THEN
        RAISE EXCEPTION 'retired aliases stay reserved' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER scope_aliases_reject_identity_mutation
    BEFORE UPDATE ON cortex_core.scope_aliases
    FOR EACH ROW EXECUTE FUNCTION cortex_core.reject_scope_alias_mutation();

CREATE TRIGGER scope_aliases_reject_delete
    BEFORE DELETE ON cortex_core.scope_aliases
    FOR EACH ROW EXECUTE FUNCTION cortex_core.reject_scope_alias_mutation();

CREATE TABLE cortex_auth.memberships (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    actor_id uuid NOT NULL REFERENCES cortex_auth.actors(actor_id),
    membership_role text NOT NULL
        CHECK (membership_role IN ('owner', 'lead', 'member', 'observer')),
    responsibility text CHECK (responsibility IS NULL OR length(responsibility) BETWEEN 1 AND 128),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, actor_id)
);

CREATE TABLE cortex_auth.roster_revisions (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    revision integer NOT NULL CHECK (revision > 0),
    entries jsonb NOT NULL,
    enacted_by uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    enacted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, revision)
);

CREATE TABLE cortex_auth.writer_policies (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    revision integer NOT NULL CHECK (revision > 0),
    allowed_roles text[] NOT NULL CHECK (array_length(allowed_roles, 1) > 0),
    enacted_by uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    enacted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, revision)
);

CREATE TABLE cortex_auth.grant_revisions (
    grant_revision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    principal_id uuid NOT NULL,
    scope_id uuid NOT NULL,
    can_read boolean NOT NULL,
    can_write boolean NOT NULL,
    can_publish boolean NOT NULL,
    revoked_at timestamptz,
    changed_by uuid,
    changed_at timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION cortex_auth.record_grant_revision()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
BEGIN
    INSERT INTO cortex_auth.grant_revisions(
        principal_id, scope_id, can_read, can_write, can_publish, revoked_at, changed_by
    )
    VALUES (
        NEW.principal_id, NEW.scope_id, NEW.can_read, NEW.can_write, NEW.can_publish,
        NEW.revoked_at,
        NULLIF(current_setting('cortex.principal_id', true), '')::uuid
    );
    RETURN NEW;
END
$function$;

CREATE TRIGGER scope_grants_record_revision
    AFTER INSERT OR UPDATE ON cortex_auth.scope_grants
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.record_grant_revision();

CREATE TABLE cortex_auth.privileged_actions (
    action_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    installation_id uuid NOT NULL
        REFERENCES cortex_auth.installations(installation_id),
    caller_principal_id uuid REFERENCES cortex_auth.principals(principal_id),
    action_type text NOT NULL,
    target_principal_id uuid,
    target_scope_id uuid,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX privileged_actions_installation_created
    ON cortex_auth.privileged_actions(installation_id, created_at DESC, action_id);

CREATE FUNCTION cortex_auth.require_installation_owner(
    p_caller_principal_id uuid,
    p_installation_id uuid
)
RETURNS void
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
BEGIN
    IF p_caller_principal_id IS NULL THEN
        RAISE EXCEPTION 'owner authority requires an authenticated principal'
            USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM cortex_auth.installation_owners AS owner
         WHERE owner.installation_id = p_installation_id
           AND owner.principal_id = p_caller_principal_id
           AND owner.revoked_at IS NULL
    ) THEN
        RAISE EXCEPTION 'operation requires installation owner authority'
            USING ERRCODE = '42501';
    END IF;
END
$function$;

CREATE FUNCTION cortex_auth.caller_installation(p_caller_principal_id uuid)
RETURNS uuid
LANGUAGE sql
STABLE
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
    SELECT installation_id
      FROM cortex_auth.principals
     WHERE principal_id = p_caller_principal_id
       AND status = 'active'
$function$;

CREATE FUNCTION cortex_auth.enroll_principal(
    p_caller_principal_id uuid,
    p_principal_name text,
    p_actor_kind text,
    p_token_hash bytea,
    p_expires_at timestamptz
)
RETURNS TABLE (
    principal_id uuid,
    actor_id uuid,
    credential_id uuid,
    generation integer
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

    INSERT INTO cortex_auth.credentials(
        credential_id, principal_id, token_hash, generation, expires_at
    )
    VALUES (new_credential_id, new_principal_id, p_token_hash, 1, p_expires_at);

    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type, target_principal_id, detail
    )
    VALUES (
        caller_installation, p_caller_principal_id, 'enroll_principal', new_principal_id,
        jsonb_build_object(
            'principal_name', p_principal_name,
            'actor_kind', p_actor_kind,
            'credential_id', new_credential_id,
            'generation', 1,
            'expires_at', p_expires_at
        )
    );

    RETURN QUERY SELECT new_principal_id, new_actor_id, new_credential_id, 1;
END
$function$;

CREATE FUNCTION cortex_auth.bind_scope(
    p_caller_principal_id uuid,
    p_principal_id uuid,
    p_scope_id uuid,
    p_can_read boolean,
    p_can_write boolean,
    p_can_publish boolean
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, pg_temp
AS $function$
DECLARE
    caller_installation uuid;
BEGIN
    caller_installation := cortex_auth.caller_installation(p_caller_principal_id);
    PERFORM cortex_auth.require_installation_owner(
        p_caller_principal_id, caller_installation
    );
    IF p_can_write AND NOT p_can_read THEN
        RAISE EXCEPTION 'write grants require read grants' USING ERRCODE = '23514';
    END IF;
    IF p_can_publish AND NOT p_can_write THEN
        RAISE EXCEPTION 'publish grants require write grants' USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM cortex_core.scopes WHERE scope_id = p_scope_id AND is_active
    ) THEN
        RAISE EXCEPTION 'scope is unknown or inactive' USING ERRCODE = 'P0002';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM cortex_auth.principals
         WHERE principal_id = p_principal_id
           AND installation_id = caller_installation
    ) THEN
        RAISE EXCEPTION 'principal is unknown to this installation'
            USING ERRCODE = 'P0002';
    END IF;

    INSERT INTO cortex_auth.scope_grants(
        principal_id, scope_id, can_read, can_write, can_publish
    )
    VALUES (p_principal_id, p_scope_id, p_can_read, p_can_write, p_can_publish)
    ON CONFLICT (principal_id, scope_id) DO UPDATE
       SET can_read = EXCLUDED.can_read,
           can_write = EXCLUDED.can_write,
           can_publish = EXCLUDED.can_publish,
           revoked_at = NULL;

    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type, target_principal_id,
        target_scope_id, detail
    )
    VALUES (
        caller_installation, p_caller_principal_id, 'bind_scope', p_principal_id,
        p_scope_id,
        jsonb_build_object(
            'can_read', p_can_read, 'can_write', p_can_write,
            'can_publish', p_can_publish
        )
    );
END
$function$;

CREATE FUNCTION cortex_auth.revoke_scope_grant(
    p_caller_principal_id uuid,
    p_principal_id uuid,
    p_scope_id uuid
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
DECLARE
    caller_installation uuid;
    revoked integer;
BEGIN
    caller_installation := cortex_auth.caller_installation(p_caller_principal_id);
    PERFORM cortex_auth.require_installation_owner(
        p_caller_principal_id, caller_installation
    );
    UPDATE cortex_auth.scope_grants
       SET revoked_at = now()
     WHERE principal_id = p_principal_id
       AND scope_id = p_scope_id
       AND revoked_at IS NULL;
    GET DIAGNOSTICS revoked = ROW_COUNT;
    IF revoked = 0 THEN
        RAISE EXCEPTION 'no active grant exists for this principal and scope'
            USING ERRCODE = 'P0002';
    END IF;
    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type, target_principal_id,
        target_scope_id
    )
    VALUES (
        caller_installation, p_caller_principal_id, 'revoke_scope_grant',
        p_principal_id, p_scope_id
    );
END
$function$;

CREATE FUNCTION cortex_auth.rotate_credential(
    p_caller_principal_id uuid,
    p_principal_id uuid,
    p_new_token_hash bytea,
    p_expires_at timestamptz
)
RETURNS TABLE (credential_id uuid, generation integer, revoked_count integer)
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

    INSERT INTO cortex_auth.credentials(
        credential_id, principal_id, token_hash, generation, expires_at
    )
    VALUES (new_credential_id, p_principal_id, p_new_token_hash, next_generation, p_expires_at);

    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type, target_principal_id, detail
    )
    VALUES (
        caller_installation, p_caller_principal_id, 'rotate_credential', p_principal_id,
        jsonb_build_object(
            'credential_id', new_credential_id,
            'generation', next_generation,
            'revoked_count', revoked,
            'expires_at', p_expires_at
        )
    );

    RETURN QUERY SELECT new_credential_id, next_generation, revoked;
END
$function$;

CREATE FUNCTION cortex_auth.revoke_credential(
    p_caller_principal_id uuid,
    p_credential_id uuid
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
DECLARE
    caller_installation uuid;
    target_principal uuid;
    revoked integer;
BEGIN
    caller_installation := cortex_auth.caller_installation(p_caller_principal_id);
    IF caller_installation IS NULL THEN
        RAISE EXCEPTION 'operation requires an authenticated principal'
            USING ERRCODE = '42501';
    END IF;
    SELECT c.principal_id
      INTO target_principal
      FROM cortex_auth.credentials AS c
      JOIN cortex_auth.principals AS p ON p.principal_id = c.principal_id
     WHERE c.credential_id = p_credential_id
       AND p.installation_id = caller_installation;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'credential is unknown to this installation'
            USING ERRCODE = 'P0002';
    END IF;
    IF p_caller_principal_id <> target_principal THEN
        PERFORM cortex_auth.require_installation_owner(
            p_caller_principal_id, caller_installation
        );
    END IF;

    UPDATE cortex_auth.credentials
       SET revoked_at = now()
     WHERE credential_id = p_credential_id
       AND revoked_at IS NULL;
    GET DIAGNOSTICS revoked = ROW_COUNT;
    IF revoked = 0 THEN
        RAISE EXCEPTION 'credential is already revoked' USING ERRCODE = 'P0002';
    END IF;

    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type, target_principal_id, detail
    )
    VALUES (
        caller_installation, p_caller_principal_id, 'revoke_credential', target_principal,
        jsonb_build_object('credential_id', p_credential_id)
    );
END
$function$;

CREATE FUNCTION cortex_auth.revoke_principal(
    p_caller_principal_id uuid,
    p_principal_id uuid
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
DECLARE
    caller_installation uuid;
    revoked integer;
BEGIN
    caller_installation := cortex_auth.caller_installation(p_caller_principal_id);
    PERFORM cortex_auth.require_installation_owner(
        p_caller_principal_id, caller_installation
    );
    IF p_caller_principal_id = p_principal_id THEN
        RAISE EXCEPTION 'owner cannot revoke its own principal; use recovery'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM cortex_auth.principals
         WHERE principal_id = p_principal_id
           AND installation_id = caller_installation
           AND status = 'active'
    ) THEN
        RAISE EXCEPTION 'principal is unknown or already revoked'
            USING ERRCODE = 'P0002';
    END IF;

    UPDATE cortex_auth.credentials
       SET revoked_at = now()
     WHERE principal_id = p_principal_id
       AND revoked_at IS NULL;
    GET DIAGNOSTICS revoked = ROW_COUNT;
    UPDATE cortex_auth.principals
       SET status = 'revoked'
     WHERE principal_id = p_principal_id;

    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type, target_principal_id, detail
    )
    VALUES (
        caller_installation, p_caller_principal_id, 'revoke_principal', p_principal_id,
        jsonb_build_object('revoked_credentials', revoked)
    );
END
$function$;

CREATE FUNCTION cortex_auth.recover_owner(
    p_recovery_token_hash bytea,
    p_new_token_hash bytea,
    p_new_recovery_token_hash bytea,
    p_expires_at timestamptz
)
RETURNS TABLE (
    installation_id uuid,
    principal_id uuid,
    credential_id uuid,
    generation integer,
    recovery_generation integer
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

    INSERT INTO cortex_auth.credentials(
        credential_id, principal_id, token_hash, generation, expires_at
    )
    VALUES (
        new_credential_id, owner_principal_id, p_new_token_hash, next_generation,
        p_expires_at
    );

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
            'recovery_generation', recovery_record.generation + 1
        )
    );

    RETURN QUERY SELECT
        recovery_record.installation_id,
        owner_principal_id,
        new_credential_id,
        next_generation,
        recovery_record.generation + 1;
END
$function$;

CREATE FUNCTION cortex_auth.rename_scope_alias(
    p_caller_principal_id uuid,
    p_scope_id uuid,
    p_new_alias text
)
RETURNS TABLE (retained_alias text, new_primary_alias text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, pg_temp
AS $function$
DECLARE
    caller_installation uuid;
    prior_primary text;
BEGIN
    caller_installation := cortex_auth.caller_installation(p_caller_principal_id);
    PERFORM cortex_auth.require_installation_owner(
        p_caller_principal_id, caller_installation
    );
    IF length(p_new_alias) NOT BETWEEN 1 AND 96 THEN
        RAISE EXCEPTION 'alias is invalid' USING ERRCODE = '23514';
    END IF;

    PERFORM 1
      FROM cortex_core.scopes AS s
     WHERE s.scope_id = p_scope_id AND s.is_active
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'scope is unknown or inactive' USING ERRCODE = 'P0002';
    END IF;

    SELECT a.alias
      INTO prior_primary
      FROM cortex_core.scope_aliases AS a
     WHERE a.scope_id = p_scope_id
       AND a.is_primary
       AND a.retired_at IS NULL;
    IF prior_primary IS NULL THEN
        RAISE EXCEPTION 'scope has no primary alias' USING ERRCODE = 'P0002';
    END IF;
    IF prior_primary = p_new_alias THEN
        RAISE EXCEPTION 'alias already resolves to this scope' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (SELECT 1 FROM cortex_core.scope_aliases WHERE alias = p_new_alias) THEN
        RAISE EXCEPTION 'alias is reserved by another identity' USING ERRCODE = '23505';
    END IF;

    UPDATE cortex_core.scope_aliases
       SET is_primary = false
     WHERE scope_id = p_scope_id
       AND is_primary
       AND retired_at IS NULL;

    INSERT INTO cortex_core.scope_aliases(alias, scope_id, is_primary)
    VALUES (p_new_alias, p_scope_id, true);

    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type, target_scope_id, detail
    )
    VALUES (
        caller_installation,
        p_caller_principal_id, 'rename_scope_alias', p_scope_id,
        jsonb_build_object('retained_alias', prior_primary, 'new_alias', p_new_alias)
    );

    RETURN QUERY SELECT prior_primary, p_new_alias;
END
$function$;

CREATE FUNCTION cortex_auth.enact_roster(
    p_caller_principal_id uuid,
    p_scope_id uuid,
    p_entries jsonb
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, pg_temp
AS $function$
DECLARE
    caller_installation uuid;
    entry jsonb;
    entry_principal uuid;
    entry_actor uuid;
    entry_role text;
    entry_responsibility text;
    resolved jsonb := '[]'::jsonb;
    next_revision integer;
    responsibility_owner_count integer;
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
    IF jsonb_typeof(p_entries) <> 'array' OR jsonb_array_length(p_entries) = 0 THEN
        RAISE EXCEPTION 'roster requires at least one entry' USING ERRCODE = '23514';
    END IF;

    FOR entry IN SELECT * FROM jsonb_array_elements(p_entries)
    LOOP
        IF entry ->> 'role' NOT IN ('owner', 'lead', 'member', 'observer') THEN
            RAISE EXCEPTION 'roster role is invalid' USING ERRCODE = '23514';
        END IF;
        BEGIN
            entry_principal := (entry ->> 'principal_id')::uuid;
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION 'roster entry principal is invalid' USING ERRCODE = '23514';
        END;
        entry_role := entry ->> 'role';
        entry_responsibility := entry ->> 'responsibility';
        IF entry_responsibility IS NOT NULL
           AND length(entry_responsibility) NOT BETWEEN 1 AND 128 THEN
            RAISE EXCEPTION 'roster responsibility is invalid' USING ERRCODE = '23514';
        END IF;

        SELECT b.actor_id
          INTO entry_actor
          FROM cortex_auth.actor_bindings AS b
          JOIN cortex_auth.principals AS p ON p.principal_id = b.principal_id
         WHERE b.principal_id = entry_principal
           AND p.installation_id = caller_installation
           AND p.status = 'active';
        IF entry_actor IS NULL THEN
            RAISE EXCEPTION 'roster principal has no active bound actor'
                USING ERRCODE = 'P0002';
        END IF;

        INSERT INTO cortex_auth.memberships(
            scope_id, actor_id, membership_role, responsibility, status
        )
        VALUES (p_scope_id, entry_actor, entry_role, entry_responsibility, 'active')
        ON CONFLICT (scope_id, actor_id) DO UPDATE
           SET membership_role = EXCLUDED.membership_role,
               responsibility = EXCLUDED.responsibility,
               status = 'active';

        resolved := resolved || jsonb_build_array(
            jsonb_build_object(
                'principal_id', entry_principal,
                'actor_id', entry_actor,
                'role', entry_role,
                'responsibility', entry_responsibility
            )
        );
    END LOOP;

    SELECT count(*)
      INTO responsibility_owner_count
      FROM jsonb_array_elements(resolved) AS resolved_entry
     WHERE resolved_entry ->> 'role' = 'owner';
    IF responsibility_owner_count > 1 THEN
        RAISE EXCEPTION 'roster responsibility routing is ambiguous: multiple owners'
            USING ERRCODE = '23514';
    END IF;

    SELECT COALESCE(max(revision), 0) + 1
      INTO next_revision
      FROM cortex_auth.roster_revisions
     WHERE scope_id = p_scope_id;

    INSERT INTO cortex_auth.roster_revisions(scope_id, revision, entries, enacted_by)
    VALUES (p_scope_id, next_revision, resolved, p_caller_principal_id);

    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type, target_scope_id, detail
    )
    VALUES (
        caller_installation, p_caller_principal_id, 'enact_roster', p_scope_id,
        jsonb_build_object('roster_revision', next_revision)
    );

    RETURN next_revision;
END
$function$;

CREATE FUNCTION cortex_auth.enact_writer_policy(
    p_caller_principal_id uuid,
    p_scope_id uuid,
    p_allowed_roles text[],
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
    role text;
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
    IF p_allowed_roles IS NULL OR array_length(p_allowed_roles, 1) IS NULL THEN
        RAISE EXCEPTION 'writer policy requires at least one allowed role'
            USING ERRCODE = '23514';
    END IF;
    FOREACH role IN ARRAY p_allowed_roles
    LOOP
        IF role NOT IN ('owner', 'lead', 'member', 'observer') THEN
            RAISE EXCEPTION 'writer policy role is invalid' USING ERRCODE = '23514';
        END IF;
    END LOOP;

    SELECT COALESCE(max(revision), 0)
      INTO current_revision
      FROM cortex_auth.writer_policies
     WHERE scope_id = p_scope_id;
    IF p_expected_revision <> current_revision THEN
        RAISE EXCEPTION 'writer-policy revision moved; recheck and retry'
            USING ERRCODE = '40001';
    END IF;
    next_revision := current_revision + 1;

    INSERT INTO cortex_auth.writer_policies(
        scope_id, revision, allowed_roles, enacted_by
    )
    VALUES (p_scope_id, next_revision, p_allowed_roles, p_caller_principal_id);

    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type, target_scope_id, detail
    )
    VALUES (
        caller_installation, p_caller_principal_id, 'enact_writer_policy', p_scope_id,
        jsonb_build_object(
            'policy_revision', next_revision, 'allowed_roles', p_allowed_roles
        )
    );

    RETURN next_revision;
END
$function$;

CREATE FUNCTION cortex_auth.list_privileged_actions(
    p_caller_principal_id uuid,
    p_limit integer
)
RETURNS TABLE (
    action_id uuid,
    action_type text,
    caller_principal_id uuid,
    target_principal_id uuid,
    target_scope_id uuid,
    detail jsonb,
    created_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
DECLARE
    caller_installation uuid;
    bounded_limit integer := least(greatest(COALESCE(p_limit, 50), 1), 200);
BEGIN
    caller_installation := cortex_auth.caller_installation(p_caller_principal_id);
    PERFORM cortex_auth.require_installation_owner(
        p_caller_principal_id, caller_installation
    );
    RETURN QUERY
    SELECT a.action_id, a.action_type, a.caller_principal_id, a.target_principal_id,
           a.target_scope_id, a.detail, a.created_at
      FROM cortex_auth.privileged_actions AS a
     WHERE a.installation_id = caller_installation
     ORDER BY a.created_at DESC, a.action_id DESC
     LIMIT bounded_limit;
END
$function$;

-- Registry secrecy: the roster/membership surface is readable only inside
-- granted scopes; identity mutations run exclusively through the SECURITY
-- DEFINER operations above.

ALTER TABLE cortex_auth.memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_auth.memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY memberships_scope_read ON cortex_auth.memberships
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY memberships_migrator_all ON cortex_auth.memberships
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_auth.roster_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_auth.roster_revisions FORCE ROW LEVEL SECURITY;
CREATE POLICY roster_revisions_scope_read ON cortex_auth.roster_revisions
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY roster_revisions_migrator_all ON cortex_auth.roster_revisions
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_auth.writer_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_auth.writer_policies FORCE ROW LEVEL SECURITY;
CREATE POLICY writer_policies_scope_read ON cortex_auth.writer_policies
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY writer_policies_migrator_all ON cortex_auth.writer_policies
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_auth.actors ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_auth.actors FORCE ROW LEVEL SECURITY;
CREATE POLICY actors_visible_read ON cortex_auth.actors
    FOR SELECT TO cortex_v2_app
    USING (
        EXISTS (
            SELECT 1
              FROM cortex_auth.actor_bindings AS binding
             WHERE binding.actor_id = actors.actor_id
               AND binding.principal_id = NULLIF(
                       current_setting('cortex.principal_id', true), ''
                   )::uuid
        )
        OR EXISTS (
            SELECT 1
              FROM cortex_auth.memberships AS membership
             WHERE membership.actor_id = actors.actor_id
               AND membership.status = 'active'
               AND cortex_core.scope_read_policy(membership.scope_id)
        )
    );
CREATE POLICY actors_migrator_all ON cortex_auth.actors
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_auth.actor_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_auth.actor_bindings FORCE ROW LEVEL SECURITY;
CREATE POLICY actor_bindings_visible_read ON cortex_auth.actor_bindings
    FOR SELECT TO cortex_v2_app
    USING (
        principal_id = NULLIF(current_setting('cortex.principal_id', true), '')::uuid
        OR EXISTS (
            SELECT 1
              FROM cortex_auth.memberships AS membership
             WHERE membership.actor_id = actor_bindings.actor_id
               AND membership.status = 'active'
               AND cortex_core.scope_read_policy(membership.scope_id)
        )
    );
CREATE POLICY actor_bindings_migrator_all ON cortex_auth.actor_bindings
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- W1 canonical content plane: source connectors, typed content items,
-- immutable revisions with provenance, status lifecycle, lexical projection,
-- outbox and typed command receipts.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_core.source_connectors (
    connector_id uuid PRIMARY KEY,
    namespace text NOT NULL UNIQUE CHECK (length(namespace) BETWEEN 1 AND 64),
    connector_kind text NOT NULL
        CHECK (connector_kind IN ('session_ingest', 'file', 'api', 'manual')),
    installation_id uuid NOT NULL
        REFERENCES cortex_auth.installations(installation_id),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION cortex_core.register_source_connector(
    p_caller_principal_id uuid,
    p_namespace text,
    p_connector_kind text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, pg_temp
AS $function$
DECLARE
    caller_installation uuid;
    new_connector_id uuid := gen_random_uuid();
BEGIN
    SELECT installation_id
      INTO caller_installation
      FROM cortex_auth.principals
     WHERE principal_id = p_caller_principal_id
       AND status = 'active';
    PERFORM cortex_auth.require_installation_owner(
        p_caller_principal_id, caller_installation
    );
    IF length(p_namespace) NOT BETWEEN 1 AND 64
       OR p_namespace !~ '^[a-z0-9][a-z0-9._-]*$' THEN
        RAISE EXCEPTION 'connector namespace is invalid' USING ERRCODE = '23514';
    END IF;
    IF p_connector_kind NOT IN ('session_ingest', 'file', 'api', 'manual') THEN
        RAISE EXCEPTION 'connector kind is invalid' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM cortex_core.source_connectors WHERE namespace = p_namespace
    ) THEN
        RAISE EXCEPTION 'connector namespace already exists' USING ERRCODE = '23505';
    END IF;

    INSERT INTO cortex_core.source_connectors(
        connector_id, namespace, connector_kind, installation_id
    )
    VALUES (new_connector_id, p_namespace, p_connector_kind, caller_installation);

    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type, detail
    )
    VALUES (
        caller_installation, p_caller_principal_id, 'register_source_connector',
        jsonb_build_object(
            'connector_id', new_connector_id, 'namespace', p_namespace,
            'connector_kind', p_connector_kind
        )
    );
    RETURN new_connector_id;
END
$function$;

CREATE TABLE cortex_core.content_items (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    content_id uuid NOT NULL,
    content_class text NOT NULL CHECK (content_class IN (
        'decision', 'lesson', 'knowledge', 'progress', 'diary',
        'message', 'session', 'artifact', 'work_product'
    )),
    created_by_principal uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, content_id),
    FOREIGN KEY (created_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE TABLE cortex_core.content_revisions (
    scope_id uuid NOT NULL,
    content_id uuid NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    payload jsonb NOT NULL,
    body_text text NOT NULL CHECK (length(body_text) BETWEEN 1 AND 65536),
    content_hash bytea NOT NULL CHECK (octet_length(content_hash) = 32),
    author_principal_id uuid NOT NULL,
    authored_at timestamptz NOT NULL DEFAULT now(),
    source_connector_id uuid REFERENCES cortex_core.source_connectors(connector_id),
    source_key text CHECK (source_key IS NULL OR length(source_key) BETWEEN 1 AND 256),
    source_observed_at timestamptz,
    supersedes_revision integer,
    PRIMARY KEY (scope_id, content_id, revision),
    FOREIGN KEY (scope_id, content_id)
        REFERENCES cortex_core.content_items(scope_id, content_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (author_principal_id, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT,
    CHECK ((source_key IS NULL) = (source_connector_id IS NULL)),
    CHECK (
        supersedes_revision IS NULL
        OR (supersedes_revision > 0 AND supersedes_revision < revision)
    )
);

CREATE UNIQUE INDEX content_revisions_unique_source_key
    ON cortex_core.content_revisions(scope_id, source_connector_id, source_key)
    WHERE source_key IS NOT NULL;

CREATE FUNCTION cortex_core.content_payload_violation(
    p_content_class text,
    p_payload jsonb
)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    required_field text;
    required_fields text[];
BEGIN
    IF jsonb_typeof(p_payload) <> 'object' THEN
        RETURN 'payload must be a JSON object';
    END IF;
    required_fields := CASE p_content_class
        WHEN 'decision' THEN ARRAY['statement']
        WHEN 'lesson' THEN ARRAY['lesson']
        WHEN 'knowledge' THEN ARRAY['content']
        WHEN 'progress' THEN ARRAY['summary']
        WHEN 'diary' THEN ARRAY['entry']
        WHEN 'message' THEN ARRAY['role', 'text']
        WHEN 'session' THEN ARRAY['source', 'message_count']
        WHEN 'artifact' THEN ARRAY['media_type', 'bytes_sha256', 'location']
        WHEN 'work_product' THEN ARRAY['kind', 'summary', 'references']
        ELSE NULL
    END;
    IF required_fields IS NULL THEN
        RETURN 'content class has no typed payload contract';
    END IF;
    FOREACH required_field IN ARRAY required_fields
    LOOP
        IF NOT (p_payload ? required_field)
           OR jsonb_typeof(p_payload -> required_field) = 'null' THEN
            RETURN format('payload is missing required field "%s"', required_field);
        END IF;
    END LOOP;
    IF p_content_class = 'message'
       AND p_payload ->> 'role' NOT IN ('user', 'assistant', 'system', 'tool') THEN
        RETURN 'message role is invalid';
    END IF;
    IF p_content_class = 'session'
       AND jsonb_typeof(p_payload -> 'message_count') <> 'number' THEN
        RETURN 'session message_count must be a number';
    END IF;
    IF p_content_class = 'artifact'
       AND p_payload ->> 'bytes_sha256' !~ '^[0-9a-f]{64}$' THEN
        RETURN 'artifact bytes_sha256 must be lowercase hex sha256';
    END IF;
    IF p_content_class = 'work_product'
       AND jsonb_typeof(p_payload -> 'references') <> 'array' THEN
        RETURN 'work_product references must be an array';
    END IF;
    RETURN NULL;
END
$function$;

CREATE FUNCTION cortex_core.validate_content_revision_append()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_core, cortex_auth, pg_temp
AS $function$
DECLARE
    item_class text;
    payload_violation text;
    declared_policy_revision text;
    current_policy_revision text;
BEGIN
    SELECT content_class
      INTO item_class
      FROM cortex_core.content_items
     WHERE scope_id = NEW.scope_id
       AND content_id = NEW.content_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'content revision requires its canonical item'
            USING ERRCODE = '23503';
    END IF;

    IF NEW.revision = 1 THEN
        IF NEW.supersedes_revision IS NOT NULL THEN
            RAISE EXCEPTION 'initial content revision supersedes nothing'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        IF NEW.supersedes_revision IS DISTINCT FROM NEW.revision - 1 THEN
            RAISE EXCEPTION 'content revision must immediately follow its predecessor'
                USING ERRCODE = '23514';
        END IF;
        IF NOT EXISTS (
            SELECT 1
              FROM cortex_core.content_revisions
             WHERE scope_id = NEW.scope_id
               AND content_id = NEW.content_id
               AND revision = NEW.revision - 1
        ) THEN
            RAISE EXCEPTION 'content revision predecessor is missing'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    payload_violation := cortex_core.content_payload_violation(item_class, NEW.payload);
    IF payload_violation IS NOT NULL THEN
        RAISE EXCEPTION '%', payload_violation USING ERRCODE = '23514';
    END IF;

    SELECT COALESCE(max(revision), 0)::text
      INTO current_policy_revision
      FROM cortex_auth.writer_policies
     WHERE scope_id = NEW.scope_id;
    declared_policy_revision := NULLIF(
        current_setting('cortex.policy_revision', true), ''
    );
    IF declared_policy_revision IS DISTINCT FROM current_policy_revision THEN
        RAISE EXCEPTION 'writer-policy revision recheck failed; re-resolve scope policy'
            USING ERRCODE = '55000';
    END IF;

    RETURN NEW;
END
$function$;

CREATE FUNCTION cortex_core.reject_content_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'canonical content rows are append-only' USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

CREATE TRIGGER content_revisions_validate_append
    BEFORE INSERT ON cortex_core.content_revisions
    FOR EACH ROW EXECUTE FUNCTION cortex_core.validate_content_revision_append();

CREATE TRIGGER content_items_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_core.content_items
    FOR EACH ROW EXECUTE FUNCTION cortex_core.reject_content_mutation();

CREATE TRIGGER content_revisions_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_core.content_revisions
    FOR EACH ROW EXECUTE FUNCTION cortex_core.reject_content_mutation();

CREATE TABLE cortex_core.content_status_log (
    scope_id uuid NOT NULL,
    content_id uuid NOT NULL,
    status_seq integer NOT NULL CHECK (status_seq > 0),
    status text NOT NULL CHECK (status IN (
        'current', 'invalidated', 'superseded', 'tombstoned'
    )),
    prior_status text NOT NULL,
    superseded_by_content_id uuid,
    reason text CHECK (reason IS NULL OR length(reason) BETWEEN 1 AND 512),
    changed_by_principal uuid NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, content_id, status_seq),
    FOREIGN KEY (scope_id, content_id)
        REFERENCES cortex_core.content_items(scope_id, content_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (scope_id, superseded_by_content_id)
        REFERENCES cortex_core.content_items(scope_id, content_id)
        ON DELETE RESTRICT,
    CHECK ((status = 'superseded') = (superseded_by_content_id IS NOT NULL))
);

CREATE FUNCTION cortex_core.validate_content_status_transition()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_core, pg_temp
AS $function$
DECLARE
    prior_status text;
    prior_seq integer;
BEGIN
    SELECT s.status, s.status_seq
      INTO prior_status, prior_seq
      FROM cortex_core.content_status_log AS s
     WHERE s.scope_id = NEW.scope_id
       AND s.content_id = NEW.content_id
     ORDER BY s.status_seq DESC
     LIMIT 1;
    prior_status := COALESCE(prior_status, 'current');
    NEW.status_seq := COALESCE(prior_seq, 0) + 1;
    NEW.prior_status := prior_status;

    IF prior_status = 'tombstoned' THEN
        RAISE EXCEPTION 'tombstoned content cannot change status'
            USING ERRCODE = '55000';
    END IF;
    IF NOT (
        (prior_status = 'current' AND NEW.status IN ('invalidated', 'superseded', 'tombstoned'))
        OR (prior_status = 'invalidated' AND NEW.status = 'current')
    ) THEN
        RAISE EXCEPTION 'content status transition % -> % is not allowed',
            prior_status, NEW.status
            USING ERRCODE = '23514';
    END IF;
    IF NEW.status = 'superseded'
       AND NEW.superseded_by_content_id = NEW.content_id THEN
        RAISE EXCEPTION 'content cannot supersede itself' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER content_status_validate_transition
    BEFORE INSERT ON cortex_core.content_status_log
    FOR EACH ROW EXECUTE FUNCTION cortex_core.validate_content_status_transition();

CREATE TRIGGER content_status_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_core.content_status_log
    FOR EACH ROW EXECUTE FUNCTION cortex_core.reject_content_mutation();

CREATE FUNCTION cortex_core.content_current_status(
    p_scope_id uuid,
    p_content_id uuid
)
RETURNS text
LANGUAGE sql
STABLE
SET search_path = pg_catalog, cortex_core, pg_temp
AS $function$
    SELECT COALESCE(
        (
            SELECT s.status
              FROM cortex_core.content_status_log AS s
             WHERE s.scope_id = p_scope_id
               AND s.content_id = p_content_id
             ORDER BY s.status_seq DESC
             LIMIT 1
        ),
        'current'
    )
$function$;

CREATE TABLE cortex_core.content_lexical_documents (
    scope_id uuid NOT NULL,
    content_id uuid NOT NULL,
    revision integer NOT NULL,
    source_text text NOT NULL,
    search_document tsvector GENERATED ALWAYS AS (
        to_tsvector('simple'::regconfig, source_text)
    ) STORED,
    PRIMARY KEY (scope_id, content_id, revision),
    FOREIGN KEY (scope_id, content_id, revision)
        REFERENCES cortex_core.content_revisions(scope_id, content_id, revision)
        ON DELETE RESTRICT
);

CREATE FUNCTION cortex_core.project_content_lexical_document()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_core, pg_temp
AS $function$
DECLARE
    canonical_body text;
BEGIN
    SELECT body_text
      INTO canonical_body
      FROM cortex_core.content_revisions
     WHERE scope_id = NEW.scope_id
       AND content_id = NEW.content_id
       AND revision = NEW.revision;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'lexical projection requires its canonical content revision'
            USING ERRCODE = '23503';
    END IF;
    NEW.source_text := canonical_body;
    RETURN NEW;
END
$function$;

CREATE TRIGGER content_lexical_documents_project_canonical
    BEFORE INSERT ON cortex_core.content_lexical_documents
    FOR EACH ROW EXECUTE FUNCTION cortex_core.project_content_lexical_document();

CREATE TRIGGER content_lexical_documents_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_core.content_lexical_documents
    FOR EACH ROW EXECUTE FUNCTION cortex_core.reject_lexical_document_mutation();

CREATE INDEX content_lexical_documents_search_gin
    ON cortex_core.content_lexical_documents USING gin(search_document);

CREATE INDEX content_items_scope_created
    ON cortex_core.content_items(scope_id, created_at DESC, content_id);

CREATE TABLE cortex_core.content_outbox_events (
    event_id uuid PRIMARY KEY,
    scope_id uuid NOT NULL,
    content_id uuid NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz,
    FOREIGN KEY (scope_id, content_id)
        REFERENCES cortex_core.content_items(scope_id, content_id)
        ON DELETE RESTRICT
);

CREATE INDEX content_outbox_events_scope_created
    ON cortex_core.content_outbox_events(scope_id, created_at, event_id);

CREATE FUNCTION cortex_core.caller_installation_matches(p_installation_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1
          FROM cortex_auth.principals AS principal
         WHERE principal.principal_id = NULLIF(
                   current_setting('cortex.principal_id', true), ''
               )::uuid
           AND principal.installation_id = p_installation_id
           AND principal.status = 'active'
    )
$function$;

CREATE TABLE cortex_core.command_receipts (
    principal_id uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    installation_id uuid REFERENCES cortex_auth.installations(installation_id),
    scope_id uuid REFERENCES cortex_core.scopes(scope_id),
    operation text NOT NULL,
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
    request_hash bytea NOT NULL,
    receipt_kind text NOT NULL CHECK (receipt_kind IN (
        'committed', 'pending_processing', 'verified_effect', 'conflict', 'unknown'
    )),
    receipt jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal_id, operation, idempotency_key),
    CHECK ((installation_id IS NULL) <> (scope_id IS NULL))
);

CREATE TABLE cortex_core.conversion_quarantine (
    quarantine_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL,
    source_namespace text NOT NULL,
    source_reference text NOT NULL,
    original_payload jsonb NOT NULL,
    payload_sha256 bytea NOT NULL CHECK (octet_length(payload_sha256) = 32),
    reason text NOT NULL,
    owner_reference text,
    disposition text NOT NULL DEFAULT 'quarantine_review'
        CHECK (disposition IN (
            'quarantine_review', 'migrated', 'retained', 'rejected', 'rebuilt'
        )),
    reviewer_principal_id uuid REFERENCES cortex_auth.principals(principal_id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX conversion_quarantine_run
    ON cortex_core.conversion_quarantine(run_id, source_namespace, source_reference);

-- RLS for the W1 canonical content plane (forced for every role including
-- owners of the runtime credential; policies reuse the 0001 helpers).

ALTER TABLE cortex_core.source_connectors ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.source_connectors FORCE ROW LEVEL SECURITY;
CREATE POLICY source_connectors_authenticated_read ON cortex_core.source_connectors
    FOR SELECT TO cortex_v2_app
    USING (
        NULLIF(current_setting('cortex.principal_id', true), '') IS NOT NULL
        AND status = 'active'
    );
CREATE POLICY source_connectors_migrator_all ON cortex_core.source_connectors
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_core.content_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.content_items FORCE ROW LEVEL SECURITY;
CREATE POLICY content_items_scope_read ON cortex_core.content_items
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY content_items_scope_insert ON cortex_core.content_items
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND created_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY content_items_migrator_all ON cortex_core.content_items
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_core.content_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.content_revisions FORCE ROW LEVEL SECURITY;
CREATE POLICY content_revisions_scope_read ON cortex_core.content_revisions
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY content_revisions_scope_insert ON cortex_core.content_revisions
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND author_principal_id = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY content_revisions_migrator_all ON cortex_core.content_revisions
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_core.content_status_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.content_status_log FORCE ROW LEVEL SECURITY;
CREATE POLICY content_status_scope_read ON cortex_core.content_status_log
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY content_status_scope_insert ON cortex_core.content_status_log
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND changed_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY content_status_migrator_all ON cortex_core.content_status_log
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_core.content_lexical_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.content_lexical_documents FORCE ROW LEVEL SECURITY;
CREATE POLICY content_lexical_scope_read ON cortex_core.content_lexical_documents
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY content_lexical_scope_insert ON cortex_core.content_lexical_documents
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY content_lexical_migrator_all ON cortex_core.content_lexical_documents
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_core.content_outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.content_outbox_events FORCE ROW LEVEL SECURITY;
CREATE POLICY content_outbox_scope_read ON cortex_core.content_outbox_events
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY content_outbox_scope_insert ON cortex_core.content_outbox_events
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY content_outbox_migrator_all ON cortex_core.content_outbox_events
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_core.command_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.command_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY command_receipts_scope_read ON cortex_core.command_receipts
    FOR SELECT TO cortex_v2_app
    USING (
        principal_id = NULLIF(current_setting('cortex.principal_id', true), '')::uuid
        AND (
            (scope_id IS NOT NULL AND cortex_core.scope_read_policy(scope_id))
            OR (installation_id IS NOT NULL
                AND cortex_core.caller_installation_matches(installation_id))
        )
    );
CREATE POLICY command_receipts_scope_insert ON cortex_core.command_receipts
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        principal_id = NULLIF(current_setting('cortex.principal_id', true), '')::uuid
        AND (
            (scope_id IS NOT NULL AND cortex_core.scope_write_policy(scope_id))
            OR (installation_id IS NOT NULL
                AND cortex_core.caller_installation_matches(installation_id))
        )
    );
CREATE POLICY command_receipts_migrator_all ON cortex_core.command_receipts
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_core.conversion_quarantine ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_core.conversion_quarantine FORCE ROW LEVEL SECURITY;
CREATE POLICY conversion_quarantine_migrator_all ON cortex_core.conversion_quarantine
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

-- Grants: app receives the least DML required; every identity mutation and
-- registry command runs through the SECURITY DEFINER operations above.

GRANT SELECT ON cortex_auth.memberships, cortex_auth.roster_revisions,
    cortex_auth.writer_policies, cortex_auth.actors, cortex_auth.actor_bindings
    TO cortex_v2_app;
GRANT SELECT ON cortex_core.source_connectors TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_core.content_items TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_core.content_revisions TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_core.content_status_log TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_core.content_lexical_documents TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_core.content_outbox_events TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_core.command_receipts TO cortex_v2_app;

REVOKE ALL ON FUNCTION cortex_auth.require_installation_owner(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.caller_installation(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.enroll_principal(uuid, text, text, bytea, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.bind_scope(uuid, uuid, uuid, boolean, boolean, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.revoke_scope_grant(uuid, uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.rotate_credential(uuid, uuid, bytea, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.revoke_credential(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.revoke_principal(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.recover_owner(bytea, bytea, bytea, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.rename_scope_alias(uuid, uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.enact_roster(uuid, uuid, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.enact_writer_policy(uuid, uuid, text[], integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.list_privileged_actions(uuid, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_core.register_source_connector(uuid, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_core.content_payload_violation(text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_core.content_current_status(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.reject_immutable_delete() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.reject_binding_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_core.reject_scope_alias_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.reject_installation_identity_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.reject_principal_identity_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.reject_credential_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.reject_actor_identity_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_auth.record_grant_revision() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_core.validate_content_revision_append() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_core.validate_content_status_transition() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_core.project_content_lexical_document() FROM PUBLIC;
REVOKE ALL ON FUNCTION cortex_core.reject_content_mutation() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION cortex_core.content_payload_violation(text, jsonb) TO cortex_v2_app;
REVOKE ALL ON FUNCTION cortex_core.caller_installation_matches(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_core.caller_installation_matches(uuid) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_auth.enroll_principal(uuid, text, text, bytea, timestamptz) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_auth.bind_scope(uuid, uuid, uuid, boolean, boolean, boolean) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_auth.revoke_scope_grant(uuid, uuid, uuid) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_auth.rotate_credential(uuid, uuid, bytea, timestamptz) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_auth.revoke_credential(uuid, uuid) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_auth.revoke_principal(uuid, uuid) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_auth.recover_owner(bytea, bytea, bytea, timestamptz) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_auth.rename_scope_alias(uuid, uuid, text) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_auth.enact_roster(uuid, uuid, jsonb) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_auth.enact_writer_policy(uuid, uuid, text[], integer) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_auth.list_privileged_actions(uuid, integer) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_core.register_source_connector(uuid, text, text) TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_core.content_current_status(uuid, uuid) TO cortex_v2_app;
