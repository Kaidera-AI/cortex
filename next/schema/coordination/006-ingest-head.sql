-- Write-only, scope-bound digest observation for R010/R011/R012 decisions.
-- The request role receives no SELECT grant or cross-project record access.
CREATE FUNCTION coordination.c11_ingest_head(p_record uuid,p_kind text)
RETURNS TABLE(current_revision bigint,tombstone boolean,payload_sha256 text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE scope record; head record;
BEGIN
    SELECT * INTO STRICT scope FROM coordination.c05_scope();
    IF p_record IS NULL OR p_kind IS NULL OR p_kind NOT IN ('knowledge','lesson','decision') THEN
        RAISE EXCEPTION 'ingest conflict' USING ERRCODE='23505';
    END IF;
    SELECT r.kind,r.current_revision,r.tombstone,p.sha256 INTO head
      FROM core.records r
      JOIN core.record_revisions v
        ON (v.tenant_id,v.project_id,v.record_id,v.revision)
         =(r.tenant_id,r.project_id,r.id,r.current_revision)
      JOIN core.payloads p
        ON (p.tenant_id,p.project_id,p.id)=(v.tenant_id,v.project_id,v.payload_ref)
     WHERE r.tenant_id=scope.tenant_id AND r.project_id=scope.project_id
       AND r.id=p_record
     FOR UPDATE OF r;
    IF NOT FOUND THEN RETURN; END IF;
    IF head.kind IS DISTINCT FROM p_kind THEN
        RAISE EXCEPTION 'ingest conflict' USING ERRCODE='23505';
    END IF;
    RETURN QUERY SELECT head.current_revision,head.tombstone,head.sha256;
END;
$$;

ALTER FUNCTION coordination.c11_ingest_head(uuid,text) OWNER TO "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION coordination.c11_ingest_head(uuid,text)
    FROM PUBLIC,"kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c11_ingest_head(uuid,text)
    TO "kaidera-runtime-core-request";

-- A no-op still needs a durable, principal-bound replay receipt. It is not a
-- C06 mutation and therefore cannot masquerade as outcome='committed'.
ALTER TABLE coordination.idempotency DROP CONSTRAINT idempotency_outcome_check;
ALTER TABLE coordination.idempotency ADD CONSTRAINT idempotency_outcome_check
    CHECK (outcome IN ('committed','unresolved','refused','observed'));

CREATE FUNCTION coordination.c11_ingest_request(p_key text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
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
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF saved.outcome NOT IN ('committed','observed')
       OR saved.receipt->>'ingest_mode' IS NULL THEN
        RAISE EXCEPTION 'request conflict' USING ERRCODE='23505';
    END IF;
    RETURN saved.receipt;
END;
$$;

CREATE FUNCTION coordination.c11_ingest_observed_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE scope record; raw text; request jsonb; head record;
BEGIN
    IF NEW.outcome IS DISTINCT FROM 'observed' THEN RETURN NEW; END IF;
    SELECT * INTO STRICT scope FROM coordination.c05_scope();
    raw:=NEW.receipt->>'request_json';
    request:=raw::jsonb;
    IF (NEW.tenant_id,NEW.project_id,NEW.principal_id)
          IS DISTINCT FROM (scope.tenant_id,scope.project_id,scope.principal_id)
       OR raw IS NULL
       OR encode(sha256(convert_to(raw,'UTF8')),'hex') IS DISTINCT FROM NEW.request_sha256
       OR jsonb_typeof(request) IS DISTINCT FROM 'array'
       OR jsonb_array_length(request)<>6
       OR request->>0 IS DISTINCT FROM 'ingest.observe'
       OR request->>2 IS NULL OR request->>2 NOT IN ('knowledge','lesson','decision')
       OR request->>3 IS DISTINCT FROM NEW.receipt->>'payload_sha256'
       OR request->>5 IS NULL OR request->>5 NOT IN ('conflict','update')
       OR NEW.receipt->>'ingest_mode' IS DISTINCT FROM request->>5
       OR NEW.receipt->>'record_id' IS DISTINCT FROM request->>1
       OR (NEW.receipt->>'revision')::bigint IS DISTINCT FROM (request->>4)::bigint
       OR NEW.receipt->>'status' IS DISTINCT FROM 'unchanged'
       OR NEW.receipt->'event_id' IS DISTINCT FROM 'null'::jsonb
       OR NEW.receipt->'tombstone' IS DISTINCT FROM 'false'::jsonb
      THEN RAISE EXCEPTION 'observed ingest receipt invalid' USING ERRCODE='23514'; END IF;
    SELECT r.kind,r.current_revision,r.tombstone,p.sha256 INTO head
      FROM core.records r
      JOIN core.record_revisions v
        ON (v.tenant_id,v.project_id,v.record_id,v.revision)
         =(r.tenant_id,r.project_id,r.id,r.current_revision)
      JOIN core.payloads p
        ON (p.tenant_id,p.project_id,p.id)=(v.tenant_id,v.project_id,v.payload_ref)
     WHERE r.tenant_id=scope.tenant_id AND r.project_id=scope.project_id
       AND r.id=(request->>1)::uuid FOR UPDATE OF r;
    IF NOT FOUND OR head.kind IS DISTINCT FROM request->>2
       OR head.current_revision IS DISTINCT FROM (request->>4)::bigint
       OR head.tombstone IS DISTINCT FROM false
       OR head.sha256 IS DISTINCT FROM request->>3 THEN
        RAISE EXCEPTION 'observed ingest receipt is not current' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
EXCEPTION WHEN invalid_text_representation OR invalid_parameter_value THEN
    RAISE EXCEPTION 'observed ingest receipt invalid' USING ERRCODE='23514';
END;
$$;

CREATE TRIGGER c11_ingest_observed_guard BEFORE INSERT ON coordination.idempotency
    FOR EACH ROW EXECUTE FUNCTION coordination.c11_ingest_observed_guard();

GRANT CREATE ON SCHEMA coordination TO "kaidera-runtime-core-verifier";
ALTER FUNCTION coordination.c11_ingest_request(text) OWNER TO "kaidera-runtime-core-verifier";
ALTER FUNCTION coordination.c11_ingest_observed_guard() OWNER TO "kaidera-runtime-core-verifier";
REVOKE CREATE ON SCHEMA coordination FROM "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION coordination.c11_ingest_request(text),
    coordination.c11_ingest_observed_guard() FROM PUBLIC,"kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c11_ingest_request(text)
    TO "kaidera-runtime-core-request";
