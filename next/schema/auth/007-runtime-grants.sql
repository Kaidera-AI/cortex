-- PG-B INCOMPLETE DRAFT: NOT MANIFESTED, NOT AN ADMITTED PREFIX20.
-- Sole future runtime-grants-0001. PG-B must complete six private ports,
-- finite-owner phase/binding client, service ACL closure/validation and the
-- final manifest append. PG-C seals the final20/44/73 compatibility contract.
-- No role names, OIDs, credentials, HBA or production authority are created.
-- Owner phases must admit the exact current DB and protected role inputs first.

CREATE TABLE auth.runtime_login_bindings (
    role_oid oid PRIMARY KEY,
    role_name name NOT NULL UNIQUE,
    logical_role text NOT NULL CHECK (logical_role IN
        ('api_request','probe','supervisor','observer','installer_enrollment')),
    installation_id uuid NOT NULL REFERENCES core.installations(id),
    active boolean NOT NULL,
    revision bigint NOT NULL CHECK (revision>0),
    operation_id uuid NOT NULL,
    database_oid oid NOT NULL,
    database_owner_oid oid NOT NULL,
    embedding_job_kind text CHECK (embedding_job_kind IS NULL OR
        length(embedding_job_kind) BETWEEN 1 AND 256)
);
CREATE TABLE coordination.supervisor_leases (
    installation_id uuid PRIMARY KEY REFERENCES core.installations(id),
    holder uuid NOT NULL,
    fence bigint NOT NULL CHECK (fence>0),
    expires_at timestamptz NOT NULL
);
CREATE TRIGGER supervisor_fence_monotonic BEFORE UPDATE ON coordination.supervisor_leases
    FOR EACH ROW EXECUTE FUNCTION coordination.refuse_fence_regression();

GRANT CREATE ON SCHEMA auth,coordination TO "kaidera-runtime-core-verifier";
ALTER TABLE auth.runtime_login_bindings OWNER TO "kaidera-runtime-core-verifier";
ALTER TABLE coordination.supervisor_leases OWNER TO "kaidera-runtime-core-verifier";
ALTER TABLE auth.runtime_login_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.runtime_login_bindings FORCE ROW LEVEL SECURITY;
ALTER TABLE coordination.supervisor_leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE coordination.supervisor_leases FORCE ROW LEVEL SECURITY;
CREATE POLICY runtime_private_owner ON auth.runtime_login_bindings
    TO "kaidera-runtime-core-verifier" USING(true) WITH CHECK(true);
CREATE POLICY runtime_private_owner ON coordination.supervisor_leases
    TO "kaidera-runtime-core-verifier" USING(true) WITH CHECK(true);
REVOKE ALL ON auth.runtime_login_bindings,coordination.supervisor_leases
    FROM PUBLIC,"kaidera-runtime-core-request";

-- This internal helper returns no binding for stale/missing/unsafe authority.
-- session_user, including its actual OID, is the authority-bearing login;
-- The backend's authenticated login OID must ALSO match session_user's name.
-- current_user is the definer and a caller's GUC is never an identity input.
-- Volatile entry/final checks use fresh READ COMMITTED snapshots. PG-B must
-- take the protected map row lock BEFORE every writable lease row lock.
CREATE FUNCTION auth.runtime_bound_installation(p_kind text) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE actual record; binding auth.runtime_login_bindings%ROWTYPE;
        request_oid oid; verifier_oid oid; owned_database record;
BEGIN
    IF p_kind IS NULL OR p_kind NOT IN
        ('api_request','probe','supervisor','observer','installer_enrollment')
       OR current_setting('transaction_isolation')<>'read committed' THEN RETURN NULL; END IF;
    SELECT r.* INTO actual FROM pg_catalog.pg_roles r
      WHERE r.oid=(SELECT usesysid FROM pg_catalog.pg_stat_activity WHERE pid=pg_backend_pid())
        AND r.rolname=session_user;
    IF NOT FOUND OR NOT actual.rolcanlogin OR actual.rolsuper OR actual.rolbypassrls
       OR actual.rolcreatedb OR actual.rolcreaterole OR actual.rolreplication
       OR actual.rolinherit THEN RETURN NULL; END IF;
    SELECT * INTO binding FROM auth.runtime_login_bindings
      WHERE role_oid=actual.oid AND role_name=actual.rolname
        AND logical_role=p_kind AND active AND revision>0;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT oid,datdba INTO owned_database FROM pg_catalog.pg_database
      WHERE datname=current_database();
    IF binding.database_oid IS DISTINCT FROM owned_database.oid
       OR binding.database_owner_oid IS DISTINCT FROM owned_database.datdba
       OR actual.oid=owned_database.datdba
       OR NOT EXISTS(SELECT 1 FROM core.installations WHERE id=binding.installation_id)
       OR EXISTS(SELECT 1 FROM pg_catalog.pg_namespace n
          WHERE n.nspname IN ('core','auth','coordination','retrieval') AND n.nspowner=actual.oid)
       OR EXISTS(SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n
          ON n.oid=c.relnamespace WHERE n.nspname IN ('core','auth','coordination','retrieval')
          AND c.relowner=actual.oid) THEN RETURN NULL; END IF;
    SELECT oid INTO request_oid FROM pg_catalog.pg_roles WHERE rolname='kaidera-runtime-core-request';
    SELECT oid INTO verifier_oid FROM pg_catalog.pg_roles WHERE rolname='kaidera-runtime-core-verifier';
    IF request_oid IS NULL OR verifier_oid IS NULL
       OR EXISTS(SELECT 1 FROM pg_catalog.pg_roles r WHERE r.oid IN (request_oid,verifier_oid)
          AND (r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreatedb
               OR r.rolcreaterole OR r.rolreplication OR r.rolinherit))
       OR EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members
          WHERE member IN (request_oid,verifier_oid) OR roleid=verifier_oid)
       OR EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members WHERE roleid=actual.oid) THEN RETURN NULL; END IF;
    IF p_kind='api_request' THEN
        IF (SELECT count(*) FROM pg_catalog.pg_auth_members WHERE member=actual.oid)<>1
           OR NOT EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members
              WHERE member=actual.oid AND roleid=request_oid AND set_option
                AND NOT inherit_option AND NOT admin_option) THEN RETURN NULL; END IF;
    ELSIF EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members WHERE member=actual.oid) THEN
        RETURN NULL;
    END IF;
    RETURN binding.installation_id;
