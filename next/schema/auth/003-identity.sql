-- Explicit installer invocation only. Existing identities are never auto-adopted.
CREATE FUNCTION auth.identity_roles_valid(p_roles text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
    SELECT p_roles IS NOT NULL AND cardinality(p_roles)<=32
      AND array_position(p_roles,NULL) IS NULL
      AND NOT EXISTS(SELECT 1 FROM unnest(p_roles) r
          WHERE length(r) NOT BETWEEN 1 AND 64 OR r !~ '^[a-z][a-z0-9_.-]{0,63}$' OR r ~ '[^a-z0-9_.-]')
      AND p_roles=ARRAY(SELECT DISTINCT r FROM unnest(p_roles) r ORDER BY r);
$$;
ALTER TABLE auth.principals ADD COLUMN kind text NOT NULL DEFAULT 'agent' CHECK(kind IN ('agent','human'));
ALTER TABLE auth.principals ADD COLUMN identity_adopted boolean NOT NULL DEFAULT false;
ALTER TABLE auth.project_grants ADD COLUMN roles text[] NOT NULL DEFAULT '{}' CHECK(auth.identity_roles_valid(roles));
CREATE VIEW auth.project_role_memberships WITH(security_barrier=true,security_invoker=true) AS
    SELECT g.tenant_id,g.project_id,g.principal_id,r.role
    FROM auth.project_grants g JOIN auth.principals p ON(p.tenant_id,p.id)=(g.tenant_id,g.principal_id)
    CROSS JOIN LATERAL unnest(g.roles) r(role) WHERE p.identity_adopted AND NOT p.disabled;
REVOKE ALL ON auth.project_role_memberships FROM PUBLIC,"kaidera-runtime-core-request";

-- New private functions derive actor and project from the C04 accepted binding.
CREATE FUNCTION auth.identity_scope(p_control boolean)
RETURNS TABLE(installation_id uuid,tenant_id uuid,project_id uuid,principal_id uuid,permission_generation bigint,action text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE digest text; installation uuid; project uuid; configured_action text;
BEGIN
    digest:=current_setting('cortex.credential_digest',true);
    installation:=NULLIF(current_setting('cortex.installation_id',true),'')::uuid;
    project:=NULLIF(current_setting('cortex.project_id',true),'')::uuid;
    configured_action:=current_setting('cortex.action',true);
    IF p_control IS NULL OR NOT auth.context_is_bound(digest,installation,project,configured_action)
       OR (p_control AND configured_action<>'control') THEN RETURN; END IF;
    RETURN QUERY SELECT * FROM auth.resolve_scope(digest,installation,project,configured_action);
EXCEPTION WHEN invalid_text_representation THEN RETURN;
END;
$$;

CREATE FUNCTION auth.identity_lookup(p_target uuid)
RETURNS TABLE(principal_id uuid,name text,kind text,adopted boolean,roles text[],generation bigint)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record;
BEGIN
    SELECT * INTO s FROM auth.identity_scope(false);
    IF NOT FOUND OR p_target IS NULL THEN RETURN; END IF;
    RETURN QUERY SELECT p.id,p.name,p.kind,p.identity_adopted,g.roles,s.permission_generation
      FROM auth.principals p JOIN auth.project_grants g ON(p.tenant_id,p.id)=(g.tenant_id,g.principal_id)
      WHERE p.tenant_id=s.tenant_id AND p.id=p_target AND g.project_id=s.project_id
        AND NOT p.disabled AND p.identity_adopted FOR SHARE OF p,g;
END;
$$;

CREATE FUNCTION auth.identity_begin(p_key text,p_sha text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; r record;
BEGIN
    SELECT * INTO s FROM auth.identity_scope(true);
    IF NOT FOUND THEN RAISE EXCEPTION 'identity_forbidden'; END IF;
    IF p_key IS NULL OR length(p_key) NOT BETWEEN 1 AND 256 OR p_sha IS NULL
       OR length(p_sha)<>64 OR p_sha ~ '[^0-9a-f]' THEN RAISE EXCEPTION 'identity_invalid_input'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(s.tenant_id::text||'/'||s.project_id::text||'/'||s.principal_id::text||'/'||p_key,0));
    SELECT * INTO r FROM coordination.idempotency i WHERE(i.tenant_id,i.project_id,i.principal_id,i.request_key)
      =(s.tenant_id,s.project_id,s.principal_id,p_key);
    IF FOUND THEN
        IF r.request_sha256<>p_sha OR r.outcome<>'committed' THEN RAISE EXCEPTION 'identity_conflict'; END IF;
        RETURN r.receipt||jsonb_build_object('replayed',true);
    END IF;
    RETURN NULL;
END;
$$;

CREATE FUNCTION auth.identity_finish(p_key text,p_sha text,p_receipt jsonb,p_audit jsonb) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; audit_id uuid:=gen_random_uuid(); body bytea; receipt jsonb;
BEGIN
    SELECT * INTO s FROM auth.identity_scope(true);
    IF NOT FOUND THEN RAISE EXCEPTION 'identity_forbidden'; END IF;
    body:=convert_to((p_audit||jsonb_build_object('actor',s.principal_id,'project',s.project_id,
                                               'request_sha256',p_sha))::text,'UTF8');
    INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256)
      VALUES(s.tenant_id,s.project_id,audit_id,body,encode(sha256(body),'hex'));
    receipt:=p_receipt||jsonb_build_object('audit_id',audit_id,'replayed',false);
    INSERT INTO coordination.idempotency(tenant_id,project_id,principal_id,request_key,request_sha256,outcome,receipt)
      VALUES(s.tenant_id,s.project_id,s.principal_id,p_key,p_sha,'committed',receipt);
    RETURN receipt;
END;
$$;

CREATE FUNCTION auth.identity_register(p_kind text,p_target uuid,p_name text,p_roles text[],p_permissions text[],
    p_credential uuid,p_digest text,p_ttl integer,p_attestation text,p_key text,p_sha text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; replay jsonb;
BEGIN
    replay:=auth.identity_begin(p_key,p_sha); IF replay IS NOT NULL THEN RETURN replay; END IF;
    SELECT * INTO s FROM auth.identity_scope(true);
    IF p_kind IS NULL OR p_kind NOT IN ('agent','human') OR p_target IS NULL OR p_name IS NULL
       OR length(p_name) NOT BETWEEN 1 AND 256 OR NOT auth.identity_roles_valid(p_roles)
       OR p_permissions IS NULL OR cardinality(p_permissions)=0
       OR NOT(p_permissions<@ARRAY['read','write','control','owner','admin'])
       OR p_digest IS NULL OR length(p_digest)<>64 OR p_digest ~ '[^0-9a-f]'
       OR p_credential IS NULL OR p_ttl IS NULL OR p_ttl NOT BETWEEN 60 AND 2592000
       OR (p_kind='human' AND (p_attestation IS NULL OR length(btrim(p_attestation)) NOT BETWEEN 1 AND 512))
       THEN RAISE EXCEPTION 'identity_invalid_input'; END IF;
    IF EXISTS(SELECT 1 FROM auth.principals p WHERE p.tenant_id=s.tenant_id AND(p.id=p_target OR p.name=p_name))
       THEN RAISE EXCEPTION 'identity_conflict'; END IF;
    INSERT INTO auth.principals(tenant_id,id,name,kind,identity_adopted) VALUES(s.tenant_id,p_target,p_name,p_kind,true);
    INSERT INTO auth.project_grants(tenant_id,project_id,principal_id,permissions,roles)
      VALUES(s.tenant_id,s.project_id,p_target,p_permissions,p_roles);
    INSERT INTO auth.credentials(tenant_id,id,principal_id,key_digest,expires_at)
      VALUES(s.tenant_id,p_credential,p_target,p_digest,clock_timestamp()+make_interval(secs=>p_ttl));
    RETURN auth.identity_finish(p_key,p_sha,jsonb_build_object('principal_id',p_target,'credential_id',p_credential),
      jsonb_build_object('operation','register','kind',p_kind,'principal_id',p_target,'roles',p_roles,
                        'permissions',p_permissions,'credential_id',p_credential,
                        'attestation_sha256',CASE WHEN p_attestation IS NULL THEN NULL ELSE encode(sha256(convert_to(p_attestation,'UTF8')),'hex') END));
END;
$$;

CREATE FUNCTION auth.identity_register_agent(p_target uuid,p_name text,p_roles text[],p_permissions text[],
    p_credential uuid,p_digest text,p_ttl integer,p_key text,p_sha text) RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
    SELECT auth.identity_register('agent',p_target,p_name,p_roles,p_permissions,p_credential,p_digest,p_ttl,NULL,p_key,p_sha);
$$;
CREATE FUNCTION auth.identity_register_human(p_target uuid,p_name text,p_roles text[],p_permissions text[],
    p_credential uuid,p_digest text,p_ttl integer,p_attestation text,p_key text,p_sha text) RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
    SELECT auth.identity_register('human',p_target,p_name,p_roles,p_permissions,p_credential,p_digest,p_ttl,p_attestation,p_key,p_sha);
$$;

CREATE FUNCTION auth.identity_set_roles(p_target uuid,p_roles text[],p_key text,p_sha text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; replay jsonb;
BEGIN
    replay:=auth.identity_begin(p_key,p_sha); IF replay IS NOT NULL THEN RETURN replay; END IF;
    SELECT * INTO s FROM auth.identity_scope(true);
    IF NOT auth.identity_roles_valid(p_roles) THEN RAISE EXCEPTION 'identity_invalid_input'; END IF;
    PERFORM 1 FROM auth.identity_lookup(p_target); IF NOT FOUND THEN RAISE EXCEPTION 'identity_conflict'; END IF;
    UPDATE auth.project_grants SET roles=p_roles WHERE(tenant_id,project_id,principal_id)=(s.tenant_id,s.project_id,p_target);
    RETURN auth.identity_finish(p_key,p_sha,jsonb_build_object('principal_id',p_target),
                               jsonb_build_object('operation','set_roles','principal_id',p_target,'roles',p_roles));
END;
$$;

-- Principal-wide keys cannot be rotated/revoked by one of several project owners.
CREATE FUNCTION auth.identity_key_target(p_credential uuid) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; target uuid;
BEGIN
    SELECT * INTO s FROM auth.identity_scope(true); IF NOT FOUND THEN RAISE EXCEPTION 'identity_forbidden'; END IF;
    SELECT c.principal_id INTO target FROM auth.credentials c JOIN auth.principals p ON(p.tenant_id,p.id)=(c.tenant_id,c.principal_id)
      WHERE c.tenant_id=s.tenant_id AND c.id=p_credential AND p.identity_adopted AND NOT p.disabled FOR UPDATE OF c,p;
    IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM auth.project_grants g WHERE(g.tenant_id,g.project_id,g.principal_id)=(s.tenant_id,s.project_id,target))
       OR (SELECT count(*) FROM auth.project_grants g WHERE(g.tenant_id,g.principal_id)=(s.tenant_id,target))<>1
       THEN RAISE EXCEPTION 'identity_forbidden'; END IF;
    RETURN target;
END;
$$;
CREATE FUNCTION auth.identity_rotate(p_target uuid,p_old uuid,p_new uuid,p_digest text,p_ttl integer,p_key text,p_sha text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; replay jsonb; target uuid;
BEGIN
    replay:=auth.identity_begin(p_key,p_sha); IF replay IS NOT NULL THEN RETURN replay; END IF;
    SELECT * INTO s FROM auth.identity_scope(true); target:=auth.identity_key_target(p_old);
    IF p_target IS NULL OR target<>p_target OR p_new IS NULL OR p_digest IS NULL OR length(p_digest)<>64 OR p_digest ~ '[^0-9a-f]'
       OR p_ttl IS NULL OR p_ttl NOT BETWEEN 60 AND 2592000 THEN RAISE EXCEPTION 'identity_invalid_input'; END IF;
    UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE(tenant_id,id)=(s.tenant_id,p_old);
    INSERT INTO auth.credentials(tenant_id,id,principal_id,key_digest,expires_at)
      VALUES(s.tenant_id,p_new,p_target,p_digest,clock_timestamp()+make_interval(secs=>p_ttl));
    RETURN auth.identity_finish(p_key,p_sha,jsonb_build_object('principal_id',p_target,'credential_id',p_new),
                               jsonb_build_object('operation','rotate','principal_id',p_target,'old_credential',p_old,'credential_id',p_new));
END;
$$;
CREATE FUNCTION auth.identity_revoke(p_credential uuid,p_key text,p_sha text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; replay jsonb; target uuid;
BEGIN
    replay:=auth.identity_begin(p_key,p_sha); IF replay IS NOT NULL THEN RETURN replay; END IF;
    SELECT * INTO s FROM auth.identity_scope(true); target:=auth.identity_key_target(p_credential);
    UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE(tenant_id,id)=(s.tenant_id,p_credential);
    RETURN auth.identity_finish(p_key,p_sha,jsonb_build_object('principal_id',target,'credential_id',p_credential,'revoked',true),
                               jsonb_build_object('operation','revoke','principal_id',target,'credential_id',p_credential));
END;
$$;

CREATE FUNCTION auth.identity_adoption_check(p_target uuid,p_name text) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; p record;
BEGIN
    SELECT * INTO s FROM auth.identity_scope(true); IF NOT FOUND THEN RAISE EXCEPTION 'identity_forbidden'; END IF;
    IF p_target IS NULL OR p_name IS NULL OR length(p_name) NOT BETWEEN 1 AND 256 THEN RETURN false; END IF;
    SELECT * INTO p FROM auth.principals WHERE tenant_id=s.tenant_id AND(id=p_target OR name=p_name) FOR SHARE;
    IF NOT FOUND THEN RETURN true; END IF;
    RETURN p.id=p_target AND p.name=p_name AND p.kind='agent' AND NOT p.disabled AND NOT p.identity_adopted;
END;
$$;
CREATE FUNCTION auth.identity_adopt(p_manifest jsonb,p_manifest_sha text,p_key text,p_sha text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; replay jsonb; item jsonb; target uuid; roles text[]; permissions text[];
BEGIN
    replay:=auth.identity_begin(p_key,p_sha); IF replay IS NOT NULL THEN RETURN replay; END IF;
    SELECT * INTO s FROM auth.identity_scope(true);
    IF p_manifest IS NULL OR jsonb_typeof(p_manifest->'agents')<>'array'
       OR jsonb_array_length(p_manifest->'agents') NOT BETWEEN 1 AND 1000
       OR p_manifest_sha IS NULL OR length(p_manifest_sha)<>64 OR p_manifest_sha ~ '[^0-9a-f]'
       THEN RAISE EXCEPTION 'identity_invalid_input'; END IF;
    FOR item IN SELECT * FROM jsonb_array_elements(p_manifest->'agents') LOOP
        target:=(item->>'principal_id')::uuid;
        roles:=ARRAY(SELECT jsonb_array_elements_text(item->'roles'));
        permissions:=ARRAY(SELECT jsonb_array_elements_text(item->'permissions'));
        IF item->>'kind' IS DISTINCT FROM 'agent' OR NOT auth.identity_roles_valid(roles)
           OR cardinality(permissions)=0 OR NOT(permissions<@ARRAY['read','write','control','owner','admin'])
           OR NOT auth.identity_adoption_check(target,item->>'name') THEN RAISE EXCEPTION 'identity_conflict'; END IF;
        INSERT INTO auth.principals(tenant_id,id,name,kind,identity_adopted)
          VALUES(s.tenant_id,target,item->>'name','agent',true)
          ON CONFLICT(tenant_id,id) DO UPDATE SET identity_adopted=true;
        INSERT INTO auth.project_grants(tenant_id,project_id,principal_id,permissions,roles)
          VALUES(s.tenant_id,s.project_id,target,permissions,roles)
          ON CONFLICT(tenant_id,project_id,principal_id) DO UPDATE SET permissions=EXCLUDED.permissions,roles=EXCLUDED.roles;
    END LOOP;
    RETURN auth.identity_finish(p_key,p_sha,jsonb_build_object('manifest_sha256',p_manifest_sha,'count',jsonb_array_length(p_manifest->'agents')),
                               jsonb_build_object('operation','adopt','manifest_sha256',p_manifest_sha,'manifest',p_manifest));
END;
$$;

GRANT INSERT ON auth.principals,auth.project_grants,auth.credentials TO "kaidera-runtime-core-verifier";
GRANT SELECT,INSERT ON core.payloads,coordination.idempotency TO "kaidera-runtime-core-verifier";
CREATE POLICY c04a_identity_private ON core.payloads TO "kaidera-runtime-core-verifier" USING(true) WITH CHECK(true);
CREATE POLICY c04a_identity_private ON coordination.idempotency TO "kaidera-runtime-core-verifier" USING(true) WITH CHECK(true);
GRANT CREATE ON SCHEMA auth TO "kaidera-runtime-core-verifier";
DO $$ DECLARE f record; BEGIN
    FOR f IN SELECT oid::regprocedure AS signature FROM pg_proc WHERE pronamespace='auth'::regnamespace AND proname LIKE 'identity_%' LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO %I',f.signature,'kaidera-runtime-core-verifier');
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,%I',f.signature,'kaidera-runtime-core-request');
    END LOOP;
END $$;
ALTER VIEW auth.project_role_memberships OWNER TO "kaidera-runtime-core-verifier";
REVOKE CREATE ON SCHEMA auth FROM "kaidera-runtime-core-verifier";
GRANT EXECUTE ON FUNCTION auth.identity_lookup(uuid),auth.identity_register_agent(uuid,text,text[],text[],uuid,text,integer,text,text),
    auth.identity_register_human(uuid,text,text[],text[],uuid,text,integer,text,text,text),auth.identity_set_roles(uuid,text[],text,text),
    auth.identity_rotate(uuid,uuid,uuid,text,integer,text,text),auth.identity_revoke(uuid,text,text),
    auth.identity_adoption_check(uuid,text),auth.identity_adopt(jsonb,text,text,text) TO "kaidera-runtime-core-request";
