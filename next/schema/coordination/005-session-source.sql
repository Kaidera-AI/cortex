-- Atomic, private legacy source-path claim for C11 session ingest.
-- The path itself is not stored; a scoped C05 write owns the claim.
CREATE TABLE coordination.session_sources (
    installation_id uuid NOT NULL REFERENCES core.installations(id),
    source_path_sha256 text NOT NULL CHECK (source_path_sha256 ~ '^[0-9a-f]{64}$'),
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    session_uuid uuid NOT NULL,
    record_id uuid NOT NULL,
    PRIMARY KEY (installation_id, source_path_sha256),
    UNIQUE (installation_id, session_uuid),
    UNIQUE (installation_id, record_id),
    FOREIGN KEY (tenant_id, project_id, record_id)
        REFERENCES core.records (tenant_id, project_id, id)
);
ALTER TABLE coordination.session_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE coordination.session_sources FORCE ROW LEVEL SECURITY;
CREATE POLICY session_source_verifier ON coordination.session_sources
    TO "kaidera-runtime-core-verifier" USING (true) WITH CHECK (true);
REVOKE ALL ON coordination.session_sources FROM PUBLIC,"kaidera-runtime-core-request";

CREATE FUNCTION coordination.c11b3_session_claim(
    p_source_path text, p_session_uuid uuid, p_record_id uuid)
RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE scope record; claimed uuid;
BEGIN
    SELECT * INTO STRICT scope FROM coordination.c05_scope();
    IF scope.action <> 'write' OR p_source_path IS NULL
       OR length(p_source_path) NOT BETWEEN 1 AND 512
       OR p_session_uuid IS NULL OR p_record_id IS NULL
       OR NOT EXISTS(SELECT 1 FROM core.records r
            WHERE r.tenant_id=scope.tenant_id AND r.project_id=scope.project_id
              AND r.id=p_record_id AND r.kind='session' AND NOT r.tombstone) THEN
        RAISE EXCEPTION 'session source conflict' USING ERRCODE='23505';
    END IF;
    INSERT INTO coordination.session_sources
      (installation_id,source_path_sha256,tenant_id,project_id,session_uuid,record_id)
    VALUES(scope.installation_id,
      encode(sha256(convert_to(p_source_path,'UTF8')),'hex'),
      scope.tenant_id,scope.project_id,p_session_uuid,p_record_id)
    ON CONFLICT (installation_id,source_path_sha256) DO UPDATE
      SET session_uuid=EXCLUDED.session_uuid
      WHERE coordination.session_sources.tenant_id=EXCLUDED.tenant_id
        AND coordination.session_sources.project_id=EXCLUDED.project_id
        AND coordination.session_sources.session_uuid=EXCLUDED.session_uuid
        AND coordination.session_sources.record_id=EXCLUDED.record_id
    RETURNING record_id INTO claimed;
    IF claimed IS DISTINCT FROM p_record_id THEN
        RAISE EXCEPTION 'session source conflict' USING ERRCODE='23505';
    END IF;
END;
$$;
GRANT CREATE ON SCHEMA coordination TO "kaidera-runtime-core-verifier";
ALTER TABLE coordination.session_sources OWNER TO "kaidera-runtime-core-verifier";
ALTER FUNCTION coordination.c11b3_session_claim(text,uuid,uuid)
    OWNER TO "kaidera-runtime-core-verifier";
REVOKE CREATE ON SCHEMA coordination FROM "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION coordination.c11b3_session_claim(text,uuid,uuid)
    FROM PUBLIC,"kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c11b3_session_claim(text,uuid,uuid)
    TO "kaidera-runtime-core-request";