END;
$$;
ALTER FUNCTION auth.runtime_bound_installation(text) OWNER TO "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION auth.runtime_bound_installation(text)
    FROM PUBLIC,"kaidera-runtime-core-request","kaidera-runtime-core-installer";

CREATE FUNCTION auth.runtime_request_installation(p_installation uuid) RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,pg_temp AS $$
    SELECT p_installation IS NOT NULL
       AND p_installation IS NOT DISTINCT FROM auth.runtime_bound_installation('api_request');
$$;
CREATE FUNCTION auth.runtime_tenant_installation(p_tenant uuid) RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,pg_temp AS $$
    SELECT EXISTS(SELECT 1 FROM core.tenants
        WHERE id=p_tenant AND installation_id=auth.runtime_bound_installation('api_request'));
$$;
ALTER FUNCTION auth.runtime_request_installation(uuid) OWNER TO "kaidera-runtime-core-verifier";
ALTER FUNCTION auth.runtime_tenant_installation(uuid) OWNER TO "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION auth.runtime_request_installation(uuid),auth.runtime_tenant_installation(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth.runtime_request_installation(uuid),auth.runtime_tenant_installation(uuid)
    TO "kaidera-runtime-core-request";
REVOKE CREATE ON SCHEMA auth,coordination FROM "kaidera-runtime-core-verifier";

