-- CX-GAP-2a-ii: installer-direct, one-use first console identity.
CREATE FUNCTION auth.first_identity(
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

    RETURN jsonb_build_object('principal_id',p_principal,'credential_id',p_credential,
                              'project_id',p_project,'replayed',false);
END;
$$;
ALTER FUNCTION auth.first_identity(uuid,uuid,uuid,uuid,uuid,text,text)
    OWNER TO "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION auth.first_identity(uuid,uuid,uuid,uuid,uuid,text,text)
    FROM PUBLIC,"kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION auth.first_identity(uuid,uuid,uuid,uuid,uuid,text,text)
    TO "kaidera-runtime-core-installer";
