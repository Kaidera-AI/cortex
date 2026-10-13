-- Read committed session UUIDs through a bound C04 read context only.
-- The request role never receives SELECT on the private source-claim table.
CREATE FUNCTION coordination.c11b4_session_ids()
RETURNS TABLE(session_uuid uuid)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE scoped record; digest text; installation uuid; project uuid;
BEGIN
    IF current_setting('cortex.action',true) IS DISTINCT FROM 'read' THEN
        RAISE EXCEPTION 'private read context required' USING ERRCODE='42501';
    END IF;
    digest := current_setting('cortex.credential_digest',true);
    installation := NULLIF(current_setting('cortex.installation_id',true),'')::uuid;
    project := NULLIF(current_setting('cortex.project_id',true),'')::uuid;
    IF NOT auth.context_is_bound(digest,installation,project,'read') THEN
        RAISE EXCEPTION 'private read context required' USING ERRCODE='42501';
    END IF;
    SELECT * INTO scoped FROM auth.resolve_scope(digest,installation,project,'read');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'private read context required' USING ERRCODE='42501';
    END IF;
    RETURN QUERY SELECT DISTINCT s.session_uuid
      FROM coordination.session_sources s
      JOIN core.records r ON (r.tenant_id,r.project_id,r.id)=
          (s.tenant_id,s.project_id,s.record_id)
      WHERE s.installation_id=scoped.installation_id
        AND s.tenant_id=scoped.tenant_id AND s.project_id=scoped.project_id
        AND r.kind='session' AND NOT r.tombstone
      ORDER BY s.session_uuid;
EXCEPTION WHEN invalid_text_representation THEN
    RAISE EXCEPTION 'private read context required' USING ERRCODE='42501';
END;
$$;
GRANT CREATE ON SCHEMA coordination TO "kaidera-runtime-core-verifier";
ALTER FUNCTION coordination.c11b4_session_ids()
    OWNER TO "kaidera-runtime-core-verifier";
REVOKE CREATE ON SCHEMA coordination FROM "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION coordination.c11b4_session_ids()
    FROM PUBLIC,"kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c11b4_session_ids()
    TO "kaidera-runtime-core-request";