-- Additive replacements retain all original credential/project/generation checks.
CREATE OR REPLACE FUNCTION auth.resolve_scope(p_digest text,p_installation uuid,p_project uuid,p_action text)
RETURNS TABLE(installation_id uuid,tenant_id uuid,project_id uuid,principal_id uuid,permission_generation bigint,action text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,pg_temp AS $$
BEGIN
    IF NOT auth.runtime_request_installation(p_installation) THEN RETURN; END IF;
    IF p_digest IS NULL OR p_action IS NULL OR length(p_digest)<>64 OR p_digest !~ '^[0-9a-f]{64}$' OR p_action NOT IN ('read','write','control') THEN
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

CREATE OR REPLACE FUNCTION auth.first_identity(
    p_installation uuid, p_tenant uuid, p_project uuid, p_principal uuid,
    p_credential uuid, p_digest text, p_key text
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE prior auth.first_identity_receipts%ROWTYPE;
        linked_project uuid;
        audit_body bytea;
        audit_payload uuid;
BEGIN
    -- Serialize a finite binding revocation before the installation lock/action.
    PERFORM 1 FROM auth.runtime_login_bindings WHERE role_name=session_user
      AND role_oid=(SELECT usesysid FROM pg_catalog.pg_stat_activity WHERE pid=pg_backend_pid())
      AND logical_role='installer_enrollment' AND installation_id=p_installation
      AND active FOR SHARE;
    IF NOT FOUND OR p_installation IS DISTINCT FROM auth.runtime_bound_installation('installer_enrollment') THEN
        RAISE EXCEPTION 'first_identity_forbidden' USING ERRCODE='42501';
    END IF;
    IF session_user IS DISTINCT FROM 'kaidera-runtime-core-installer'
       OR p_installation IS NULL OR p_tenant IS NULL OR p_project IS NULL
       OR p_principal IS NULL OR p_credential IS NULL OR p_digest IS NULL
       OR p_digest !~ '^[0-9a-f]{64}$'
       OR p_key IS NULL OR length(p_key) NOT BETWEEN 1 AND 256 THEN
        RAISE EXCEPTION 'first_identity_forbidden' USING ERRCODE='42501';
    END IF;

    -- The installation lock serializes distinct attempts, including their replay decision.
    PERFORM 1 FROM core.installations WHERE id=p_installation FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'first_identity_forbidden' USING ERRCODE='42501';
    END IF;
    SELECT * INTO prior FROM auth.first_identity_receipts
      WHERE installation_id=p_installation;
    IF FOUND THEN
        IF prior.tenant_id IS DISTINCT FROM p_tenant
           OR prior.project_id IS DISTINCT FROM p_project
           OR prior.principal_id IS DISTINCT FROM p_principal
           OR prior.credential_id IS DISTINCT FROM p_credential
           OR prior.key_digest IS DISTINCT FROM p_digest
           OR prior.request_key IS DISTINCT FROM p_key THEN
            RAISE EXCEPTION 'first_identity_forbidden' USING ERRCODE='42501';
        END IF;
        IF p_installation IS DISTINCT FROM auth.runtime_bound_installation('installer_enrollment') THEN
            RAISE EXCEPTION 'first_identity_forbidden' USING ERRCODE='42501';
        END IF;
        RETURN jsonb_build_object('principal_id',prior.principal_id,
            'credential_id',prior.credential_id,'project_id',prior.project_id,
            'replayed',true);
    END IF;

    SELECT p.id INTO linked_project FROM core.tenants t JOIN core.projects p
      ON p.tenant_id=t.id
      WHERE t.id=p_tenant AND t.installation_id=p_installation
        AND p.id=p_project AND p.name='default';
    IF NOT FOUND
       OR EXISTS (SELECT 1 FROM auth.principals p JOIN core.tenants t
           ON t.id=p.tenant_id WHERE t.installation_id=p_installation)
       OR EXISTS (SELECT 1 FROM core.record_aliases a JOIN core.tenants t
           ON t.id=a.tenant_id WHERE t.installation_id=p_installation
             AND a.source_namespace='cortex.core.identity')
       OR EXISTS (SELECT 1 FROM core.records r JOIN core.tenants t
           ON t.id=r.tenant_id WHERE t.installation_id=p_installation
             AND r.kind='core.identity') THEN
        RAISE EXCEPTION 'first_identity_forbidden' USING ERRCODE='42501';
    END IF;

    INSERT INTO auth.principals(tenant_id,id,name,kind,identity_adopted)
      VALUES(p_tenant,p_principal,'first-console','human',true);
    INSERT INTO auth.project_grants(tenant_id,project_id,principal_id,permissions,roles)
      VALUES(p_tenant,p_project,p_principal,ARRAY['read','write']::text[],ARRAY[]::text[]);
    INSERT INTO auth.credentials(tenant_id,id,principal_id,key_digest)
      VALUES(p_tenant,p_credential,p_principal,p_digest);
    INSERT INTO auth.first_identity_receipts(
        installation_id,tenant_id,project_id,principal_id,credential_id,key_digest,request_key)
      VALUES(p_installation,p_tenant,p_project,p_principal,p_credential,p_digest,p_key);

    audit_body:=convert_to(jsonb_build_object(
        'actor_kind','installer','actor','kaidera-runtime-core-installer',
        'operation','first_identity','principal_id',p_principal)::text,'UTF8');
    audit_payload:=gen_random_uuid();
    INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256)
      VALUES(p_tenant,p_project,audit_payload,audit_body,encode(sha256(audit_body),'hex'));
    PERFORM coordination.c06_fact('identity',p_principal,audit_payload);

    IF p_installation IS DISTINCT FROM auth.runtime_bound_installation('installer_enrollment') THEN
            RAISE EXCEPTION 'first_identity_forbidden' USING ERRCODE='42501';
        END IF;
        RETURN jsonb_build_object('principal_id',p_principal,'credential_id',p_credential,
                              'project_id',p_project,'replayed',false);
END;
$$;

CREATE OR REPLACE FUNCTION auth.enrollment_readiness(p_installation uuid)
RETURNS auth.enrollment_readiness_state
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,pg_temp AS $$
DECLARE default_count bigint;
        default_tenant uuid;
        default_project uuid;
        receipt record;
BEGIN
    IF NOT auth.runtime_request_installation(p_installation) THEN
        RETURN 'unavailable'::auth.enrollment_readiness_state;
    END IF;
    IF p_installation IS NULL OR NOT EXISTS (
        SELECT 1 FROM core.installations WHERE id=p_installation
    ) THEN RETURN 'unavailable'::auth.enrollment_readiness_state; END IF;

    SELECT count(*) INTO default_count FROM core.tenants t
      JOIN core.projects p ON p.tenant_id=t.id
      WHERE t.installation_id=p_installation AND p.name='default';
    IF default_count IS DISTINCT FROM 1 THEN
        RETURN 'unavailable'::auth.enrollment_readiness_state;
    END IF;
    SELECT t.id,p.id INTO default_tenant,default_project FROM core.tenants t
      JOIN core.projects p ON p.tenant_id=t.id
      WHERE t.installation_id=p_installation AND p.name='default';

    SELECT r.tenant_id,r.project_id,r.principal_id INTO receipt
      FROM auth.first_identity_receipts r WHERE r.installation_id=p_installation;
    IF NOT FOUND THEN
        IF EXISTS (SELECT 1 FROM auth.principals p JOIN core.tenants t
            ON t.id=p.tenant_id WHERE t.installation_id=p_installation)
           OR EXISTS (SELECT 1 FROM core.record_aliases a JOIN core.tenants t
            ON t.id=a.tenant_id WHERE t.installation_id=p_installation
              AND a.source_namespace='cortex.core.identity')
           OR EXISTS (SELECT 1 FROM core.records r JOIN core.tenants t
            ON t.id=r.tenant_id WHERE t.installation_id=p_installation
              AND r.kind='core.identity') THEN
            RETURN 'unavailable'::auth.enrollment_readiness_state;
        END IF;
        IF NOT auth.runtime_request_installation(p_installation) THEN
            RETURN 'unavailable'::auth.enrollment_readiness_state;
        END IF;
        RETURN 'enrollment_required'::auth.enrollment_readiness_state;
    END IF;

    IF receipt.tenant_id IS DISTINCT FROM default_tenant
       OR receipt.project_id IS DISTINCT FROM default_project
       OR NOT EXISTS (
           SELECT 1 FROM auth.principals p JOIN auth.project_grants g
             ON (g.tenant_id,g.principal_id)=(p.tenant_id,p.id)
           WHERE (p.tenant_id,p.id)=(receipt.tenant_id,receipt.principal_id)
             AND p.kind='human' AND p.identity_adopted IS TRUE
             AND g.project_id=default_project
             AND g.permissions @> ARRAY['read','write']::text[]
             AND cardinality(g.permissions)=2
             AND g.roles=ARRAY[]::text[]
       ) THEN RETURN 'unavailable'::auth.enrollment_readiness_state; END IF;
    IF NOT auth.runtime_request_installation(p_installation) THEN
            RETURN 'unavailable'::auth.enrollment_readiness_state;
        END IF;
        RETURN 'ready'::auth.enrollment_readiness_state;
END;
$$;

-- Restrictive installation gates AND the inherited permissive C04 policies.
-- No caller-selected installation string creates tenant authority.
DO $$
DECLARE item record;
BEGIN
    FOR item IN SELECT n.nspname,c.relname FROM pg_catalog.pg_class c
      JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname IN ('core','auth','coordination','retrieval') AND c.relkind='r'
        AND EXISTS(SELECT 1 FROM pg_catalog.pg_attribute a
          WHERE a.attrelid=c.oid AND a.attname='tenant_id' AND a.attnum>0 AND NOT a.attisdropped)
    LOOP
        EXECUTE format('CREATE POLICY runtime_installation_scope ON %I.%I AS RESTRICTIVE FOR ALL TO "kaidera-runtime-core-request" USING(auth.runtime_tenant_installation(tenant_id)) WITH CHECK(auth.runtime_tenant_installation(tenant_id))',item.nspname,item.relname);
    END LOOP;
END;
$$;

-- Explicit current/default PUBLIC closure. Restore only the reviewed request
-- helpers; ordinary service grants must be applied by the finite owner client.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO "kaidera-runtime-core-request";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA core,auth,coordination,retrieval FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA core,auth,coordination,retrieval FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA core,auth,coordination,retrieval FROM PUBLIC;
REVOKE ALL ON TYPE auth.enrollment_readiness_state FROM PUBLIC;
GRANT USAGE ON TYPE auth.enrollment_readiness_state TO "kaidera-runtime-core-verifier";
GRANT USAGE ON SCHEMA coordination TO "kaidera-runtime-core-verifier";

DO $$
DECLARE creator text; schema_name text;
BEGIN
    -- Original schema owners, current finite executor and admitted database owner
    -- are the actual creators in this operation, not guessed service identities.
    FOR creator IN SELECT DISTINCT role_name FROM (
        SELECT pg_catalog.pg_get_userbyid(nspowner) AS role_name FROM pg_catalog.pg_namespace
          WHERE nspname IN ('core','auth','coordination','retrieval')
        UNION ALL SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_catalog.pg_database
          WHERE datname=current_database()
        UNION ALL SELECT current_user::text
        UNION ALL SELECT 'kaidera-runtime-core-verifier') AS actual_creators
    LOOP
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON FUNCTIONS FROM PUBLIC',creator);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON TYPES FROM PUBLIC',creator);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON TABLES FROM PUBLIC',creator);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON SEQUENCES FROM PUBLIC',creator);
        FOREACH schema_name IN ARRAY ARRAY['core','auth','coordination','retrieval','public'] LOOP
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE ALL ON FUNCTIONS FROM PUBLIC',creator,schema_name);
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE ALL ON TYPES FROM PUBLIC',creator,schema_name);
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE ALL ON TABLES FROM PUBLIC',creator,schema_name);
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE ALL ON SEQUENCES FROM PUBLIC',creator,schema_name);
        END LOOP;
    END LOOP;
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC,"kaidera-runtime-core-request"',current_database());
    -- Narrow CONNECT renewal: one current owner-admitted DB only, no grant option.
    EXECUTE format('GRANT CONNECT,TEMP ON DATABASE %I TO "kaidera-runtime-core-verifier"',current_database());
