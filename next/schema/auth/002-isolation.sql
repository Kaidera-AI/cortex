-- Additive C04 boundary. Existing canonical migrations remain immutable.
DO $$
DECLARE name text; role_row record;
BEGIN
    FOREACH name IN ARRAY ARRAY['kaidera-runtime-core-request','kaidera-runtime-core-verifier'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=name) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',name);
        END IF;
        SELECT * INTO role_row FROM pg_roles WHERE rolname=name;
        IF role_row.rolsuper OR role_row.rolbypassrls OR role_row.rolcreatedb OR role_row.rolcreaterole
           OR role_row.rolreplication OR role_row.rolcanlogin OR role_row.rolinherit
           OR EXISTS(SELECT 1 FROM pg_auth_members WHERE member=role_row.oid)
           OR (name='kaidera-runtime-core-verifier' AND EXISTS(SELECT 1 FROM pg_auth_members WHERE roleid=role_row.oid)) THEN
            RAISE EXCEPTION 'unsafe existing Core role' USING ERRCODE='42501';
        END IF;
    END LOOP;
END;
$$;

REVOKE ALL ON SCHEMA core,auth,coordination,retrieval FROM PUBLIC;
GRANT USAGE ON SCHEMA core,auth,coordination,retrieval TO "kaidera-runtime-core-request";
GRANT USAGE ON SCHEMA core,auth TO "kaidera-runtime-core-verifier";
REVOKE ALL ON ALL TABLES IN SCHEMA core,auth,coordination,retrieval FROM PUBLIC,"kaidera-runtime-core-request","kaidera-runtime-core-verifier";

