-- A write grant and a roster role are not project-creation capabilities.
CREATE TABLE cortex_auth.project_create_rights (
    project_scope_id uuid NOT NULL REFERENCES cortex_auth.project_installations(scope_id),
    principal_id uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    PRIMARY KEY (project_scope_id, principal_id)
);

CREATE FUNCTION cortex_auth.require_project_create(p_caller uuid, p_source_scope uuid)
RETURNS void LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, pg_temp
AS $function$
BEGIN
    IF cortex_auth.is_project_owner(p_caller, p_source_scope) THEN
        RETURN;
    END IF;
    IF cortex_auth.project_lead(p_caller, p_source_scope)
       AND EXISTS (
           SELECT 1 FROM cortex_auth.project_create_rights AS right_record
            WHERE right_record.project_scope_id = p_source_scope
              AND right_record.principal_id = p_caller
              AND right_record.revoked_at IS NULL
       ) THEN
        RETURN;
    END IF;
    RAISE EXCEPTION 'project creation requires an explicit live right'
        USING ERRCODE = 'PZC01';
END
$function$;

CREATE FUNCTION cortex_auth.allow_project_create(
    p_owner uuid, p_project_alias text, p_target_principal uuid, p_allowed boolean
) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, pg_temp
AS $function$
DECLARE
    project_id uuid;
    installation uuid;
    affected integer;
BEGIN
    IF p_allowed IS NULL THEN
        RAISE EXCEPTION 'allowed must be true or false' USING ERRCODE = '23514';
    END IF;
    SELECT project.scope_id, project.installation_id INTO project_id, installation
      FROM cortex_auth.project_installations AS project
      JOIN cortex_core.scopes AS scope ON scope.scope_id = project.scope_id
      JOIN cortex_core.scope_aliases AS alias ON alias.scope_id = project.scope_id
      JOIN cortex_auth.installations AS active_installation
        ON active_installation.installation_id = project.installation_id
     WHERE alias.alias = p_project_alias AND alias.retired_at IS NULL
       AND scope.is_active AND active_installation.status = 'active';
    IF project_id IS NULL OR NOT cortex_auth.is_project_owner(p_owner, project_id) THEN
        RAISE EXCEPTION 'only the owning installation may change project-create rights'
            USING ERRCODE = 'PZC01';
    END IF;
    PERFORM 1 FROM cortex_auth.project_installations WHERE scope_id = project_id FOR UPDATE;

    IF p_allowed THEN
        IF NOT EXISTS (
            SELECT 1 FROM cortex_auth.memberships AS membership
              JOIN cortex_auth.actor_bindings AS binding ON binding.actor_id = membership.actor_id
              JOIN cortex_auth.actors AS actor ON actor.actor_id = binding.actor_id
              JOIN cortex_auth.principals AS principal ON principal.principal_id = binding.principal_id
              JOIN cortex_auth.scope_grants AS grant_record
                ON grant_record.scope_id = membership.scope_id
               AND grant_record.principal_id = principal.principal_id
             WHERE membership.scope_id = project_id AND membership.status = 'active'
               AND membership.membership_role = 'lead' AND actor.actor_kind = 'agent'
               AND actor.status = 'active' AND actor.installation_id = installation
               AND principal.principal_id = p_target_principal
               AND principal.installation_id = installation AND principal.status = 'active'
               AND grant_record.revoked_at IS NULL
               AND grant_record.can_read AND grant_record.can_write
        ) THEN
            RAISE EXCEPTION 'only a current project lead may receive a create right'
                USING ERRCODE = 'PZC01';
        END IF;
        INSERT INTO cortex_auth.project_create_rights(project_scope_id, principal_id)
        VALUES (project_id, p_target_principal)
        ON CONFLICT (project_scope_id, principal_id)
        DO UPDATE SET granted_at = now(), revoked_at = NULL;
    ELSE
        -- Revocation remains possible after lead demotion or principal deactivation.
        IF NOT EXISTS (
            SELECT 1 FROM cortex_auth.project_create_rights AS right_record
              JOIN cortex_auth.principals AS principal
                ON principal.principal_id = right_record.principal_id
             WHERE right_record.project_scope_id = project_id
               AND right_record.principal_id = p_target_principal
               AND principal.installation_id = installation
        ) THEN
            RAISE EXCEPTION 'project-create right not found'
                USING ERRCODE = 'P0002';
        END IF;
        UPDATE cortex_auth.project_create_rights
           SET revoked_at = now()
         WHERE project_scope_id = project_id
           AND principal_id = p_target_principal
           AND revoked_at IS NULL;
    END IF;
    GET DIAGNOSTICS affected = ROW_COUNT;
    INSERT INTO cortex_auth.privileged_actions(
        installation_id, caller_principal_id, action_type,
        target_principal_id, target_scope_id, detail
    ) VALUES (
        installation, p_owner, 'allow_project_create',
        p_target_principal, project_id,
        jsonb_build_object('allowed', p_allowed, 'affected', affected)
    );
    RETURN affected;
END
$function$;

REVOKE ALL ON cortex_auth.project_create_rights FROM PUBLIC, cortex_v2_app;
REVOKE ALL ON FUNCTION cortex_auth.require_project_create(uuid,uuid),
    cortex_auth.allow_project_create(uuid,text,uuid,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_auth.require_project_create(uuid,uuid),
    cortex_auth.allow_project_create(uuid,text,uuid,boolean) TO cortex_v2_app;