END;
$$;

GRANT EXECUTE ON FUNCTION auth.bind_scope(text,uuid,uuid,text) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION auth.enrollment_readiness(uuid) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION auth.has_access(uuid,uuid,text,uuid) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION auth.identity_adopt(text,text,text,text) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION auth.identity_adoption_check(uuid,text) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION auth.identity_binding_valid() TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION auth.identity_lookup(uuid) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION auth.identity_register_agent(uuid,text,text[],text[],uuid,text,integer,text,text) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION auth.identity_register_human(uuid,text,text[],text[],uuid,text,integer,text,text,text) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION auth.identity_revoke(uuid,text,text) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION auth.identity_rotate(uuid,uuid,uuid,text,integer,text,text) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION auth.identity_set_roles(uuid,text[],text,text) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION auth.resolve_scope(text,uuid,uuid,text) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c05_record_head(uuid,text,text,bigint) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c05_record_payload(uuid,bigint) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c05_request(text,text) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c05_update_head(uuid,bigint,bigint,boolean) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c06_bootstrap() TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c06_record_event(uuid,bigint) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c06_record_reserved(uuid) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c06_reserved() TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c07_expire(text,bigint) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c07_poisoned(text,bigint,uuid) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c07_quarantined(text,bigint,uuid) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c07_record(text,bigint,bigint,uuid,text,text,jsonb) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c07_register(text,bigint,bigint,boolean) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c07_scan(text,bigint,bigint) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c07_scope() TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c07_status(text) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c11b3_session_claim(text,uuid,uuid) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c11b4_session_ids() TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c11b_request(text) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c11b_writer_matches(text) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.capture_job(uuid) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.outbox_page(bigint,integer) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.prune_outbox() TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.publish_outbox(integer) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION retrieval.finite_vector(real[]) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION auth.runtime_request_installation(uuid) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION auth.runtime_tenant_installation(uuid) TO "kaidera-runtime-core-request";

