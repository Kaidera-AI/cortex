-- C11b: read only the current principal's committed C05 request metadata.
-- This is a private write-context helper; the caller still replays through C05.
CREATE FUNCTION coordination.c11b_request(p_key text)
RETURNS TABLE(request_json text,receipt jsonb)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE scope record; saved record;
BEGIN
    SELECT * INTO STRICT scope FROM coordination.c05_scope();
    IF p_key IS NULL OR length(p_key) NOT BETWEEN 1 AND 256 THEN
        RAISE EXCEPTION 'request conflict' USING ERRCODE='23505';
    END IF;
    SELECT i.outcome,i.receipt INTO saved FROM coordination.idempotency i
        WHERE i.tenant_id=scope.tenant_id AND i.project_id=scope.project_id
          AND i.principal_id=scope.principal_id AND i.request_key=p_key;
    IF NOT FOUND THEN RETURN; END IF;
    IF saved.outcome IS DISTINCT FROM 'committed' OR saved.receipt->>'request_json' IS NULL THEN
        RAISE EXCEPTION 'request conflict' USING ERRCODE='23505';
    END IF;
    RETURN QUERY SELECT saved.receipt->>'request_json',saved.receipt;
END;
$$;
CREATE FUNCTION coordination.c11b_writer_matches(p_name text)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE scope record;
BEGIN
    SELECT * INTO STRICT scope FROM coordination.c05_scope();
    RETURN p_name IS NOT NULL AND length(p_name) BETWEEN 1 AND 256
        AND EXISTS(SELECT 1 FROM auth.principals p
            WHERE p.tenant_id=scope.tenant_id AND p.id=scope.principal_id
              AND p.name=p_name AND p.disabled=false);
END;
$$;
GRANT CREATE ON SCHEMA coordination TO "kaidera-runtime-core-verifier";
ALTER FUNCTION coordination.c11b_request(text) OWNER TO "kaidera-runtime-core-verifier";
ALTER FUNCTION coordination.c11b_writer_matches(text) OWNER TO "kaidera-runtime-core-verifier";
REVOKE CREATE ON SCHEMA coordination FROM "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION coordination.c11b_request(text) FROM PUBLIC,"kaidera-runtime-core-request";
REVOKE ALL ON FUNCTION coordination.c11b_writer_matches(text) FROM PUBLIC,"kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c11b_request(text) TO "kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c11b_writer_matches(text) TO "kaidera-runtime-core-request";
