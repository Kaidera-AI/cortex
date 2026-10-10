-- C07: one scoped project instance per module generation. No cursor inferred from a
-- global project page is an installation-complete checkpoint.
CREATE TABLE coordination.consumer_project_checkpoints (
    installation_id uuid NOT NULL REFERENCES core.installations,
    module_id text NOT NULL CHECK (length(module_id) BETWEEN 1 AND 128),
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    scan_cursor bigint NOT NULL CHECK (scan_cursor >= 0),
    applied_cursor bigint NOT NULL CHECK (applied_cursor >= 0 AND applied_cursor <= scan_cursor),
    state text NOT NULL CHECK (state IN ('active','expired','rebuilding','blocked')),
    snapshot_cursor bigint NOT NULL CHECK (snapshot_cursor >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (installation_id,module_id,tenant_id,project_id,generation),
    FOREIGN KEY (tenant_id,project_id) REFERENCES core.projects(tenant_id,id)
);
CREATE TABLE coordination.consumer_aggregate_heads (
    installation_id uuid NOT NULL,
    module_id text NOT NULL,
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    generation bigint NOT NULL,
    aggregate_id uuid NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    event_id uuid NOT NULL,
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    tombstone boolean NOT NULL,
    PRIMARY KEY (installation_id,module_id,tenant_id,project_id,generation,aggregate_id),
    FOREIGN KEY (installation_id,module_id,tenant_id,project_id,generation)
      REFERENCES coordination.consumer_project_checkpoints
);
CREATE TABLE coordination.consumer_event_outcomes (
    installation_id uuid NOT NULL,
    module_id text NOT NULL,
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    generation bigint NOT NULL,
    cursor bigint NOT NULL CHECK (cursor > 0),
    event_id uuid NOT NULL,
    aggregate_id uuid NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('applied','stale','poison','held')),
    error_code text CHECK (error_code ~ '^[a-z][a-z0-9_]{0,63}$'),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (installation_id,module_id,tenant_id,project_id,generation,cursor),
    UNIQUE (installation_id,module_id,tenant_id,project_id,generation,event_id),
    FOREIGN KEY (installation_id,module_id,tenant_id,project_id,generation)
      REFERENCES coordination.consumer_project_checkpoints
);
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['consumer_project_checkpoints','consumer_aggregate_heads','consumer_event_outcomes'] LOOP
    EXECUTE format('ALTER TABLE coordination.%I ENABLE ROW LEVEL SECURITY',t);
    EXECUTE format('ALTER TABLE coordination.%I FORCE ROW LEVEL SECURITY',t);
    EXECUTE format('CREATE POLICY c07_private ON coordination.%I TO "kaidera-runtime-core-verifier" USING (true) WITH CHECK (true)',t);
    EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON coordination.%I TO "kaidera-runtime-core-verifier"',t);
    EXECUTE format('ALTER TABLE coordination.%I OWNER TO "kaidera-runtime-core-verifier"',t);
  END LOOP;
END $$;

CREATE FUNCTION coordination.c07_scope() RETURNS TABLE(installation_id uuid,tenant_id uuid,project_id uuid)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,auth,coordination,pg_temp AS $$
DECLARE s record;
BEGIN
  SELECT * INTO s FROM auth.identity_scope(true);
  IF NOT FOUND OR s.action<>'control' THEN RAISE EXCEPTION 'consumer_forbidden'; END IF;
  RETURN QUERY SELECT s.installation_id,s.tenant_id,s.project_id;
END $$;