-- PG-B private lease and read ports. No role creation or per-install names.
-- Writable ports retain the protected map/revision lock before lease locks.
-- Read-only ports hold no map row lock: their final fresh revision check is
-- the publication linearization point. All refuse non-READ-COMMITTED use.

CREATE FUNCTION coordination.runtime_lease_acquire(p_installation uuid,p_holder uuid,p_ttl integer) RETURNS bigint
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE admitted auth.runtime_login_bindings%ROWTYPE;
        locked auth.runtime_login_bindings%ROWTYPE;
        final_binding auth.runtime_login_bindings%ROWTYPE;
        lease coordination.supervisor_leases%ROWTYPE;
        cutoff timestamptz := clock_timestamp()+interval '10 seconds';
        acquired boolean;
BEGIN
    IF p_ttl IS NULL OR p_ttl NOT BETWEEN 1 AND 10000 THEN
        RAISE EXCEPTION 'runtime_lease_ttl_invalid' USING ERRCODE='55000';
    END IF;

    IF p_installation IS NULL OR p_holder IS NULL
       OR p_installation IS DISTINCT FROM auth.runtime_bound_installation('supervisor') THEN
        RAISE EXCEPTION 'runtime_lease_forbidden' USING ERRCODE='42501';
    END IF;
    SELECT * INTO admitted FROM auth.runtime_login_bindings
      WHERE role_name=session_user AND logical_role='supervisor' AND active
        AND role_oid=(SELECT usesysid FROM pg_catalog.pg_stat_activity WHERE pid=pg_backend_pid())
        AND installation_id=p_installation;
    IF NOT FOUND THEN RAISE EXCEPTION 'runtime_lease_forbidden' USING ERRCODE='42501'; END IF;
    SELECT * INTO locked FROM auth.runtime_login_bindings WHERE role_oid=admitted.role_oid FOR SHARE;
    IF locked IS DISTINCT FROM admitted
       OR p_installation IS DISTINCT FROM auth.runtime_bound_installation('supervisor') THEN
        RAISE EXCEPTION 'runtime_binding_changed' USING ERRCODE='42501';
    END IF;
    -- ON CONFLICT serializes an initially absent row while preserving its
    -- retained fence. Failure rolls back this speculative insert atomically.
    INSERT INTO coordination.supervisor_leases(installation_id,holder,fence,expires_at)
      VALUES(p_installation,p_holder,1,clock_timestamp()+p_ttl*interval '1 millisecond')
      ON CONFLICT(installation_id) DO NOTHING;
    acquired:=FOUND;
    SELECT * INTO lease FROM coordination.supervisor_leases
      WHERE installation_id=p_installation FOR UPDATE;
    IF NOT FOUND OR clock_timestamp()>=cutoff THEN
        RAISE EXCEPTION 'runtime_lease_unavailable' USING ERRCODE='55000';
    END IF;
    IF NOT acquired THEN
        IF lease.expires_at>clock_timestamp() OR lease.fence=9223372036854775807 THEN
            RAISE EXCEPTION 'runtime_lease_busy' USING ERRCODE='55000';
        END IF;
        UPDATE coordination.supervisor_leases SET holder=p_holder,fence=fence+1,
            expires_at=clock_timestamp()+p_ttl*interval '1 millisecond'
          WHERE installation_id=p_installation RETURNING * INTO lease;
    END IF;

    SELECT * INTO final_binding FROM auth.runtime_login_bindings WHERE role_oid=admitted.role_oid;
    IF final_binding IS DISTINCT FROM admitted
       OR p_installation IS DISTINCT FROM auth.runtime_bound_installation('supervisor') THEN
        RAISE EXCEPTION 'runtime_binding_changed' USING ERRCODE='42501';
    END IF;
    IF clock_timestamp()>=cutoff OR lease.expires_at<=clock_timestamp() THEN
        RAISE EXCEPTION 'runtime_lease_expired' USING ERRCODE='55000';
    END IF;
    RETURN lease.fence;
END;
$$;

CREATE FUNCTION coordination.runtime_lease_renew(p_installation uuid,p_holder uuid,p_fence bigint,p_ttl integer) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE admitted auth.runtime_login_bindings%ROWTYPE;
        locked auth.runtime_login_bindings%ROWTYPE;
        final_binding auth.runtime_login_bindings%ROWTYPE;
        lease coordination.supervisor_leases%ROWTYPE;
        cutoff timestamptz := clock_timestamp()+interval '10 seconds';
        acquired boolean;
