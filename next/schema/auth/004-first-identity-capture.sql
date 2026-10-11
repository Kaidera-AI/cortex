-- CX-GAP-2a-i: the sole installer first-identity scope and C06 capture branches.
DO $$
DECLARE installer record;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='kaidera-runtime-core-installer') THEN
        CREATE ROLE "kaidera-runtime-core-installer" LOGIN NOINHERIT NOSUPERUSER
            NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    SELECT * INTO installer FROM pg_roles WHERE rolname='kaidera-runtime-core-installer';
    IF NOT installer.rolcanlogin OR installer.rolinherit OR installer.rolsuper
       OR installer.rolcreatedb OR installer.rolcreaterole OR installer.rolreplication
       OR installer.rolbypassrls OR EXISTS (
           SELECT 1 FROM pg_auth_members WHERE member=installer.oid OR roleid=installer.oid
       ) THEN
        RAISE EXCEPTION 'unsafe first-identity installer role' USING ERRCODE='42501';
    END IF;
END;
$$;
GRANT USAGE ON SCHEMA auth TO "kaidera-runtime-core-installer";

CREATE TABLE auth.first_identity_receipts (
    installation_id uuid PRIMARY KEY REFERENCES core.installations(id),
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    credential_id uuid NOT NULL,
    key_digest text NOT NULL CHECK (key_digest ~ '^[0-9a-f]{64}$'),
    request_key text NOT NULL CHECK (length(request_key) BETWEEN 1 AND 256),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id,project_id) REFERENCES core.projects(tenant_id,id),
    FOREIGN KEY (tenant_id,principal_id) REFERENCES auth.principals(tenant_id,id),
    FOREIGN KEY (tenant_id,credential_id) REFERENCES auth.credentials(tenant_id,id)
);
ALTER TABLE auth.first_identity_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.first_identity_receipts FORCE ROW LEVEL SECURITY;
ALTER TABLE auth.first_identity_receipts OWNER TO "kaidera-runtime-core-verifier";
GRANT SELECT,INSERT ON auth.first_identity_receipts TO "kaidera-runtime-core-verifier";
CREATE POLICY first_identity_private ON auth.first_identity_receipts
    TO "kaidera-runtime-core-verifier" USING (true) WITH CHECK (true);