CREATE FUNCTION coordination.c07_status(p_module text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,auth,coordination,pg_temp AS $$
DECLARE s record; c record; f record; floor_value bigint;
BEGIN
  SELECT * INTO STRICT s FROM coordination.c07_scope();
  SELECT * INTO c FROM coordination.consumer_project_checkpoints
   WHERE (installation_id,module_id,tenant_id,project_id)=(s.installation_id,p_module,s.tenant_id,s.project_id)
   ORDER BY generation DESC LIMIT 1;
  IF NOT FOUND THEN RETURN NULL; END IF;
  SELECT * INTO f FROM coordination.feed_state WHERE installation_id=s.installation_id;
  floor_value:=COALESCE((f.project_retained_floors->>(s.tenant_id::text||':'||s.project_id::text))::bigint,0);
  RETURN jsonb_build_object('scan_cursor',c.scan_cursor,'applied_cursor',c.applied_cursor,
    'generation',c.generation,'state',c.state,'floor',floor_value,
    'lag',GREATEST(0,COALESCE(f.last_published_cursor,0)-c.applied_cursor),
    'oldest_poison',(SELECT min(o.cursor) FROM coordination.consumer_event_outcomes o
      WHERE (o.installation_id,o.module_id,o.tenant_id,o.project_id,o.generation)=
        (s.installation_id,p_module,s.tenant_id,s.project_id,c.generation) AND o.outcome='poison'),
    'outcome_count',(SELECT count(*) FROM coordination.consumer_event_outcomes o
      WHERE (o.installation_id,o.module_id,o.tenant_id,o.project_id,o.generation)=
        (s.installation_id,p_module,s.tenant_id,s.project_id,c.generation)),
    'pending_outcomes',(SELECT count(*) FROM coordination.consumer_event_outcomes o
      WHERE (o.installation_id,o.module_id,o.tenant_id,o.project_id,o.generation)=
        (s.installation_id,p_module,s.tenant_id,s.project_id,c.generation)
        AND o.cursor>c.applied_cursor));
END $$;

CREATE FUNCTION coordination.c07_register(p_module text,p_snapshot bigint,p_generation bigint,p_rebuild boolean)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; current_row record; f record; floor_value bigint;
BEGIN
  SELECT * INTO STRICT s FROM coordination.c07_scope();
  IF p_module IS NULL OR length(p_module) NOT BETWEEN 1 AND 128 OR p_snapshot IS NULL OR p_snapshot<0
     OR p_generation IS NULL OR p_generation<1 THEN RAISE EXCEPTION 'consumer_invalid_input'; END IF;
  INSERT INTO coordination.feed_state(installation_id) VALUES(s.installation_id) ON CONFLICT DO NOTHING;
  SELECT * INTO STRICT f FROM coordination.feed_state WHERE installation_id=s.installation_id FOR UPDATE;
  floor_value:=COALESCE((f.project_retained_floors->>(s.tenant_id::text||':'||s.project_id::text))::bigint,0);
  IF p_snapshot<floor_value OR p_snapshot>f.last_published_cursor THEN RAISE EXCEPTION 'consumer_expired'; END IF;
  SELECT * INTO current_row FROM coordination.consumer_project_checkpoints
    WHERE (installation_id,module_id,tenant_id,project_id)=(s.installation_id,p_module,s.tenant_id,s.project_id)
    ORDER BY generation DESC LIMIT 1 FOR UPDATE;
  IF FOUND THEN
    IF NOT p_rebuild OR p_generation<=current_row.generation OR p_snapshot<current_row.scan_cursor AND current_row.state<>'expired'
      THEN RAISE EXCEPTION 'consumer_generation_required'; END IF;
    UPDATE coordination.consumer_project_checkpoints SET state='expired'
      WHERE (installation_id,module_id,tenant_id,project_id,generation)=
        (s.installation_id,p_module,s.tenant_id,s.project_id,current_row.generation);
  ELSIF p_rebuild OR p_generation<>1 THEN
    RAISE EXCEPTION 'consumer_generation_required';
  END IF;
  INSERT INTO coordination.consumer_project_checkpoints
    (installation_id,module_id,tenant_id,project_id,generation,scan_cursor,applied_cursor,state,snapshot_cursor)
    VALUES(s.installation_id,p_module,s.tenant_id,s.project_id,p_generation,p_snapshot,p_snapshot,'active',p_snapshot);
  INSERT INTO coordination.consumer_checkpoints(installation_id,module_id,applied_cursor,generation,state)
    VALUES(s.installation_id,p_module,0,1,'blocked') ON CONFLICT DO NOTHING;
  RETURN coordination.c07_status(p_module);
END $$;

CREATE FUNCTION coordination.c07_expire(p_module text,p_generation bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,coordination,pg_temp AS $$
DECLARE s record;
BEGIN
  SELECT * INTO STRICT s FROM coordination.c07_scope();
  UPDATE coordination.consumer_project_checkpoints SET state='expired',updated_at=clock_timestamp()
    WHERE (installation_id,module_id,tenant_id,project_id,generation)=
      (s.installation_id,p_module,s.tenant_id,s.project_id,p_generation);
END $$;

CREATE FUNCTION coordination.c07_poisoned(p_module text,p_generation bigint,p_aggregate uuid) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,auth,coordination,pg_temp AS $$
DECLARE s record;
BEGIN
  SELECT * INTO STRICT s FROM coordination.c07_scope();
  RETURN EXISTS(SELECT 1 FROM coordination.consumer_event_outcomes o
    JOIN coordination.quarantine q ON (q.tenant_id,q.project_id,q.event_id,q.module_id)=
      (o.tenant_id,o.project_id,o.event_id,o.module_id)
    WHERE (o.installation_id,o.module_id,o.tenant_id,o.project_id,o.generation,o.aggregate_id)=
      (s.installation_id,p_module,s.tenant_id,s.project_id,p_generation,p_aggregate)
      AND o.outcome='poison' AND q.resolved_at IS NULL);
END $$;

CREATE FUNCTION coordination.c07_record(p_module text,p_generation bigint,p_cursor bigint,p_event uuid,
  p_outcome text,p_error text,p_receipt jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; c record; e record; existing record; new_applied bigint; candidate record;
BEGIN
  SELECT * INTO STRICT s FROM coordination.c07_scope();
  SELECT * INTO STRICT c FROM coordination.consumer_project_checkpoints
    WHERE (installation_id,module_id,tenant_id,project_id,generation)=
      (s.installation_id,p_module,s.tenant_id,s.project_id,p_generation) FOR UPDATE;
  IF c.state<>'active' THEN RAISE EXCEPTION 'consumer_expired'; END IF;
  SELECT p.cursor,o.* INTO e FROM coordination.published_events p
    JOIN coordination.outbox o ON (o.installation_id,o.tenant_id,o.project_id,o.event_id)=
      (p.installation_id,p.tenant_id,p.project_id,p.event_id)
    WHERE (p.installation_id,p.tenant_id,p.project_id,p.cursor,p.event_id)=
      (s.installation_id,s.tenant_id,s.project_id,p_cursor,p_event);
  IF NOT FOUND THEN RAISE EXCEPTION 'consumer_event_mismatch'; END IF;
  IF p_outcome NOT IN ('applied','stale','poison','held') OR p_cursor<=c.snapshot_cursor
    THEN RAISE EXCEPTION 'consumer_invalid_input'; END IF;
  SELECT * INTO existing FROM coordination.consumer_event_outcomes
    WHERE (installation_id,module_id,tenant_id,project_id,generation,cursor)=
      (s.installation_id,p_module,s.tenant_id,s.project_id,p_generation,p_cursor);
  IF FOUND AND existing.event_id<>p_event THEN RAISE EXCEPTION 'consumer_event_mismatch'; END IF;
  IF FOUND AND existing.outcome IN ('applied','stale') AND p_outcome IN ('poison','held')
    THEN RAISE EXCEPTION 'consumer_outcome_immutable'; END IF;
  IF p_outcome IN ('applied','stale') THEN
    IF p_receipt IS NULL OR p_receipt->>'event_id'<>p_event::text
       OR p_receipt->>'aggregate_id'<>e.aggregate_id::text
       OR (p_receipt->>'revision')::bigint<>e.aggregate_revision
       OR (p_receipt->>'tombstone')::boolean<>e.tombstone
       OR p_receipt->>'payload_sha256'<>e.payload_sha256
       OR (p_receipt->>'generation')::bigint<>p_generation
       OR (p_receipt->>'stale')::boolean<>(p_outcome='stale')
       THEN RAISE EXCEPTION 'consumer_receipt_mismatch'; END IF;
    INSERT INTO coordination.consumer_aggregate_heads
      (installation_id,module_id,tenant_id,project_id,generation,aggregate_id,revision,event_id,payload_sha256,tombstone)
      VALUES(s.installation_id,p_module,s.tenant_id,s.project_id,p_generation,e.aggregate_id,
        e.aggregate_revision,p_event,e.payload_sha256,e.tombstone)
      ON CONFLICT (installation_id,module_id,tenant_id,project_id,generation,aggregate_id)
      DO UPDATE SET revision=EXCLUDED.revision,event_id=EXCLUDED.event_id,
        payload_sha256=EXCLUDED.payload_sha256,tombstone=EXCLUDED.tombstone
      WHERE coordination.consumer_aggregate_heads.revision<EXCLUDED.revision;
    UPDATE coordination.quarantine SET resolved_at=clock_timestamp()
      WHERE (tenant_id,project_id,event_id,module_id)=(s.tenant_id,s.project_id,p_event,p_module)
        AND resolved_at IS NULL;
  ELSIF p_outcome='poison' THEN
    IF p_error IS NULL OR p_error !~ '^[a-z][a-z0-9_]{0,63}$' THEN RAISE EXCEPTION 'consumer_invalid_input'; END IF;
    INSERT INTO coordination.quarantine(tenant_id,project_id,event_id,module_id,error_code)
      VALUES(s.tenant_id,s.project_id,p_event,p_module,p_error)
      ON CONFLICT (tenant_id,project_id,event_id,module_id) DO NOTHING;
  END IF;
  INSERT INTO coordination.consumer_event_outcomes
    (installation_id,module_id,tenant_id,project_id,generation,cursor,event_id,aggregate_id,outcome,error_code)
    VALUES(s.installation_id,p_module,s.tenant_id,s.project_id,p_generation,p_cursor,p_event,e.aggregate_id,p_outcome,p_error)
    ON CONFLICT (installation_id,module_id,tenant_id,project_id,generation,cursor)
    DO UPDATE SET outcome=EXCLUDED.outcome,error_code=EXCLUDED.error_code,recorded_at=clock_timestamp()
    WHERE coordination.consumer_event_outcomes.event_id=EXCLUDED.event_id;
  UPDATE coordination.consumer_project_checkpoints SET scan_cursor=GREATEST(scan_cursor,p_cursor),
    updated_at=clock_timestamp() WHERE (installation_id,module_id,tenant_id,project_id,generation)=
      (s.installation_id,p_module,s.tenant_id,s.project_id,p_generation);
  new_applied:=c.applied_cursor;
  FOR candidate IN SELECT p.cursor,COALESCE(o.outcome,'missing') AS outcome
    FROM coordination.published_events p LEFT JOIN coordination.consumer_event_outcomes o
      ON (o.installation_id,o.module_id,o.tenant_id,o.project_id,o.generation,o.cursor)=
        (s.installation_id,p_module,s.tenant_id,s.project_id,p_generation,p.cursor)
    WHERE (p.installation_id,p.tenant_id,p.project_id)=(s.installation_id,s.tenant_id,s.project_id)
      AND p.cursor>c.applied_cursor AND p.cursor<=GREATEST(c.scan_cursor,p_cursor) ORDER BY p.cursor LOOP
    IF candidate.outcome NOT IN ('applied','stale') THEN EXIT; END IF;
    new_applied:=candidate.cursor;
  END LOOP;
  UPDATE coordination.consumer_project_checkpoints SET applied_cursor=GREATEST(applied_cursor,new_applied)
    WHERE (installation_id,module_id,tenant_id,project_id,generation)=
      (s.installation_id,p_module,s.tenant_id,s.project_id,p_generation);
  RETURN coordination.c07_status(p_module);
END $$;

CREATE FUNCTION coordination.c07_scan(p_module text,p_generation bigint,p_cursor bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,coordination,pg_temp AS $$
DECLARE s record; c record; f record;
BEGIN
  SELECT * INTO STRICT s FROM coordination.c07_scope();
  SELECT * INTO STRICT c FROM coordination.consumer_project_checkpoints
    WHERE (installation_id,module_id,tenant_id,project_id,generation)=
      (s.installation_id,p_module,s.tenant_id,s.project_id,p_generation) FOR UPDATE;
  SELECT * INTO STRICT f FROM coordination.feed_state WHERE installation_id=s.installation_id;
  IF c.state<>'active' OR p_cursor<c.scan_cursor OR p_cursor>f.last_published_cursor
    THEN RAISE EXCEPTION 'consumer_invalid_input'; END IF;
  IF EXISTS(SELECT 1 FROM coordination.published_events p
    WHERE (p.installation_id,p.tenant_id,p.project_id)=(s.installation_id,s.tenant_id,s.project_id)
      AND p.cursor>c.scan_cursor AND p.cursor<=p_cursor
      AND NOT EXISTS(SELECT 1 FROM coordination.consumer_event_outcomes o
        WHERE (o.installation_id,o.module_id,o.tenant_id,o.project_id,o.generation,o.cursor)=
          (s.installation_id,p_module,s.tenant_id,s.project_id,p_generation,p.cursor)))
    THEN RAISE EXCEPTION 'consumer_unrecorded_event'; END IF;
  UPDATE coordination.consumer_project_checkpoints SET scan_cursor=p_cursor,updated_at=clock_timestamp()
    WHERE (installation_id,module_id,tenant_id,project_id,generation)=
      (s.installation_id,p_module,s.tenant_id,s.project_id,p_generation);
  RETURN coordination.c07_status(p_module);
END $$;

GRANT INSERT ON coordination.consumer_checkpoints TO "kaidera-runtime-core-verifier";
GRANT SELECT,INSERT,UPDATE ON coordination.quarantine TO "kaidera-runtime-core-verifier";
CREATE POLICY c07_quarantine_private ON coordination.quarantine TO "kaidera-runtime-core-verifier" USING (true) WITH CHECK (true);
GRANT CREATE ON SCHEMA coordination TO "kaidera-runtime-core-verifier";
DO $$ DECLARE f record; BEGIN
  FOR f IN SELECT p.oid::regprocedure signature FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='coordination' AND p.proname LIKE 'c07_%' LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO "kaidera-runtime-core-verifier"',f.signature);
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,"kaidera-runtime-core-request"',f.signature);
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO "kaidera-runtime-core-request"',f.signature);
  END LOOP;
END $$;
REVOKE CREATE ON SCHEMA coordination FROM "kaidera-runtime-core-verifier";