BEGIN
    IF p_ttl IS NULL OR p_ttl NOT BETWEEN 1 AND 10000 THEN
        RAISE EXCEPTION 'runtime_lease_ttl_invalid' USING ERRCODE='55000';
    END IF;
    IF p_fence IS NULL OR p_fence<=0 THEN
        RAISE EXCEPTION 'runtime_lease_token_invalid' USING ERRCODE='55000';
    END IF;

    IF p_installation IS NULL OR p_holder IS NULL
       OR p_installation IS DISTINCT FROM auth.runtime_bound_installation('supervisor') THEN
        RAISE EXCEPTION 'runtime_lease_forbidden' USING ERRCODE='42501';
    END IF;
    SELECT * INTO admitted FROM auth.runtime_login_bindings
      WHERE role_name=session_user AND logical_role='supervisor' AND active
        AND role_oid=(SELECT usesysid FROM pg_catalog.pg_stat_activity WHERE pid=pg_backend_pid())
        AND installation_id=p_installation;
    IF NOT FOUND THEN RAISE EXCEPTION 'runtime_lease_forbidden' USING ERRCODE='42501'; END IF;
    SELECT * INTO locked FROM auth.runtime_login_bindings WHERE role_oid=admitted.role_oid FOR SHARE;
    IF locked IS DISTINCT FROM admitted
       OR p_installation IS DISTINCT FROM auth.runtime_bound_installation('supervisor') THEN
        RAISE EXCEPTION 'runtime_binding_changed' USING ERRCODE='42501';
    END IF;
    SELECT * INTO lease FROM coordination.supervisor_leases
      WHERE installation_id=p_installation FOR UPDATE;
    IF NOT FOUND OR clock_timestamp()>=cutoff THEN
        RAISE EXCEPTION 'runtime_lease_unavailable' USING ERRCODE='55000';
    END IF;
    IF lease.holder IS DISTINCT FROM p_holder OR lease.fence IS DISTINCT FROM p_fence
       OR lease.expires_at<=clock_timestamp() THEN
        RAISE EXCEPTION 'runtime_lease_not_live' USING ERRCODE='55000';
    END IF;
    UPDATE coordination.supervisor_leases
      SET expires_at=clock_timestamp()+p_ttl*interval '1 millisecond'
      WHERE installation_id=p_installation RETURNING * INTO lease;

    SELECT * INTO final_binding FROM auth.runtime_login_bindings WHERE role_oid=admitted.role_oid;
    IF final_binding IS DISTINCT FROM admitted
       OR p_installation IS DISTINCT FROM auth.runtime_bound_installation('supervisor') THEN
        RAISE EXCEPTION 'runtime_binding_changed' USING ERRCODE='42501';
    END IF;
    IF clock_timestamp()>=cutoff OR lease.expires_at<=clock_timestamp() THEN
        RAISE EXCEPTION 'runtime_lease_expired' USING ERRCODE='55000';
    END IF;
    RETURN true;
END;
$$;

CREATE FUNCTION coordination.runtime_lease_release(p_installation uuid,p_holder uuid,p_fence bigint) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE admitted auth.runtime_login_bindings%ROWTYPE;
        locked auth.runtime_login_bindings%ROWTYPE;
        final_binding auth.runtime_login_bindings%ROWTYPE;
        lease coordination.supervisor_leases%ROWTYPE;
        cutoff timestamptz := clock_timestamp()+interval '10 seconds';
        acquired boolean;
BEGIN
    IF p_fence IS NULL OR p_fence<=0 THEN
        RAISE EXCEPTION 'runtime_lease_token_invalid' USING ERRCODE='55000';
    END IF;

    IF p_installation IS NULL OR p_holder IS NULL
       OR p_installation IS DISTINCT FROM auth.runtime_bound_installation('supervisor') THEN
        RAISE EXCEPTION 'runtime_lease_forbidden' USING ERRCODE='42501';
    END IF;
    SELECT * INTO admitted FROM auth.runtime_login_bindings
      WHERE role_name=session_user AND logical_role='supervisor' AND active
        AND role_oid=(SELECT usesysid FROM pg_catalog.pg_stat_activity WHERE pid=pg_backend_pid())
        AND installation_id=p_installation;
    IF NOT FOUND THEN RAISE EXCEPTION 'runtime_lease_forbidden' USING ERRCODE='42501'; END IF;
    SELECT * INTO locked FROM auth.runtime_login_bindings WHERE role_oid=admitted.role_oid FOR SHARE;
    IF locked IS DISTINCT FROM admitted
       OR p_installation IS DISTINCT FROM auth.runtime_bound_installation('supervisor') THEN
        RAISE EXCEPTION 'runtime_binding_changed' USING ERRCODE='42501';
    END IF;
    SELECT * INTO lease FROM coordination.supervisor_leases
      WHERE installation_id=p_installation FOR UPDATE;
    IF NOT FOUND OR clock_timestamp()>=cutoff THEN
        RAISE EXCEPTION 'runtime_lease_unavailable' USING ERRCODE='55000';
    END IF;
    IF lease.holder IS DISTINCT FROM p_holder OR lease.fence IS DISTINCT FROM p_fence
       OR lease.expires_at<=clock_timestamp() THEN
        RAISE EXCEPTION 'runtime_lease_not_live' USING ERRCODE='55000';
    END IF;
    UPDATE coordination.supervisor_leases SET expires_at=clock_timestamp()
      WHERE installation_id=p_installation;

    SELECT * INTO final_binding FROM auth.runtime_login_bindings WHERE role_oid=admitted.role_oid;
    IF final_binding IS DISTINCT FROM admitted
       OR p_installation IS DISTINCT FROM auth.runtime_bound_installation('supervisor') THEN
        RAISE EXCEPTION 'runtime_binding_changed' USING ERRCODE='42501';
    END IF;
    IF clock_timestamp()>=cutoff THEN
        RAISE EXCEPTION 'runtime_lease_expired' USING ERRCODE='55000';
    END IF;
    RETURN true;
