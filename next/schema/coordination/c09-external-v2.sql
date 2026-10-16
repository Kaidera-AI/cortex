-- S3 SOURCE DRAFT. NOT MANIFESTED, NOT an admitted production migration.
-- PG-C owns schema admission/sealing. No existing SQL byte or manifest is changed.
-- Execute only in a separately authorized copied-DB qualification after review.
CREATE TABLE coordination.vector_bindings (
    installation_id uuid NOT NULL,
    module_id text NOT NULL,
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    generation bigint NOT NULL,
    binding jsonb NOT NULL CHECK(jsonb_typeof(binding)='object'),
    PRIMARY KEY(installation_id,module_id,tenant_id,project_id,generation),
    FOREIGN KEY(installation_id,module_id,tenant_id,project_id,generation)
      REFERENCES coordination.consumer_project_checkpoints
);
CREATE TABLE coordination.vector_applied_heads (
    installation_id uuid NOT NULL,
    module_id text NOT NULL,
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    generation bigint NOT NULL,
    aggregate_id uuid NOT NULL,
    kind text NOT NULL,
    revision bigint NOT NULL CHECK(revision>0),
    event_id uuid NOT NULL,
    payload_sha256 text NOT NULL CHECK(payload_sha256 ~ '^[0-9a-f]{64}$'),
    tombstone boolean NOT NULL,
    effect_sha256 text NOT NULL CHECK(effect_sha256 ~ '^[0-9a-f]{64}$'),
    point_id uuid NOT NULL,
    PRIMARY KEY(installation_id,module_id,tenant_id,project_id,generation,aggregate_id),
    FOREIGN KEY(installation_id,module_id,tenant_id,project_id,generation)
      REFERENCES coordination.vector_bindings
);
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['vector_bindings','vector_applied_heads'] LOOP
    EXECUTE format('ALTER TABLE coordination.%I ENABLE ROW LEVEL SECURITY',t);
    EXECUTE format('ALTER TABLE coordination.%I FORCE ROW LEVEL SECURITY',t);
    EXECUTE format('CREATE POLICY c09_private ON coordination.%I TO "kaidera-runtime-core-verifier" USING(true) WITH CHECK(true)',t);
    EXECUTE format('ALTER TABLE coordination.%I OWNER TO "kaidera-runtime-core-verifier"',t);
    EXECUTE format('REVOKE ALL ON coordination.%I FROM PUBLIC,"kaidera-runtime-core-request"',t);
  END LOOP;
END $$;

CREATE FUNCTION coordination.c09_point_id(p_key jsonb) RETURNS uuid
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
  WITH canonical AS (
    SELECT '{'||string_agg(to_jsonb(key)::text||':'||value::text,',' ORDER BY key COLLATE "C")||'}' AS body
      FROM jsonb_each(p_key)
  ), digest AS (
    SELECT substring(sha256(convert_to(body,'UTF8')) FROM 1 FOR 16) AS bytes FROM canonical
  ) SELECT encode(set_byte(set_byte(bytes,6,(get_byte(bytes,6)&15)|80),8,(get_byte(bytes,8)&63)|128),'hex')::uuid
      FROM digest;
$$;

