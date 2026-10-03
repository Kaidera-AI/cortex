-- S5/S5c plus r25 canonical registry. No backfill of missing legacy fields.
-- Authentication uses current_setting populated by the server, not request actor text.
-- Plaintext credentials never reach SQL, registry records, receipts or audit.
DO $guard$
BEGIN
    IF current_user <> 'cortex_v2_migrator' THEN
        RAISE EXCEPTION 'S5 migration requires cortex_v2_migrator';
    END IF;
END
$guard$;

CREATE TABLE cortex_core.project_registry (
    project_scope_id uuid PRIMARY KEY REFERENCES cortex_auth.project_installations(scope_id),
    original_project_id text NOT NULL UNIQUE CHECK (length(original_project_id) > 0),
    display_name text NOT NULL,
    default_agent text,
    status text NOT NULL,
    parent_project_key text,
    repo_root text NOT NULL CHECK (length(repo_root) > 0),
    roots jsonb NOT NULL CHECK (jsonb_typeof(roots) = 'array' AND jsonb_array_length(roots) > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE TABLE cortex_core.project_identities (
    identity_id uuid PRIMARY KEY,
    project_scope_id uuid NOT NULL REFERENCES cortex_core.project_registry(project_scope_id),
    original_identity_id text NOT NULL,
    identity_name text NOT NULL,
    identity_kind text NOT NULL,
    original_record jsonb NOT NULL CHECK (jsonb_typeof(original_record) = 'object'),
    UNIQUE(project_scope_id, original_identity_id)
);
CREATE TABLE cortex_core.project_profiles (
    profile_id uuid PRIMARY KEY,
    project_scope_id uuid NOT NULL REFERENCES cortex_core.project_registry(project_scope_id),
    original_profile_id text NOT NULL,
    agent_name text NOT NULL,
    original_record jsonb NOT NULL CHECK (jsonb_typeof(original_record) = 'object'),
    UNIQUE(project_scope_id, original_profile_id)
);
CREATE INDEX project_profiles_agent ON cortex_core.project_profiles(project_scope_id, agent_name);

DO $rls$
DECLARE name text;
BEGIN
    FOREACH name IN ARRAY ARRAY['project_registry','project_identities','project_profiles'] LOOP
        EXECUTE format('ALTER TABLE cortex_core.%I ENABLE ROW LEVEL SECURITY', name);
        EXECUTE format('ALTER TABLE cortex_core.%I FORCE ROW LEVEL SECURITY', name);
        EXECUTE format('CREATE POLICY %I ON cortex_core.%I FOR SELECT TO cortex_v2_app USING (cortex_core.scope_read_policy(project_scope_id))', name || '_read', name);
        EXECUTE format('CREATE POLICY %I ON cortex_core.%I FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true)', name || '_migrator', name);
        EXECUTE format('REVOKE ALL ON cortex_core.%I FROM PUBLIC, cortex_v2_app', name);
        EXECUTE format('GRANT SELECT ON cortex_core.%I TO cortex_v2_app', name);
    END LOOP;
END
$rls$;

CREATE TABLE cortex_auth.credential_managers (
    credential_id uuid PRIMARY KEY REFERENCES cortex_auth.credentials(credential_id),
    manager text NOT NULL CHECK (manager IN ('kos','openkai','user'))
);
CREATE TABLE cortex_auth.project_creation_operations (
    operation_id uuid PRIMARY KEY,
    creator_principal_id uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    project_scope_id uuid NOT NULL UNIQUE REFERENCES cortex_auth.project_installations(scope_id),
    source_scope_id uuid REFERENCES cortex_auth.project_installations(scope_id),
    lead_principal_id uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    console_principal_id uuid REFERENCES cortex_auth.principals(principal_id),
    lead_manager text NOT NULL CHECK (lead_manager IN ('kos','openkai','user')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER project_creation_operations_immutable
    BEFORE UPDATE OR DELETE ON cortex_auth.project_creation_operations
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.reject_immutable_delete();
CREATE TRIGGER credential_managers_immutable
    BEFORE UPDATE OR DELETE ON cortex_auth.credential_managers
    FOR EACH ROW EXECUTE FUNCTION cortex_auth.reject_immutable_delete();
REVOKE ALL ON cortex_auth.credential_managers, cortex_auth.project_creation_operations FROM PUBLIC,cortex_v2_app;

-- Shared internal issuer, unavailable to the application role. No backfill of other keys.
CREATE FUNCTION cortex_auth.issue_project_credential(p_principal uuid, p_hash bytea, p_manager text)
RETURNS jsonb LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
DECLARE new_id uuid := gen_random_uuid(); next_generation integer; expiry timestamptz := now()+interval '365 days';
BEGIN
    IF p_hash IS NULL OR octet_length(p_hash) <> 32 OR p_manager IS NULL OR p_manager NOT IN ('kos','openkai','user') THEN
        RAISE EXCEPTION 'invalid project credential' USING ERRCODE='23514';
    END IF;
    PERFORM 1 FROM cortex_auth.principals WHERE principal_id=p_principal AND status='active' FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'inactive recipient' USING ERRCODE='PZC01'; END IF;
    SELECT coalesce(max(generation),0)+1 INTO next_generation FROM cortex_auth.credentials WHERE principal_id=p_principal;
    -- Lost initial delivery: supersede immediately; generic rotation/overlap is S10.
    UPDATE cortex_auth.credentials SET revoked_at=now() WHERE principal_id=p_principal AND revoked_at IS NULL;
    INSERT INTO cortex_auth.credentials(credential_id,principal_id,token_hash,generation,expires_at)
        VALUES(new_id,p_principal,p_hash,next_generation,expiry);
    INSERT INTO cortex_auth.credential_managers VALUES(new_id,p_manager);
    RETURN jsonb_build_object('principal_id',p_principal,'credential_id',new_id,'generation',next_generation,'expires_at',expiry,'manager',p_manager);
END
$function$;
REVOKE ALL ON FUNCTION cortex_auth.issue_project_credential(uuid,bytea,text) FROM PUBLIC,cortex_v2_app;

CREATE FUNCTION cortex_auth.create_project(
    p_caller uuid, p_operation uuid, p_request jsonb, p_lead_hash bytea, p_console_hash bytea
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, pg_temp
AS $function$
DECLARE
    installation uuid; source_id uuid; parent_id uuid; owner_actor record;
    new_scope uuid := gen_random_uuid(); lead_id uuid := gen_random_uuid(); lead_actor uuid := gen_random_uuid();
    console_id uuid; console_actor uuid; is_owner boolean; first_project boolean;
    has_console boolean; lead_credential jsonb; console_credential jsonb;
BEGIN
    IF p_caller IS DISTINCT FROM NULLIF(current_setting('cortex.principal_id',true),'')::uuid THEN
        RAISE EXCEPTION 'caller mismatch' USING ERRCODE='PZC01';
    END IF;
    installation := cortex_auth.caller_installation(p_caller);
    -- One installation lock covers concurrent first-project creation and current authority checks.
    PERFORM 1 FROM cortex_auth.installations WHERE installation_id=installation AND status='active' FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'installation unavailable' USING ERRCODE='PZC01'; END IF;
    SELECT EXISTS(SELECT 1 FROM cortex_auth.installation_owners WHERE installation_id=installation
                  AND principal_id=p_caller AND revoked_at IS NULL) INTO is_owner;
    IF p_request->>'source_project' IS NULL THEN
        IF NOT is_owner THEN RAISE EXCEPTION 'source create right required' USING ERRCODE='PZC01'; END IF;
    ELSE
        SELECT binding.scope_id INTO source_id FROM cortex_auth.project_installations binding
          JOIN cortex_core.scope_aliases alias ON alias.scope_id=binding.scope_id
          JOIN cortex_core.scopes scope ON scope.scope_id=binding.scope_id
         WHERE binding.installation_id=installation AND alias.alias=p_request->>'source_project'
           AND alias.retired_at IS NULL AND scope.is_active;
        IF source_id IS NULL THEN RAISE EXCEPTION 'source create right required' USING ERRCODE='PZC01'; END IF;
        PERFORM cortex_auth.require_project_create(p_caller,source_id);
    END IF;
    IF p_operation IS NULL OR p_request IS NULL OR jsonb_typeof(p_request)<>'object'
       OR length(coalesce(p_request->>'project_key','')) NOT BETWEEN 1 AND 96
       OR length(coalesce(p_request->>'display_name','')) NOT BETWEEN 1 AND 128
       OR length(coalesce(p_request->>'lead_name','')) NOT BETWEEN 1 AND 128
       OR p_request->>'lead_name' = 'console'
       OR length(coalesce(p_request->>'lead_responsibility','')) NOT BETWEEN 1 AND 128
       OR coalesce(p_request->>'lead_key_manager','') NOT IN ('kos','openkai','user')
       OR coalesce(jsonb_typeof(p_request->'with_console'),'') <> 'boolean'
       OR length(coalesce(p_request->>'repo_root','')) NOT BETWEEN 1 AND 4096
       OR coalesce(jsonb_typeof(p_request->'roots'),'') <> 'array'
       OR EXISTS(SELECT 1 FROM jsonb_object_keys(p_request) key WHERE key NOT IN
                 ('source_project','project_key','display_name','lead_name','lead_responsibility','lead_key_manager','with_console','parent_project_key','repo_root','roots')) THEN
        RAISE EXCEPTION 'invalid project request' USING ERRCODE='23514';
    END IF;
    IF jsonb_array_length(p_request->'roots') NOT BETWEEN 1 AND 64
       OR (SELECT count(*) FROM jsonb_array_elements(p_request->'roots') root WHERE root->>'kind'='primary')<>1
       OR NOT EXISTS(SELECT 1 FROM jsonb_array_elements(p_request->'roots') root WHERE root->>'kind'='primary' AND root->>'path'=p_request->>'repo_root')
       OR EXISTS(SELECT 1 FROM jsonb_array_elements(p_request->'roots') root WHERE
                 jsonb_typeof(root)<>'object' OR jsonb_typeof(root->'path') IS DISTINCT FROM 'string'
                 OR length(coalesce(root->>'path','')) NOT BETWEEN 1 AND 4096
                 OR coalesce(root->>'kind','') NOT IN ('primary','reference')) THEN
        RAISE EXCEPTION 'invalid project roots' USING ERRCODE='23514';
    END IF;
    IF p_request->>'parent_project_key' IS NOT NULL THEN
        SELECT binding.scope_id INTO parent_id FROM cortex_auth.project_installations binding
          JOIN cortex_core.scope_aliases alias ON alias.scope_id=binding.scope_id
          JOIN cortex_core.scopes scope ON scope.scope_id=binding.scope_id
         WHERE binding.installation_id=installation AND alias.alias=p_request->>'parent_project_key'
           AND alias.retired_at IS NULL AND scope.is_active
           AND (is_owner OR cortex_auth.project_lead(p_caller,binding.scope_id));
        IF parent_id IS NULL THEN RAISE EXCEPTION 'parent unavailable' USING ERRCODE='PZC01'; END IF;
    END IF;
    has_console := (p_request->>'with_console')::boolean;
    IF p_lead_hash IS NULL OR octet_length(p_lead_hash)<>32
       OR (has_console AND (p_console_hash IS NULL OR octet_length(p_console_hash)<>32 OR p_console_hash=p_lead_hash))
       OR (NOT has_console AND p_console_hash IS NOT NULL) THEN
        RAISE EXCEPTION 'invalid recipient digests' USING ERRCODE='23514';
    END IF;
    SELECT NOT EXISTS(SELECT 1 FROM cortex_auth.project_installations WHERE installation_id=installation) INTO first_project;
    INSERT INTO cortex_core.scopes(scope_id,scope_kind,display_name) VALUES(new_scope,'project',p_request->>'display_name');
    INSERT INTO cortex_core.scope_aliases(alias,scope_id,is_primary) VALUES(p_request->>'project_key',new_scope,true);
    INSERT INTO cortex_auth.project_installations(scope_id,installation_id) VALUES(new_scope,installation);
    INSERT INTO cortex_core.project_registry VALUES(new_scope,new_scope::text,p_request->>'display_name',
        p_request->>'lead_name','active',p_request->>'parent_project_key',p_request->>'repo_root',p_request->'roots',now(),now());
    INSERT INTO cortex_auth.principals(principal_id,installation_id,principal_name,status)
        VALUES(lead_id,installation,(p_request->>'lead_name')||'@'||(p_request->>'project_key'),'active');
    INSERT INTO cortex_auth.actors(actor_id,installation_id,actor_kind,display_name)
        VALUES(lead_actor,installation,'agent',p_request->>'lead_name');
    INSERT INTO cortex_auth.actor_bindings(actor_id,principal_id,bound_by_principal_id) VALUES(lead_actor,lead_id,p_caller);
    INSERT INTO cortex_auth.scope_grants(principal_id,scope_id,can_read,can_write,can_publish) VALUES(lead_id,new_scope,true,true,true);
    INSERT INTO cortex_auth.memberships(scope_id,actor_id,membership_role,responsibility)
        VALUES(new_scope,lead_actor,'lead',p_request->>'lead_responsibility');
    INSERT INTO cortex_core.project_identities VALUES(lead_actor,new_scope,lead_actor::text,p_request->>'lead_name','agent',
        jsonb_build_object('id',lead_actor,'name',p_request->>'lead_name','status','active','responsibility',p_request->>'lead_responsibility',
                           'capabilities',jsonb_build_object('keep_visible',true)));
    lead_credential := cortex_auth.issue_project_credential(lead_id,p_lead_hash,p_request->>'lead_key_manager');
    FOR owner_actor IN
        SELECT principal.principal_id, binding.actor_id FROM cortex_auth.installation_owners owner
          JOIN cortex_auth.principals principal USING(principal_id)
          JOIN cortex_auth.actor_bindings binding USING(principal_id)
          JOIN cortex_auth.actors actor USING(actor_id)
         WHERE owner.installation_id=installation AND owner.revoked_at IS NULL AND principal.status='active'
           AND actor.status='active' AND actor.installation_id=installation
    LOOP
        INSERT INTO cortex_auth.scope_grants(principal_id,scope_id,can_read,can_write,can_publish)
            VALUES(owner_actor.principal_id,new_scope,true,true,true);
        INSERT INTO cortex_auth.memberships(scope_id,actor_id,membership_role) VALUES(new_scope,owner_actor.actor_id,'owner');
    END LOOP;
    IF first_project THEN
        INSERT INTO cortex_auth.project_create_rights(project_scope_id,principal_id) VALUES(new_scope,lead_id);
    END IF;
    IF NOT is_owner THEN
        INSERT INTO cortex_auth.project_sponsors(project_scope_id,sponsor_principal_id,source_scope_id) VALUES(new_scope,p_caller,source_id);
    END IF;
    IF has_console THEN
        console_id:=gen_random_uuid(); console_actor:=gen_random_uuid();
        INSERT INTO cortex_auth.principals(principal_id,installation_id,principal_name,status) VALUES(console_id,installation,'console@'||(p_request->>'project_key'),'active');
        INSERT INTO cortex_auth.actors(actor_id,installation_id,actor_kind,display_name) VALUES(console_actor,installation,'service','console');
        INSERT INTO cortex_auth.actor_bindings(actor_id,principal_id,bound_by_principal_id) VALUES(console_actor,console_id,p_caller);
        INSERT INTO cortex_auth.scope_grants(principal_id,scope_id,can_read,can_write) VALUES(console_id,new_scope,true,true);
        INSERT INTO cortex_auth.memberships(scope_id,actor_id,membership_role) VALUES(new_scope,console_actor,'member');
        console_credential:=cortex_auth.issue_project_credential(console_id,p_console_hash,'kos');
    END IF;
    INSERT INTO cortex_auth.project_creation_operations VALUES(p_operation,p_caller,new_scope,source_id,lead_id,console_id,p_request->>'lead_key_manager',now());
    INSERT INTO cortex_auth.privileged_actions(installation_id,caller_principal_id,action_type,target_principal_id,target_scope_id,detail)
        VALUES(installation,p_caller,'create_project',lead_id,new_scope,jsonb_build_object('operation_id',p_operation,'source_scope_id',source_id,
        'lead',lead_credential,'console',console_credential,'first_project_create_right',first_project));
    RETURN jsonb_build_object('operation_id',p_operation,'project_id',new_scope,'project_key',p_request->>'project_key',
        'lead',lead_credential,'console',console_credential,'delivery_state','reissue_required');
END
$function$;
REVOKE ALL ON FUNCTION cortex_auth.create_project(uuid,uuid,jsonb,bytea,bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_auth.create_project(uuid,uuid,jsonb,bytea,bytea) TO cortex_v2_app;

CREATE FUNCTION cortex_auth.reissue_project_creation_keys(p_caller uuid,p_operation uuid,p_lead_hash bytea,p_console_hash bytea)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, pg_temp
AS $function$
DECLARE op cortex_auth.project_creation_operations%ROWTYPE; installation uuid; lead_key jsonb; console_key jsonb; project_key text;
BEGIN
    IF p_caller IS DISTINCT FROM NULLIF(current_setting('cortex.principal_id',true),'')::uuid THEN
        RAISE EXCEPTION 'caller mismatch' USING ERRCODE='PZC01';
    END IF;
    installation:=cortex_auth.caller_installation(p_caller);
    PERFORM 1 FROM cortex_auth.installations WHERE installation_id=installation AND status='active' FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'operation unavailable' USING ERRCODE='PZC01'; END IF;
    SELECT creation.* INTO op FROM cortex_auth.project_creation_operations creation
      JOIN cortex_auth.project_installations project ON project.scope_id=creation.project_scope_id
      JOIN cortex_core.scopes scope ON scope.scope_id=project.scope_id
     WHERE creation.operation_id=p_operation AND creation.creator_principal_id=p_caller
       AND project.installation_id=installation AND scope.is_active FOR UPDATE OF creation;
    IF NOT FOUND THEN RAISE EXCEPTION 'operation unavailable' USING ERRCODE='PZC01'; END IF;
    PERFORM cortex_auth.require_project_key_manager(p_caller,op.project_scope_id);
    IF p_lead_hash IS NULL OR octet_length(p_lead_hash)<>32
       OR (op.console_principal_id IS NOT NULL AND (p_console_hash IS NULL OR octet_length(p_console_hash)<>32 OR p_console_hash=p_lead_hash))
       OR (op.console_principal_id IS NULL AND p_console_hash IS NOT NULL) THEN
        RAISE EXCEPTION 'invalid recipient digests' USING ERRCODE='23514';
    END IF;
    lead_key:=cortex_auth.issue_project_credential(op.lead_principal_id,p_lead_hash,op.lead_manager);
    IF op.console_principal_id IS NOT NULL THEN
        console_key:=cortex_auth.issue_project_credential(op.console_principal_id,p_console_hash,'kos');
    END IF;
    SELECT alias INTO project_key FROM cortex_core.scope_aliases WHERE scope_id=op.project_scope_id AND is_primary AND retired_at IS NULL;
    INSERT INTO cortex_auth.privileged_actions(installation_id,caller_principal_id,action_type,target_scope_id,detail)
        VALUES(installation,p_caller,'reissue_project_creation_keys',op.project_scope_id,
               jsonb_build_object('operation_id',p_operation,'reason','recipient_store_failed','lead',lead_key,'console',console_key));
    RETURN jsonb_build_object('operation_id',p_operation,'project_id',op.project_scope_id,'project_key',project_key,
                             'lead',lead_key,'console',console_key,'delivery_state','reissue_required');
END
$function$;
REVOKE ALL ON FUNCTION cortex_auth.reissue_project_creation_keys(uuid,uuid,bytea,bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_auth.reissue_project_creation_keys(uuid,uuid,bytea,bytea) TO cortex_v2_app;