END;
$$;

CREATE FUNCTION coordination.runtime_lease_assert_live(p_installation uuid,p_holder uuid,p_fence bigint) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE admitted auth.runtime_login_bindings%ROWTYPE;
        locked auth.runtime_login_bindings%ROWTYPE;
        final_binding auth.runtime_login_bindings%ROWTYPE;
        lease coordination.supervisor_leases%ROWTYPE;
        cutoff timestamptz := clock_timestamp()+interval '10 seconds';
        acquired boolean;
BEGIN
    IF p_fence IS NULL OR p_fence<=0 THEN
        RAISE EXCEPTION 'runtime_lease_token_invalid' USING ERRCODE='55000';
    END IF;

    IF p_installation IS NULL OR p_holder IS NULL
       OR p_installation IS DISTINCT FROM auth.runtime_bound_installation('supervisor') THEN
        RAISE EXCEPTION 'runtime_lease_forbidden' USING ERRCODE='42501';
    END IF;
    SELECT * INTO admitted FROM auth.runtime_login_bindings
      WHERE role_name=session_user AND logical_role='supervisor' AND active
        AND role_oid=(SELECT usesysid FROM pg_catalog.pg_stat_activity WHERE pid=pg_backend_pid())
        AND installation_id=p_installation;
    IF NOT FOUND THEN RAISE EXCEPTION 'runtime_lease_forbidden' USING ERRCODE='42501'; END IF;
    SELECT * INTO locked FROM auth.runtime_login_bindings WHERE role_oid=admitted.role_oid FOR SHARE;
    IF locked IS DISTINCT FROM admitted
       OR p_installation IS DISTINCT FROM auth.runtime_bound_installation('supervisor') THEN
        RAISE EXCEPTION 'runtime_binding_changed' USING ERRCODE='42501';
    END IF;
    SELECT * INTO lease FROM coordination.supervisor_leases
      WHERE installation_id=p_installation FOR UPDATE;
    IF NOT FOUND OR clock_timestamp()>=cutoff THEN
        RAISE EXCEPTION 'runtime_lease_unavailable' USING ERRCODE='55000';
    END IF;
    IF lease.holder IS DISTINCT FROM p_holder OR lease.fence IS DISTINCT FROM p_fence
       OR lease.expires_at<=clock_timestamp() THEN
        RAISE EXCEPTION 'runtime_lease_not_live' USING ERRCODE='55000';
    END IF;

    SELECT * INTO final_binding FROM auth.runtime_login_bindings WHERE role_oid=admitted.role_oid;
    IF final_binding IS DISTINCT FROM admitted
       OR p_installation IS DISTINCT FROM auth.runtime_bound_installation('supervisor') THEN
        RAISE EXCEPTION 'runtime_binding_changed' USING ERRCODE='42501';
    END IF;
    IF clock_timestamp()>=cutoff OR lease.expires_at<=clock_timestamp() THEN
        RAISE EXCEPTION 'runtime_lease_expired' USING ERRCODE='55000';
    END IF;
    RETURN true;
END;
$$;