CREATE FUNCTION coordination.c09_bind(p_module text,p_generation bigint,p_binding jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; c record; expected jsonb; existing jsonb;
BEGIN
  SELECT * INTO STRICT s FROM coordination.c07_scope();
  SELECT * INTO STRICT c FROM coordination.consumer_project_checkpoints
    WHERE(installation_id,module_id,tenant_id,project_id,generation)=
      (s.installation_id,p_module,s.tenant_id,s.project_id,p_generation) FOR UPDATE;
  -- No synthetic snapshot credit; generation materialization is a separate gate.
  IF c.state<>'active' OR c.snapshot_cursor<>0 THEN RAISE EXCEPTION 'vector_binding_refused'; END IF;
  IF p_binding IS NULL OR jsonb_typeof(p_binding)<>'object'
      OR (SELECT count(*) FROM jsonb_object_keys(p_binding))<>7
      OR NOT p_binding ?& ARRAY['installation_id','tenant_id','project_id','module_id',
                               'consumer_generation','collection_generation','model_identity']
      OR jsonb_typeof(p_binding->'collection_generation') IS DISTINCT FROM 'string'
      OR jsonb_typeof(p_binding->'model_identity') IS DISTINCT FROM 'string'
      OR p_binding->>'model_identity' !~ '^[0-9a-f]{64}$'
      OR (p_binding->>'collection_generation')::uuid='00000000-0000-0000-0000-000000000000'::uuid
    THEN RAISE EXCEPTION 'vector_binding_refused'; END IF;
  expected:=jsonb_build_object('installation_id',s.installation_id::text,'tenant_id',s.tenant_id::text,
    'project_id',s.project_id::text,'module_id',p_module,'consumer_generation',p_generation,
    'collection_generation',(p_binding->>'collection_generation')::uuid::text,
    'model_identity',p_binding->>'model_identity');
  IF expected IS DISTINCT FROM p_binding THEN RAISE EXCEPTION 'vector_binding_refused'; END IF;
  SELECT binding INTO existing FROM coordination.vector_bindings
    WHERE(installation_id,module_id,tenant_id,project_id,generation)=
      (s.installation_id,p_module,s.tenant_id,s.project_id,p_generation);
  IF FOUND THEN
    IF existing IS DISTINCT FROM expected THEN RAISE EXCEPTION 'vector_binding_immutable'; END IF;
    RETURN true;
  END IF;
  IF c.scan_cursor<>0 OR c.applied_cursor<>0 OR EXISTS(
      SELECT 1 FROM coordination.consumer_aggregate_heads WHERE
       (installation_id,module_id,tenant_id,project_id,generation)=
       (s.installation_id,p_module,s.tenant_id,s.project_id,p_generation))
    THEN RAISE EXCEPTION 'vector_binding_refused'; END IF;
  INSERT INTO coordination.vector_bindings VALUES(s.installation_id,p_module,s.tenant_id,s.project_id,p_generation,expected);
  RETURN true;
END $$;

-- One existing checkpoint/poison mechanism. Its v1 validator remains untouched,
-- private, and unreachable directly by request-role clients after this wrapper.
ALTER FUNCTION coordination.c07_record(text,bigint,bigint,uuid,text,text,jsonb) RENAME TO c07_record_v1;
REVOKE ALL ON FUNCTION coordination.c07_record_v1(text,bigint,bigint,uuid,text,text,jsonb)
  FROM PUBLIC,"kaidera-runtime-core-request";
CREATE FUNCTION coordination.c07_record(p_module text,p_generation bigint,p_cursor bigint,p_event uuid,
  p_outcome text,p_error text,p_receipt jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; binding_value jsonb; requested record; observed record; key_value jsonb;
        v1 jsonb; result_value jsonb; r jsonb; o jsonb; previous record;
BEGIN
  SELECT * INTO STRICT s FROM coordination.c07_scope();
  -- Serialize generation/effect bookkeeping under the SAME existing C07 lock.
  PERFORM 1 FROM coordination.consumer_project_checkpoints WHERE
    (installation_id,module_id,tenant_id,project_id,generation)=
    (s.installation_id,p_module,s.tenant_id,s.project_id,p_generation) FOR UPDATE;
  SELECT binding INTO binding_value FROM coordination.vector_bindings WHERE
    (installation_id,module_id,tenant_id,project_id,generation)=
    (s.installation_id,p_module,s.tenant_id,s.project_id,p_generation);
  IF NOT FOUND OR p_outcome NOT IN ('applied','stale') THEN
    RETURN coordination.c07_record_v1(p_module,p_generation,p_cursor,p_event,p_outcome,p_error,p_receipt);
  END IF;
  r:=p_receipt; o:=r->'observed';
  IF r IS NULL OR jsonb_typeof(r) IS DISTINCT FROM 'object'
      OR NOT r ?& ARRAY['version','sink','key','point_id','event_id','aggregate_id','revision','tombstone',
        'payload_sha256','generation','stale','effect_sha256','observed','proof','operation_id']
      OR (SELECT count(*) FROM jsonb_object_keys(r))<>15
      OR r->'version' IS DISTINCT FROM '2'::jsonb OR r->>'sink' IS DISTINCT FROM 'qdrant'
      OR r->>'proof' IS DISTINCT FROM 'completed_keyed_readback'
      OR jsonb_typeof(r->'key') IS DISTINCT FROM 'object'
      OR jsonb_typeof(r->'operation_id') IS DISTINCT FROM 'number'
      OR (r->>'operation_id')::numeric<0 OR trunc((r->>'operation_id')::numeric)<>(r->>'operation_id')::numeric
      OR jsonb_typeof(r->'effect_sha256') IS DISTINCT FROM 'string' OR r->>'effect_sha256' !~ '^[0-9a-f]{64}$'
      OR jsonb_typeof(o) IS DISTINCT FROM 'object'
      OR NOT o ?& ARRAY['revision','event_id','payload_sha256','tombstone','effect_sha256']
      OR (SELECT count(*) FROM jsonb_object_keys(o))<>5
      OR jsonb_typeof(o->'revision') IS DISTINCT FROM 'number'
      OR jsonb_typeof(o->'event_id') IS DISTINCT FROM 'string'
      OR jsonb_typeof(o->'payload_sha256') IS DISTINCT FROM 'string'
      OR jsonb_typeof(o->'tombstone') IS DISTINCT FROM 'boolean'
      OR jsonb_typeof(o->'effect_sha256') IS DISTINCT FROM 'string'
      OR o->>'effect_sha256' !~ '^[0-9a-f]{64}$'
    THEN RAISE EXCEPTION 'consumer_receipt_mismatch'; END IF;
  SELECT e.* INTO STRICT requested FROM coordination.published_events p JOIN coordination.outbox e
    ON(e.installation_id,e.tenant_id,e.project_id,e.event_id)=(p.installation_id,p.tenant_id,p.project_id,p.event_id)
    WHERE(p.installation_id,p.tenant_id,p.project_id,p.cursor,p.event_id)=
      (s.installation_id,s.tenant_id,s.project_id,p_cursor,p_event);
  key_value:=binding_value||jsonb_build_object('aggregate_id',requested.aggregate_id::text);
  IF r->'key' IS DISTINCT FROM key_value
      OR jsonb_typeof(r->'point_id') IS DISTINCT FROM 'string'
      OR r->>'point_id' IS DISTINCT FROM coordination.c09_point_id(key_value)::text
    THEN RAISE EXCEPTION 'consumer_receipt_mismatch'; END IF;
  -- A stale witness must name an ACTUAL committed/published same-key successor,
  -- never a desired revision, collection scan head, engine counter or liveness.
  SELECT e.* INTO STRICT observed FROM coordination.published_events p JOIN coordination.outbox e
    ON(e.installation_id,e.tenant_id,e.project_id,e.event_id)=(p.installation_id,p.tenant_id,p.project_id,p.event_id)
    WHERE(p.installation_id,p.tenant_id,p.project_id,e.aggregate_id,e.event_id)=
      (s.installation_id,s.tenant_id,s.project_id,requested.aggregate_id,(o->>'event_id')::uuid);
  IF (o->>'revision')::numeric IS DISTINCT FROM observed.aggregate_revision
      OR o->>'payload_sha256' IS DISTINCT FROM observed.payload_sha256
      OR (o->>'tombstone')::boolean IS DISTINCT FROM observed.tombstone
      OR observed.aggregate_kind IS DISTINCT FROM requested.aggregate_kind
      OR (p_outcome='stale' AND observed.aggregate_revision<=requested.aggregate_revision)
      OR (p_outcome='applied' AND (observed.event_id<>requested.event_id OR o IS DISTINCT FROM
           jsonb_build_object('revision',requested.aggregate_revision,'event_id',requested.event_id::text,
             'payload_sha256',requested.payload_sha256,'tombstone',requested.tombstone,'effect_sha256',r->>'effect_sha256')))
    THEN RAISE EXCEPTION 'consumer_receipt_mismatch'; END IF;
  SELECT * INTO previous FROM coordination.vector_applied_heads WHERE
    (installation_id,module_id,tenant_id,project_id,generation,aggregate_id)=
    (s.installation_id,p_module,s.tenant_id,s.project_id,p_generation,requested.aggregate_id);
  IF FOUND AND previous.revision=observed.aggregate_revision AND
      (previous.event_id<>observed.event_id OR previous.effect_sha256<>o->>'effect_sha256')
    THEN RAISE EXCEPTION 'consumer_receipt_mismatch'; END IF;
  SELECT jsonb_object_agg(key,value) INTO v1 FROM jsonb_each(r)
    WHERE key=ANY(ARRAY['event_id','aggregate_id','revision','tombstone','payload_sha256','generation','stale']);
  result_value:=coordination.c07_record_v1(p_module,p_generation,p_cursor,p_event,p_outcome,p_error,v1);
  -- Same transaction: if receipt persistence fails, C07's applied/poison/prefix
  -- changes roll back too. Do not mark a synthetic desired revision as indexed.
  INSERT INTO coordination.vector_applied_heads VALUES(s.installation_id,p_module,s.tenant_id,s.project_id,
    p_generation,requested.aggregate_id,observed.aggregate_kind,observed.aggregate_revision,observed.event_id,
    observed.payload_sha256,observed.tombstone,o->>'effect_sha256',(r->>'point_id')::uuid)
    ON CONFLICT(installation_id,module_id,tenant_id,project_id,generation,aggregate_id)
    DO UPDATE SET revision=EXCLUDED.revision,event_id=EXCLUDED.event_id,payload_sha256=EXCLUDED.payload_sha256,
      tombstone=EXCLUDED.tombstone,effect_sha256=EXCLUDED.effect_sha256,point_id=EXCLUDED.point_id,kind=EXCLUDED.kind
    WHERE coordination.vector_applied_heads.revision<EXCLUDED.revision;
  RETURN result_value;
END $$;

CREATE FUNCTION coordination.c09_state(p_module text,p_ids uuid[]) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; c record; binding_value jsonb; result_value jsonb; floor_value bigint;
BEGIN
  SELECT * INTO STRICT s FROM auth.identity_scope(false);
  IF s.action<>'read' OR cardinality(p_ids)>100 THEN RAISE EXCEPTION 'vector_read_refused'; END IF;
  SELECT * INTO STRICT c FROM coordination.consumer_project_checkpoints WHERE
    (installation_id,module_id,tenant_id,project_id)=(s.installation_id,p_module,s.tenant_id,s.project_id)
    ORDER BY generation DESC LIMIT 1;
  SELECT COALESCE((project_retained_floors->>(s.tenant_id::text||':'||s.project_id::text))::bigint,0)
    INTO floor_value FROM coordination.feed_state WHERE installation_id=s.installation_id;
  IF c.state<>'active' OR c.applied_cursor<COALESCE(floor_value,0) THEN RAISE EXCEPTION 'vector_read_refused'; END IF;
  SELECT binding INTO STRICT binding_value FROM coordination.vector_bindings WHERE
    (installation_id,module_id,tenant_id,project_id,generation)=
    (s.installation_id,p_module,s.tenant_id,s.project_id,c.generation);
  WITH current_records AS (
    SELECT r.*,h.revision AS indexed_revision,h.event_id,h.payload_sha256,h.effect_sha256,h.point_id,
      (h.revision=r.current_revision AND h.tombstone=r.tombstone AND h.kind=r.kind) IS TRUE AS indexed
    FROM core.records r LEFT JOIN coordination.vector_applied_heads h ON
      (h.installation_id,h.module_id,h.tenant_id,h.project_id,h.generation,h.aggregate_id)=
      (s.installation_id,p_module,r.tenant_id,r.project_id,c.generation,r.id)
    WHERE(r.tenant_id,r.project_id)=(s.tenant_id,s.project_id)
  ) SELECT jsonb_build_object('binding',binding_value,
      'indexed_records',count(*) FILTER(WHERE NOT tombstone AND indexed),
      'pending_records',count(*) FILTER(WHERE NOT tombstone AND NOT indexed),
      'targets',COALESCE(jsonb_agg(jsonb_build_object('record_id',id::text,'kind',kind,
          'revision',current_revision,'tombstone',tombstone,'indexed_revision',indexed_revision,
          'event_id',event_id::text,'payload_sha256',payload_sha256,'effect_sha256',effect_sha256,
          'point_id',point_id::text,'indexed',indexed)) FILTER(WHERE id=ANY(p_ids)),'[]'::jsonb))
    INTO result_value FROM current_records;
  RETURN result_value;
END $$;
GRANT CREATE ON SCHEMA coordination TO "kaidera-runtime-core-verifier";
DO $$ DECLARE f record; BEGIN
  FOR f IN SELECT p.oid::regprocedure AS signature FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='coordination' AND p.proname IN('c09_point_id','c09_bind','c09_state','c07_record') LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO "kaidera-runtime-core-verifier"',f.signature);
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,"kaidera-runtime-core-request"',f.signature);
    IF f.signature::text NOT LIKE '%c09_point_id%' THEN
      EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO "kaidera-runtime-core-request"',f.signature);
    END IF;
  END LOOP;
END $$;
REVOKE CREATE ON SCHEMA coordination FROM "kaidera-runtime-core-verifier";