CREATE FUNCTION auth.first_identity_bootstrap_scope()
RETURNS TABLE(installation_id uuid,tenant_id uuid,project_id uuid,principal_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,pg_temp AS $$
BEGIN
    IF session_user <> 'kaidera-runtime-core-installer' THEN RETURN; END IF;
    RETURN QUERY SELECT r.installation_id,r.tenant_id,r.project_id,r.principal_id
      FROM auth.first_identity_receipts r
      JOIN core.tenants t ON (t.id,t.installation_id)=(r.tenant_id,r.installation_id)
      JOIN core.projects p ON (p.tenant_id,p.id)=(r.tenant_id,r.project_id)
      WHERE p.name='default' AND r.xmin::text=pg_current_xact_id()::text
        AND NOT EXISTS (
          SELECT 1 FROM core.record_aliases a JOIN core.tenants at ON at.id=a.tenant_id
          WHERE at.installation_id=r.installation_id
            AND a.source_namespace='cortex.core.identity'
            AND (a.external_id<>r.principal_id::text
                 OR a.xmin::text<>pg_current_xact_id()::text));
END;
$$;
ALTER FUNCTION auth.first_identity_bootstrap_scope()
    OWNER TO "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION auth.first_identity_bootstrap_scope()
    FROM PUBLIC,"kaidera-runtime-core-request","kaidera-runtime-core-installer";

CREATE OR REPLACE FUNCTION coordination.c06_fact(p_kind text,p_target uuid,p_payload uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; aggregate uuid; revision bigint; event uuid; bootstrap boolean:=false;
        audit jsonb;
BEGIN
    SELECT * INTO s FROM auth.identity_scope(false);
    IF NOT FOUND THEN
        SELECT * INTO s FROM auth.first_identity_bootstrap_scope();
        IF NOT FOUND OR p_kind<>'identity' OR p_target IS DISTINCT FROM s.principal_id
           OR p_payload IS NULL THEN RAISE EXCEPTION 'outbox_forbidden'; END IF;
        SELECT convert_from(p.body,'UTF8')::jsonb INTO audit FROM core.payloads p
          WHERE (p.tenant_id,p.project_id,p.id)=(s.tenant_id,s.project_id,p_payload)
            AND p.xmin::text=pg_current_xact_id()::text;
        IF audit IS NULL OR audit->>'actor_kind'<>'installer'
           OR audit->>'actor'<>'kaidera-runtime-core-installer'
           OR audit->>'operation'<>'first_identity'
           OR audit->>'principal_id'<>p_target::text
           OR audit - ARRAY['actor_kind','actor','operation','principal_id'] <> '{}'::jsonb
           THEN RAISE EXCEPTION 'outbox_forbidden'; END IF;
        bootstrap:=true;
    ELSIF s.action NOT IN ('write','control') OR p_kind NOT IN ('job','identity')
          OR p_target IS NULL OR p_payload IS NULL THEN
        RAISE EXCEPTION 'outbox_forbidden';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('c06:'||s.tenant_id||':'||s.project_id||':'||p_kind||':'||p_target,0));
    SELECT record_id INTO aggregate FROM core.record_aliases
      WHERE (tenant_id,project_id,source_namespace,external_id)=(s.tenant_id,s.project_id,'cortex.core.'||p_kind,p_target::text);
    IF bootstrap AND aggregate IS NOT NULL THEN RAISE EXCEPTION 'outbox_forbidden'; END IF;
    IF aggregate IS NULL THEN
        aggregate:=gen_random_uuid(); revision:=1;
        INSERT INTO core.records(tenant_id,project_id,id,kind,current_revision,tombstone)
          VALUES(s.tenant_id,s.project_id,aggregate,'core.'||p_kind,revision,false);
        INSERT INTO core.record_aliases VALUES(s.tenant_id,s.project_id,'cortex.core.'||p_kind,p_target::text,aggregate);
    ELSE
        UPDATE core.records SET current_revision=current_revision+1
          WHERE (tenant_id,project_id,id)=(s.tenant_id,s.project_id,aggregate) RETURNING current_revision INTO STRICT revision;
    END IF;
    INSERT INTO core.record_revisions(tenant_id,project_id,record_id,revision,payload_ref,tombstone)
      VALUES(s.tenant_id,s.project_id,aggregate,revision,p_payload,false);
    SELECT event_id INTO STRICT event FROM coordination.outbox
      WHERE (tenant_id,project_id,aggregate_id,aggregate_revision)=(s.tenant_id,s.project_id,aggregate,revision);
    RETURN event;
END;
$$;

CREATE OR REPLACE FUNCTION coordination.c06_capture_revision() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; k text; digest text; bootstrap boolean:=false;
BEGIN
    IF coordination.c06_bootstrap() THEN RETURN NEW; END IF;
    SELECT * INTO s FROM auth.identity_scope(false);
    IF NOT FOUND THEN
        SELECT * INTO s FROM auth.first_identity_bootstrap_scope();
        IF NOT FOUND OR NEW.tenant_id<>s.tenant_id OR NEW.project_id<>s.project_id
           OR NEW.revision<>1 OR NEW.tombstone THEN RAISE EXCEPTION 'outbox_forbidden'; END IF;
        IF NOT EXISTS (SELECT 1 FROM core.record_aliases a
            WHERE (a.tenant_id,a.project_id,a.source_namespace,a.external_id,a.record_id)=
              (s.tenant_id,s.project_id,'cortex.core.identity',s.principal_id::text,NEW.record_id)
              AND a.xmin::text=pg_current_xact_id()::text)
           OR NOT EXISTS (SELECT 1 FROM core.payloads p
            WHERE (p.tenant_id,p.project_id,p.id)=(s.tenant_id,s.project_id,NEW.payload_ref)
              AND p.xmin::text=pg_current_xact_id()::text)
           THEN RAISE EXCEPTION 'outbox_forbidden'; END IF;
        bootstrap:=true;
    ELSIF s.action NOT IN ('write','control')
          OR (s.tenant_id,s.project_id)<>(NEW.tenant_id,NEW.project_id) THEN
        RAISE EXCEPTION 'outbox_forbidden';
    END IF;
    SELECT kind INTO STRICT k FROM core.records WHERE (tenant_id,project_id,id)=
      (NEW.tenant_id,NEW.project_id,NEW.record_id);
    IF bootstrap AND k<>'core.identity' THEN RAISE EXCEPTION 'outbox_forbidden'; END IF;
    SELECT sha256 INTO STRICT digest FROM core.payloads WHERE (tenant_id,project_id,id)=
      (NEW.tenant_id,NEW.project_id,NEW.payload_ref);
    INSERT INTO coordination.outbox(event_id,installation_id,tenant_id,project_id,aggregate_id,aggregate_kind,
      aggregate_revision,operation,tombstone,schema_version,payload_ref,payload_sha256,occurred_at)
    VALUES(gen_random_uuid(),s.installation_id,NEW.tenant_id,NEW.project_id,NEW.record_id,k,NEW.revision,
      CASE WHEN NEW.tombstone THEN 'delete' ELSE 'upsert' END,NEW.tombstone,1,NEW.payload_ref,digest,clock_timestamp());
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION coordination.c06_identity_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; target uuid; bootstrap boolean:=false;
BEGIN
    IF coordination.c06_bootstrap() THEN RETURN NULL; END IF;
    SELECT * INTO s FROM auth.identity_scope(true);
    IF NOT FOUND THEN
        SELECT * INTO s FROM auth.first_identity_bootstrap_scope();
        IF NOT FOUND OR TG_OP<>'INSERT' THEN RAISE EXCEPTION 'identity_forbidden'; END IF;
        bootstrap:=true;
    END IF;
    IF TG_TABLE_NAME='principals' THEN target:=NEW.id; ELSE target:=NEW.principal_id; END IF;
    IF TG_TABLE_NAME='project_grants' THEN
        IF NEW.project_id<>s.project_id THEN RAISE EXCEPTION 'identity_forbidden'; END IF;
    END IF;
    IF bootstrap AND target<>s.principal_id THEN RAISE EXCEPTION 'identity_forbidden'; END IF;
    IF NEW.tenant_id<>s.tenant_id OR NOT EXISTS(SELECT 1 FROM core.record_aliases a JOIN coordination.outbox o
        ON(o.tenant_id,o.project_id,o.aggregate_id)=(a.tenant_id,a.project_id,a.record_id)
        WHERE (a.tenant_id,a.project_id,a.source_namespace,a.external_id)=(s.tenant_id,s.project_id,'cortex.core.identity',target::text)
          AND o.xmin::text=pg_current_xact_id()::text)
      THEN RAISE EXCEPTION 'identity mutation requires captured event' USING ERRCODE='23514'; END IF;
    RETURN NULL;
END;
$$;