CREATE FUNCTION coordination.runtime_lease_health(p_installation uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE admitted auth.runtime_login_bindings%ROWTYPE;
        final_binding auth.runtime_login_bindings%ROWTYPE;
        lease coordination.supervisor_leases%ROWTYPE;
        cutoff timestamptz := clock_timestamp()+interval '10 seconds';
        value jsonb;
BEGIN
    IF p_installation IS NULL OR current_setting('transaction_isolation')<>'read committed' THEN
        RAISE EXCEPTION 'runtime_read_forbidden' USING ERRCODE='42501';
    END IF;
    SELECT * INTO admitted FROM auth.runtime_login_bindings
      WHERE role_name=session_user AND active AND installation_id=p_installation
        AND role_oid=(SELECT usesysid FROM pg_catalog.pg_stat_activity WHERE pid=pg_backend_pid())
        AND logical_role IN ('supervisor','observer');
    IF NOT FOUND OR p_installation IS DISTINCT FROM auth.runtime_bound_installation(admitted.logical_role) THEN
        RAISE EXCEPTION 'runtime_read_forbidden' USING ERRCODE='42501';
    END IF;

    SELECT * INTO lease FROM coordination.supervisor_leases WHERE installation_id=p_installation;
    IF NOT FOUND THEN value:=jsonb_build_object('state','absent','fence',NULL);
    ELSE value:=jsonb_build_object('state',CASE WHEN lease.expires_at>clock_timestamp()
              THEN 'live' ELSE 'expired' END,'fence',lease.fence); END IF;
    SELECT * INTO final_binding FROM auth.runtime_login_bindings WHERE role_oid=admitted.role_oid;
    IF final_binding IS DISTINCT FROM admitted
       OR p_installation IS DISTINCT FROM auth.runtime_bound_installation(admitted.logical_role) THEN
        RAISE EXCEPTION 'runtime_read_binding_changed' USING ERRCODE='42501';
    END IF;
    IF clock_timestamp()>=cutoff THEN
        RAISE EXCEPTION 'runtime_read_timeout' USING ERRCODE='55000';
    END IF;
    -- Re-evaluate the clock after the final authority check, without a job query.
    IF lease.installation_id IS NOT NULL THEN
        value:=jsonb_build_object('state',CASE WHEN lease.expires_at>clock_timestamp()
              THEN 'live' ELSE 'expired' END,'fence',lease.fence);
    END IF;
    RETURN value;
END;
$$;
CREATE FUNCTION coordination.runtime_observe(p_installation uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE admitted auth.runtime_login_bindings%ROWTYPE;
        final_binding auth.runtime_login_bindings%ROWTYPE;
        cutoff timestamptz := clock_timestamp()+interval '10 seconds';
        size_bytes bigint; pending bigint; embedding_pending bigint; health jsonb;
BEGIN
    IF p_installation IS NULL OR current_setting('transaction_isolation')<>'read committed' THEN
        RAISE EXCEPTION 'runtime_read_forbidden' USING ERRCODE='42501';
    END IF;
    SELECT * INTO admitted FROM auth.runtime_login_bindings
      WHERE role_name=session_user AND active AND installation_id=p_installation
        AND role_oid=(SELECT usesysid FROM pg_catalog.pg_stat_activity WHERE pid=pg_backend_pid())
        AND logical_role ='observer';
    IF NOT FOUND OR p_installation IS DISTINCT FROM auth.runtime_bound_installation(admitted.logical_role) THEN
        RAISE EXCEPTION 'runtime_read_forbidden' USING ERRCODE='42501';
    END IF;

    IF admitted.embedding_job_kind IS NULL THEN
        RAISE EXCEPTION 'runtime_embedding_kind_unbound' USING ERRCODE='55000';
    END IF;
    BEGIN
        SELECT count(*) FILTER(WHERE j.state='pending'),
               count(*) FILTER(WHERE j.state='pending' AND j.kind=admitted.embedding_job_kind)
          INTO pending,embedding_pending FROM coordination.jobs j
          JOIN core.tenants t ON t.id=j.tenant_id WHERE t.installation_id=p_installation;
        size_bytes:=pg_catalog.pg_database_size(current_database());
        health:=coordination.runtime_lease_health(p_installation);
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'runtime_observe_unavailable' USING ERRCODE='55000';
    END;
    SELECT * INTO final_binding FROM auth.runtime_login_bindings WHERE role_oid=admitted.role_oid;
    IF final_binding IS DISTINCT FROM admitted
       OR p_installation IS DISTINCT FROM auth.runtime_bound_installation(admitted.logical_role) THEN
        RAISE EXCEPTION 'runtime_read_binding_changed' USING ERRCODE='42501';
    END IF;
    IF clock_timestamp()>=cutoff THEN
        RAISE EXCEPTION 'runtime_read_timeout' USING ERRCODE='55000';
    END IF;
    IF size_bytes IS NULL OR size_bytes<0 OR pending IS NULL OR embedding_pending IS NULL THEN
        RAISE EXCEPTION 'runtime_observe_unavailable' USING ERRCODE='55000';
    END IF;
    RETURN jsonb_build_object('database_bytes',size_bytes,'pending_jobs',pending,
             'pending_embedding_jobs',embedding_pending,'lease',health);
END;
$$;
ALTER FUNCTION coordination.runtime_lease_acquire(uuid,uuid,integer) OWNER TO "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION coordination.runtime_lease_acquire(uuid,uuid,integer) FROM PUBLIC,"kaidera-runtime-core-request","kaidera-runtime-core-installer";
ALTER FUNCTION coordination.runtime_lease_renew(uuid,uuid,bigint,integer) OWNER TO "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION coordination.runtime_lease_renew(uuid,uuid,bigint,integer) FROM PUBLIC,"kaidera-runtime-core-request","kaidera-runtime-core-installer";
ALTER FUNCTION coordination.runtime_lease_release(uuid,uuid,bigint) OWNER TO "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION coordination.runtime_lease_release(uuid,uuid,bigint) FROM PUBLIC,"kaidera-runtime-core-request","kaidera-runtime-core-installer";
ALTER FUNCTION coordination.runtime_lease_assert_live(uuid,uuid,bigint) OWNER TO "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION coordination.runtime_lease_assert_live(uuid,uuid,bigint) FROM PUBLIC,"kaidera-runtime-core-request","kaidera-runtime-core-installer";
ALTER FUNCTION coordination.runtime_lease_health(uuid) OWNER TO "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION coordination.runtime_lease_health(uuid) FROM PUBLIC,"kaidera-runtime-core-request","kaidera-runtime-core-installer";
ALTER FUNCTION coordination.runtime_observe(uuid) OWNER TO "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION coordination.runtime_observe(uuid) FROM PUBLIC,"kaidera-runtime-core-request","kaidera-runtime-core-installer";
