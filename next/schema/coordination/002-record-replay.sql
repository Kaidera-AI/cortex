-- C05 private mutation decisions. The public C04 read boundary is unchanged.
CREATE FUNCTION coordination.c05_scope()
RETURNS TABLE(installation_id uuid,tenant_id uuid,project_id uuid,principal_id uuid,permission_generation bigint,action text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE resolved record;
BEGIN
    IF current_setting('cortex.action',true) IS DISTINCT FROM 'write'
       OR NOT auth.context_is_bound(current_setting('cortex.credential_digest',true),
            NULLIF(current_setting('cortex.installation_id',true),'')::uuid,
            NULLIF(current_setting('cortex.project_id',true),'')::uuid,'write') THEN
        RAISE EXCEPTION 'private write context required' USING ERRCODE='42501';
    END IF;
    SELECT * INTO resolved FROM auth.resolve_scope(current_setting('cortex.credential_digest',true),
        NULLIF(current_setting('cortex.installation_id',true),'')::uuid,
        NULLIF(current_setting('cortex.project_id',true),'')::uuid,'write');
    IF NOT FOUND THEN RAISE EXCEPTION 'private write context required' USING ERRCODE='42501'; END IF;
    RETURN QUERY SELECT resolved.installation_id,resolved.tenant_id,resolved.project_id,
        resolved.principal_id,resolved.permission_generation,resolved.action;
EXCEPTION WHEN invalid_text_representation THEN
    RAISE EXCEPTION 'private write context required' USING ERRCODE='42501';
END;
$$;

CREATE FUNCTION coordination.c05_request(p_key text,p_digest text)
RETURNS TABLE(request_sha256 text,outcome text,receipt jsonb)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE scope record; saved record;
BEGIN
    SELECT * INTO STRICT scope FROM coordination.c05_scope();
    IF p_key IS NULL OR length(p_key) NOT BETWEEN 1 AND 256 OR p_digest IS NULL
       OR p_digest !~ '^[0-9a-f]{64}$' OR length(p_digest)<>64 THEN
        RAISE EXCEPTION 'request conflict' USING ERRCODE='23505';
    END IF;
    SELECT i.request_sha256,i.outcome,i.receipt INTO saved FROM coordination.idempotency i
        WHERE i.tenant_id=scope.tenant_id AND i.project_id=scope.project_id
          AND i.principal_id=scope.principal_id AND i.request_key=p_key;
    IF NOT FOUND THEN RETURN; END IF;
    IF saved.request_sha256 IS DISTINCT FROM p_digest OR saved.outcome IS DISTINCT FROM 'committed' THEN
        RAISE EXCEPTION 'request conflict' USING ERRCODE='23505';
    END IF;
    RETURN QUERY SELECT saved.request_sha256,saved.outcome,saved.receipt;
END;
$$;

CREATE FUNCTION coordination.c05_record_head(p_record uuid,p_operation text,p_kind text,p_expected bigint)
RETURNS TABLE(kind text,current_revision bigint)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE scope record; head record;
BEGIN
    SELECT * INTO STRICT scope FROM coordination.c05_scope();
    IF p_record IS NULL OR p_expected IS NULL OR p_expected<0 OR p_expected>=9223372036854775807
       OR p_operation IS NULL OR p_operation NOT IN ('put','delete')
       OR (p_operation='put' AND p_kind IS NULL) THEN
        RAISE EXCEPTION 'record conflict' USING ERRCODE='23505';
    END IF;
    SELECT r.kind,r.current_revision INTO head FROM core.records r
        WHERE r.tenant_id=scope.tenant_id AND r.project_id=scope.project_id AND r.id=p_record FOR UPDATE;
    IF NOT FOUND THEN
        IF p_expected<>0 OR p_operation='delete' THEN
            RAISE EXCEPTION 'record conflict' USING ERRCODE='23505';
        END IF;
        RETURN;
    END IF;
    IF head.current_revision<>p_expected OR (p_operation='put' AND head.kind IS DISTINCT FROM p_kind) THEN
        RAISE EXCEPTION 'record conflict' USING ERRCODE='23505';
    END IF;
    RETURN QUERY SELECT head.kind,head.current_revision;
END;
$$;

CREATE FUNCTION coordination.c05_record_payload(p_record uuid,p_expected bigint)
RETURNS TABLE(payload_id uuid,payload_sha256 text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE scope record; head record;
BEGIN
    SELECT * INTO STRICT scope FROM coordination.c05_scope();
    SELECT r.current_revision INTO head FROM core.records r
        WHERE r.tenant_id=scope.tenant_id AND r.project_id=scope.project_id AND r.id=p_record FOR UPDATE;
    IF NOT FOUND OR p_expected IS NULL OR head.current_revision<>p_expected OR p_expected<1 THEN
        RAISE EXCEPTION 'record conflict' USING ERRCODE='23505';
    END IF;
    RETURN QUERY SELECT p.id,p.sha256 FROM core.record_revisions v JOIN core.payloads p
        ON (p.tenant_id,p.project_id,p.id)=(v.tenant_id,v.project_id,v.payload_ref)
        WHERE v.tenant_id=scope.tenant_id AND v.project_id=scope.project_id
          AND v.record_id=p_record AND v.revision=p_expected;
    IF NOT FOUND THEN RAISE EXCEPTION 'record conflict' USING ERRCODE='23505'; END IF;
END;
$$;

CREATE FUNCTION coordination.c05_update_head(p_record uuid,p_expected bigint,p_revision bigint,p_tombstone boolean)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE scope record; head record;
BEGIN
    SELECT * INTO STRICT scope FROM coordination.c05_scope();
    SELECT r.current_revision INTO head FROM core.records r
        WHERE r.tenant_id=scope.tenant_id AND r.project_id=scope.project_id AND r.id=p_record FOR UPDATE;
    IF NOT FOUND OR p_expected IS NULL OR p_revision IS NULL OR p_tombstone IS NULL
       OR head.current_revision<>p_expected OR p_expected<1 OR p_expected>=9223372036854775807
       OR p_revision<>p_expected+1 THEN
        RAISE EXCEPTION 'record conflict' USING ERRCODE='23505';
    END IF;
    UPDATE core.records r SET current_revision=p_revision,tombstone=p_tombstone
        WHERE r.tenant_id=scope.tenant_id AND r.project_id=scope.project_id AND r.id=p_record;
END;
$$;

GRANT USAGE ON SCHEMA coordination TO "kaidera-runtime-core-verifier";
GRANT SELECT ON core.records,core.record_revisions,core.payloads,coordination.idempotency TO "kaidera-runtime-core-verifier";
GRANT UPDATE ON core.records TO "kaidera-runtime-core-verifier";
CREATE POLICY c05_verifier_records_read ON core.records FOR SELECT TO "kaidera-runtime-core-verifier" USING (true);
CREATE POLICY c05_verifier_records_update ON core.records FOR UPDATE TO "kaidera-runtime-core-verifier" USING (true) WITH CHECK (true);
CREATE POLICY c05_verifier_revisions_read ON core.record_revisions FOR SELECT TO "kaidera-runtime-core-verifier" USING (true);
CREATE POLICY c05_verifier_payloads_read ON core.payloads FOR SELECT TO "kaidera-runtime-core-verifier" USING (true);
CREATE POLICY c05_verifier_requests_read ON coordination.idempotency FOR SELECT TO "kaidera-runtime-core-verifier" USING (true);
GRANT CREATE ON SCHEMA coordination TO "kaidera-runtime-core-verifier";
ALTER FUNCTION coordination.c05_scope() OWNER TO "kaidera-runtime-core-verifier";
ALTER FUNCTION coordination.c05_request(text,text) OWNER TO "kaidera-runtime-core-verifier";
ALTER FUNCTION coordination.c05_record_head(uuid,text,text,bigint) OWNER TO "kaidera-runtime-core-verifier";
ALTER FUNCTION coordination.c05_record_payload(uuid,bigint) OWNER TO "kaidera-runtime-core-verifier";
ALTER FUNCTION coordination.c05_update_head(uuid,bigint,bigint,boolean) OWNER TO "kaidera-runtime-core-verifier";
REVOKE CREATE ON SCHEMA coordination FROM "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION coordination.c05_scope(),coordination.c05_request(text,text),
    coordination.c05_record_head(uuid,text,text,bigint),coordination.c05_record_payload(uuid,bigint),
    coordination.c05_update_head(uuid,bigint,bigint,boolean) FROM PUBLIC,"kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c05_request(text,text),coordination.c05_record_head(uuid,text,text,bigint),
    coordination.c05_record_payload(uuid,bigint),coordination.c05_update_head(uuid,bigint,bigint,boolean)
    TO "kaidera-runtime-core-request";