CREATE FUNCTION auth.resolve_scope(p_digest text,p_installation uuid,p_project uuid,p_action text)
RETURNS TABLE(installation_id uuid,tenant_id uuid,project_id uuid,principal_id uuid,permission_generation bigint,action text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,pg_temp AS $$
BEGIN
    IF length(p_digest)<>64 OR p_digest !~ '^[0-9a-f]{64}$' OR p_action NOT IN ('read','write','control') THEN
        RETURN;
    END IF;
    RETURN QUERY SELECT i.id,t.id,pr.id,p.id,gen.generation,p_action
        FROM auth.credentials c
        JOIN auth.principals p ON (p.tenant_id,p.id)=(c.tenant_id,c.principal_id)
        JOIN core.tenants t ON t.id=c.tenant_id
        JOIN core.installations i ON i.id=t.installation_id
        JOIN core.projects pr ON pr.tenant_id=t.id AND pr.id=p_project
        JOIN auth.project_grants g ON (g.tenant_id,g.project_id,g.principal_id)=(t.id,pr.id,p.id)
        JOIN auth.permission_generations gen ON (gen.tenant_id,gen.project_id)=(t.id,pr.id)
        WHERE c.key_digest=p_digest AND i.id=p_installation AND NOT p.disabled
          AND c.revoked_at IS NULL AND (c.expires_at IS NULL OR c.expires_at>clock_timestamp())
          AND (g.permissions && ARRAY['owner','admin']
               OR (p_action='read' AND 'read'=ANY(g.permissions))
               OR (p_action='write' AND 'write'=ANY(g.permissions)))
        FOR SHARE OF c,p,t,i,pr,g,gen;
END;
$$;

CREATE FUNCTION auth.has_access(p_tenant uuid,p_project uuid,p_need text,p_principal uuid DEFAULT NULL)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,pg_temp AS $$
DECLARE resolved record; configured_action text;
BEGIN
    configured_action:=current_setting('cortex.action',true);
    IF p_need NOT IN ('read','write') OR configured_action NOT IN ('read','write','control')
       OR (p_need='write' AND configured_action<>'write') THEN RETURN false; END IF;
    SELECT * INTO resolved FROM auth.resolve_scope(current_setting('cortex.credential_digest',true),
        NULLIF(current_setting('cortex.installation_id',true),'')::uuid,
        NULLIF(current_setting('cortex.project_id',true),'')::uuid,configured_action);
    IF NOT FOUND THEN RETURN false; END IF;
    IF resolved.tenant_id<>p_tenant OR resolved.project_id<>p_project
       OR (p_principal IS NOT NULL AND resolved.principal_id<>p_principal) THEN RETURN false; END IF;
    IF p_need='read' AND configured_action<>'read' THEN
        PERFORM 1 FROM auth.resolve_scope(current_setting('cortex.credential_digest',true),
            NULLIF(current_setting('cortex.installation_id',true),'')::uuid,
            NULLIF(current_setting('cortex.project_id',true),'')::uuid,'read');
        IF NOT FOUND THEN RETURN false; END IF;
    END IF;
    RETURN true;
EXCEPTION WHEN invalid_text_representation THEN RETURN false;
END;
$$;

CREATE FUNCTION auth.bump_generation(p_tenant uuid,p_project uuid)
RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,pg_temp AS $$
    INSERT INTO auth.permission_generations AS gen(tenant_id,project_id,generation)
        VALUES(p_tenant,p_project,1)
        ON CONFLICT(tenant_id,project_id) DO UPDATE SET generation=gen.generation+1;
$$;

CREATE FUNCTION auth.permission_changed() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,pg_temp AS $$
DECLARE old_tenant uuid; new_tenant uuid; old_principal uuid; new_principal uuid;
        old_project uuid; new_project uuid; item record;
BEGIN
    IF TG_OP<>'INSERT' THEN
        old_tenant:=OLD.tenant_id;
        IF TG_TABLE_NAME='principals' THEN old_principal:=OLD.id;
        ELSE old_principal:=OLD.principal_id; END IF;
        IF TG_TABLE_NAME='project_grants' THEN old_project:=OLD.project_id; END IF;
    END IF;
    IF TG_OP<>'DELETE' THEN
        new_tenant:=NEW.tenant_id;
        IF TG_TABLE_NAME='principals' THEN new_principal:=NEW.id;
        ELSE new_principal:=NEW.principal_id; END IF;
        IF TG_TABLE_NAME='project_grants' THEN new_project:=NEW.project_id; END IF;
    END IF;
    IF TG_TABLE_NAME='project_grants' THEN
        FOR item IN SELECT DISTINCT scope.tenant_id,scope.project_id
            FROM (VALUES(old_tenant,old_project),(new_tenant,new_project)) AS scope(tenant_id,project_id)
            WHERE scope.tenant_id IS NOT NULL AND scope.project_id IS NOT NULL LOOP
            PERFORM auth.bump_generation(item.tenant_id,item.project_id);
        END LOOP;
    ELSE
        FOR item IN SELECT DISTINCT g.tenant_id,g.project_id FROM auth.project_grants g
            WHERE (g.tenant_id,g.principal_id)=(old_tenant,old_principal)
               OR (g.tenant_id,g.principal_id)=(new_tenant,new_principal) LOOP
            PERFORM auth.bump_generation(item.tenant_id,item.project_id);
        END LOOP;
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER grants_permission_changed AFTER INSERT OR UPDATE OR DELETE ON auth.project_grants
    FOR EACH ROW EXECUTE FUNCTION auth.permission_changed();
CREATE TRIGGER credentials_permission_changed AFTER INSERT OR UPDATE OR DELETE ON auth.credentials
    FOR EACH ROW EXECUTE FUNCTION auth.permission_changed();
CREATE TRIGGER principals_permission_changed AFTER UPDATE OR DELETE ON auth.principals
    FOR EACH ROW EXECUTE FUNCTION auth.permission_changed();

GRANT SELECT,UPDATE ON core.installations,core.tenants,core.projects TO "kaidera-runtime-core-verifier";
GRANT SELECT,UPDATE ON ALL TABLES IN SCHEMA auth TO "kaidera-runtime-core-verifier";
GRANT INSERT ON auth.permission_generations TO "kaidera-runtime-core-verifier";
-- Ownership transfer requires temporary CREATE; runtime verifier has none afterwards.
GRANT CREATE ON SCHEMA auth TO "kaidera-runtime-core-verifier";
ALTER FUNCTION auth.resolve_scope(text,uuid,uuid,text) OWNER TO "kaidera-runtime-core-verifier";
ALTER FUNCTION auth.has_access(uuid,uuid,text,uuid) OWNER TO "kaidera-runtime-core-verifier";
ALTER FUNCTION auth.bump_generation(uuid,uuid) OWNER TO "kaidera-runtime-core-verifier";
ALTER FUNCTION auth.permission_changed() OWNER TO "kaidera-runtime-core-verifier";
REVOKE CREATE ON SCHEMA auth FROM "kaidera-runtime-core-verifier";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA auth FROM PUBLIC,"kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION auth.resolve_scope(text,uuid,uuid,text),auth.has_access(uuid,uuid,text,uuid)
    TO "kaidera-runtime-core-request";

DO $$
DECLARE item record; private_metadata boolean; principal_arg text;
BEGIN
    FOR item IN SELECT n.nspname,c.relname FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname IN ('core','auth','coordination','retrieval') AND c.relkind='r'
          AND NOT (n.nspname='core' AND c.relname='schema_migrations') LOOP
        EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY',item.nspname,item.relname);
        EXECUTE format('ALTER TABLE %I.%I FORCE ROW LEVEL SECURITY',item.nspname,item.relname);
        private_metadata:=item.nspname='auth' OR (item.nspname='core' AND item.relname IN ('installations','tenants','projects'));
        IF private_metadata THEN
            EXECUTE format('CREATE POLICY c04_verifier_lookup ON %I.%I FOR ALL TO "kaidera-runtime-core-verifier" USING(true) WITH CHECK(true)',item.nspname,item.relname);
        ELSIF EXISTS(SELECT 1 FROM information_schema.columns
                     WHERE table_schema=item.nspname AND table_name=item.relname AND column_name='tenant_id')
          AND EXISTS(SELECT 1 FROM information_schema.columns
                     WHERE table_schema=item.nspname AND table_name=item.relname AND column_name='project_id') THEN
            principal_arg:=CASE WHEN item.nspname='coordination' AND item.relname='idempotency' THEN ',principal_id' ELSE '' END;
            EXECUTE format('GRANT SELECT ON %I.%I TO "kaidera-runtime-core-request"',item.nspname,item.relname);
            EXECUTE format('CREATE POLICY c04_read ON %I.%I FOR SELECT TO "kaidera-runtime-core-request" USING(auth.has_access(tenant_id,project_id,''read''%s))',item.nspname,item.relname,principal_arg);
            IF NOT (item.nspname='coordination' AND item.relname IN ('published_events','quarantine')) THEN
                EXECUTE format('GRANT INSERT,UPDATE ON %I.%I TO "kaidera-runtime-core-request"',item.nspname,item.relname);
                EXECUTE format('CREATE POLICY c04_insert ON %I.%I FOR INSERT TO "kaidera-runtime-core-request" WITH CHECK(auth.has_access(tenant_id,project_id,''write''%s))',item.nspname,item.relname,principal_arg);
                EXECUTE format('CREATE POLICY c04_update ON %I.%I FOR UPDATE TO "kaidera-runtime-core-request" USING(auth.has_access(tenant_id,project_id,''write''%s)) WITH CHECK(auth.has_access(tenant_id,project_id,''write''%s))',item.nspname,item.relname,principal_arg,principal_arg);
            END IF;
        END IF;
    END LOOP;
END;
$$;
