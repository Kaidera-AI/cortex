-- No project receives an installation by inference: the fixture/converter binds it explicitly.
CREATE TABLE cortex_auth.project_installations (
    scope_id uuid PRIMARY KEY REFERENCES cortex_core.scopes(scope_id),
    installation_id uuid NOT NULL REFERENCES cortex_auth.installations(installation_id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE cortex_auth.project_sponsors (
    project_scope_id uuid NOT NULL REFERENCES cortex_auth.project_installations(scope_id),
    sponsor_principal_id uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    source_scope_id uuid NOT NULL REFERENCES cortex_auth.project_installations(scope_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_scope_id),
    CHECK (project_scope_id <> source_scope_id)
);

CREATE FUNCTION cortex_auth.check_project_binding() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, cortex_auth, cortex_core, pg_temp
AS $function$
BEGIN
    IF TG_TABLE_NAME = 'project_installations' THEN
        IF NOT EXISTS (SELECT 1 FROM cortex_core.scopes
                       WHERE scope_id = NEW.scope_id AND scope_kind = 'project' AND is_active)
           OR NOT EXISTS (SELECT 1 FROM cortex_auth.installations
                          WHERE installation_id = NEW.installation_id AND status = 'active') THEN
            RAISE EXCEPTION 'project provenance requires a live project and installation' USING ERRCODE = '23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'project_sponsors' THEN
        IF NOT EXISTS (
            SELECT 1 FROM cortex_auth.project_installations AS target
              JOIN cortex_auth.project_installations AS source
                ON source.scope_id = NEW.source_scope_id
               AND source.installation_id = target.installation_id
              JOIN cortex_auth.principals AS principal
                ON principal.principal_id = NEW.sponsor_principal_id
               AND principal.installation_id = target.installation_id
             WHERE target.scope_id = NEW.project_scope_id
        ) THEN
            RAISE EXCEPTION 'sponsor provenance mismatch' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER project_installations_check BEFORE INSERT ON cortex_auth.project_installations
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.check_project_binding();
CREATE TRIGGER project_installations_immutable BEFORE UPDATE OR DELETE ON cortex_auth.project_installations
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.reject_immutable_delete();
CREATE TRIGGER project_sponsors_check BEFORE INSERT ON cortex_auth.project_sponsors
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.check_project_binding();
CREATE TRIGGER project_sponsors_immutable BEFORE UPDATE OR DELETE ON cortex_auth.project_sponsors
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.reject_immutable_delete();

CREATE FUNCTION cortex_auth.project_lead(p_caller uuid, p_scope uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM cortex_auth.memberships AS m
          JOIN cortex_auth.actor_bindings AS b ON b.actor_id = m.actor_id
          JOIN cortex_auth.actors AS a ON a.actor_id = m.actor_id
          JOIN cortex_auth.principals AS p ON p.principal_id = b.principal_id
          JOIN cortex_auth.project_installations AS project
            ON project.scope_id = m.scope_id AND project.installation_id = p.installation_id
          JOIN cortex_auth.installations AS installation ON installation.installation_id = project.installation_id
          JOIN cortex_core.scopes AS s ON s.scope_id = project.scope_id
          JOIN cortex_auth.scope_grants AS g ON g.scope_id = m.scope_id AND g.principal_id = p.principal_id
         WHERE p.principal_id = p_caller AND m.scope_id = p_scope
           AND p_caller = NULLIF(current_setting('cortex.principal_id', true), '')::uuid
           AND p.status = 'active' AND installation.status = 'active' AND s.is_active
           AND a.actor_kind = 'agent' AND a.status = 'active' AND a.installation_id = p.installation_id
           AND m.status = 'active' AND m.membership_role = 'lead'
           AND g.revoked_at IS NULL AND g.can_read AND g.can_write
    )
$function$;
CREATE FUNCTION cortex_auth.is_project_owner(p_caller uuid, p_scope uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM cortex_auth.project_installations AS project
          JOIN cortex_auth.installations AS installation ON installation.installation_id = project.installation_id
          JOIN cortex_core.scopes AS s ON s.scope_id = project.scope_id
          JOIN cortex_auth.installation_owners AS owner ON owner.installation_id = project.installation_id
          JOIN cortex_auth.principals AS p ON p.principal_id = owner.principal_id
         WHERE project.scope_id = p_scope AND owner.principal_id = p_caller
           AND p_caller = NULLIF(current_setting('cortex.principal_id', true), '')::uuid
           AND owner.revoked_at IS NULL AND p.status = 'active'
           AND installation.status = 'active' AND s.is_active
    )
$function$;
CREATE FUNCTION cortex_auth.can_manage_project_keys(p_caller uuid, p_scope uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, pg_temp
AS $function$
    SELECT cortex_auth.is_project_owner(p_caller, p_scope)
        OR cortex_auth.project_lead(p_caller, p_scope)
        OR EXISTS (
            SELECT 1 FROM cortex_auth.project_sponsors AS sponsor
              JOIN cortex_auth.project_installations AS target
                ON target.scope_id = sponsor.project_scope_id
              JOIN cortex_core.scopes AS project ON project.scope_id = target.scope_id
              JOIN cortex_auth.installations AS installation
                ON installation.installation_id = target.installation_id
             WHERE sponsor.project_scope_id = p_scope AND sponsor.sponsor_principal_id = p_caller
               AND project.is_active AND installation.status = 'active'
               AND cortex_auth.project_lead(p_caller, sponsor.source_scope_id)
        )
$function$;
CREATE FUNCTION cortex_auth.require_project_key_manager(p_caller uuid, p_scope uuid) RETURNS void
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, pg_temp
AS $function$
BEGIN
    IF NOT cortex_auth.can_manage_project_keys(p_caller, p_scope) THEN
        RAISE EXCEPTION 'project key manager required' USING ERRCODE = 'PZK01';
    END IF;
END
$function$;

REVOKE ALL ON cortex_auth.project_installations,cortex_auth.project_sponsors FROM PUBLIC,cortex_v2_app;
REVOKE ALL ON FUNCTION cortex_auth.check_project_binding(),cortex_auth.project_lead(uuid,uuid),cortex_auth.is_project_owner(uuid,uuid),cortex_auth.can_manage_project_keys(uuid,uuid),cortex_auth.require_project_key_manager(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_auth.can_manage_project_keys(uuid,uuid),cortex_auth.require_project_key_manager(uuid,uuid),cortex_auth.is_project_owner(uuid,uuid),cortex_auth.project_lead(uuid,uuid) TO cortex_v2_app;
