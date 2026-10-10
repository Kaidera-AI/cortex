-- D1 refusal classification only. This function never grants a project scope.
CREATE FUNCTION auth.credential_active(p_digest text, p_installation uuid)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,pg_temp AS $$
BEGIN
    IF p_digest IS NULL OR p_installation IS NULL OR
       length(p_digest) <> 64 OR p_digest !~ '^[0-9a-f]{64}$' THEN
        RETURN false;
    END IF;
    RETURN EXISTS (
        SELECT 1 FROM auth.credentials c
        JOIN auth.principals p ON (p.tenant_id,p.id)=(c.tenant_id,c.principal_id)
        JOIN core.tenants t ON t.id=c.tenant_id
        WHERE c.key_digest=p_digest AND t.installation_id=p_installation
          AND NOT p.disabled AND c.revoked_at IS NULL
          AND (c.expires_at IS NULL OR c.expires_at>clock_timestamp())
    );
END;
$$;
ALTER FUNCTION auth.credential_active(text,uuid) OWNER TO "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION auth.credential_active(text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth.credential_active(text,uuid)
    TO "kaidera-runtime-core-request";
