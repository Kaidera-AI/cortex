-- C06 additive capture and committed publication. Bootstrap is not runtime admission.
CREATE FUNCTION coordination.c06_bootstrap() RETURNS boolean
LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
    SELECT rolsuper FROM pg_roles WHERE rolname=session_user;
$$;

-- The request trigger must classify a scoped Core fact even for a write-only
-- principal; the public read policy intentionally hides that record.
CREATE FUNCTION coordination.c06_record_reserved(p_record uuid) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; reserved boolean;
BEGIN
    SELECT * INTO s FROM coordination.c05_scope();
    IF NOT FOUND THEN RAISE EXCEPTION 'private write context required' USING ERRCODE='42501'; END IF;
    SELECT EXISTS(SELECT 1 FROM core.records r
      WHERE r.tenant_id=s.tenant_id AND r.project_id=s.project_id AND r.id=p_record
        AND (r.kind='core' OR r.kind LIKE 'core.%')) INTO reserved;
    RETURN reserved;
END;
$$;

CREATE FUNCTION coordination.c06_reserved() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE reserved boolean;
BEGIN
    IF coordination.c06_bootstrap() OR current_user='kaidera-runtime-core-verifier' THEN RETURN NEW; END IF;
    IF TG_TABLE_NAME='record_aliases' THEN
        reserved:=NEW.source_namespace='cortex.core' OR NEW.source_namespace LIKE 'cortex.core.%';
        IF TG_OP='UPDATE' THEN reserved:=reserved OR OLD.source_namespace='cortex.core' OR OLD.source_namespace LIKE 'cortex.core.%'; END IF;
    ELSIF TG_TABLE_NAME='records' THEN
        reserved:=NEW.kind='core' OR NEW.kind LIKE 'core.%';
        IF TG_OP='UPDATE' THEN reserved:=reserved OR OLD.kind='core' OR OLD.kind LIKE 'core.%'; END IF;
    ELSE
        reserved:=coordination.c06_record_reserved(NEW.record_id);
    END IF;
    IF reserved THEN RAISE EXCEPTION 'reserved Core fact' USING ERRCODE='42501'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER c06_reserved_alias BEFORE INSERT OR UPDATE ON core.record_aliases
    FOR EACH ROW EXECUTE FUNCTION coordination.c06_reserved();
CREATE TRIGGER c06_reserved_record BEFORE INSERT OR UPDATE ON core.records
    FOR EACH ROW EXECUTE FUNCTION coordination.c06_reserved();
CREATE TRIGGER c06_reserved_revision BEFORE INSERT ON core.record_revisions
    FOR EACH ROW EXECUTE FUNCTION coordination.c06_reserved();

CREATE FUNCTION coordination.c06_capture_revision() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; k text; digest text;
BEGIN
    IF coordination.c06_bootstrap() THEN RETURN NEW; END IF;
    SELECT * INTO s FROM auth.identity_scope(false);
    IF NOT FOUND OR s.action NOT IN ('write','control') OR (s.tenant_id,s.project_id)<>(NEW.tenant_id,NEW.project_id)
       THEN RAISE EXCEPTION 'outbox_forbidden'; END IF;
    SELECT kind INTO STRICT k FROM core.records WHERE (tenant_id,project_id,id)=(NEW.tenant_id,NEW.project_id,NEW.record_id);
    SELECT sha256 INTO STRICT digest FROM core.payloads WHERE (tenant_id,project_id,id)=(NEW.tenant_id,NEW.project_id,NEW.payload_ref);
    INSERT INTO coordination.outbox(event_id,installation_id,tenant_id,project_id,aggregate_id,aggregate_kind,
      aggregate_revision,operation,tombstone,schema_version,payload_ref,payload_sha256,occurred_at)
    VALUES(gen_random_uuid(),s.installation_id,NEW.tenant_id,NEW.project_id,NEW.record_id,k,NEW.revision,
      CASE WHEN NEW.tombstone THEN 'delete' ELSE 'upsert' END,NEW.tombstone,1,NEW.payload_ref,digest,clock_timestamp());
    RETURN NEW;
END;
$$;
CREATE TRIGGER c06_capture_revision AFTER INSERT ON core.record_revisions
    FOR EACH ROW EXECUTE FUNCTION coordination.c06_capture_revision();

-- No caller supplies kind, alias, revision, installation, project, or event identity.
CREATE FUNCTION coordination.c06_fact(p_kind text,p_target uuid,p_payload uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; aggregate uuid; revision bigint; event uuid;
BEGIN
    SELECT * INTO s FROM auth.identity_scope(false);
    IF NOT FOUND OR s.action NOT IN ('write','control') OR p_kind NOT IN ('job','identity') OR p_target IS NULL OR p_payload IS NULL
       THEN RAISE EXCEPTION 'outbox_forbidden'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('c06:'||s.tenant_id||':'||s.project_id||':'||p_kind||':'||p_target,0));
    SELECT record_id INTO aggregate FROM core.record_aliases
      WHERE (tenant_id,project_id,source_namespace,external_id)=(s.tenant_id,s.project_id,'cortex.core.'||p_kind,p_target::text);
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

-- A writer receives only its newly captured event identity, never a read capability.
CREATE FUNCTION coordination.c06_record_event(p_record uuid,p_revision bigint) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; event uuid;
BEGIN
    SELECT * INTO STRICT s FROM coordination.c05_scope();
    SELECT o.event_id INTO event FROM coordination.outbox o
      WHERE o.installation_id=s.installation_id AND o.tenant_id=s.tenant_id AND o.project_id=s.project_id
        AND o.aggregate_id=p_record AND o.aggregate_revision=p_revision
        AND o.xmin::text=pg_current_xact_id()::text;
    RETURN event;
END;
$$;

CREATE FUNCTION coordination.c06_job_snapshot(p_job uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; j record; snapshot jsonb;
BEGIN
    SELECT * INTO s FROM auth.identity_scope(false);
    IF NOT FOUND OR s.action<>'write' THEN RAISE EXCEPTION 'outbox_forbidden'; END IF;
    SELECT * INTO STRICT j FROM coordination.jobs WHERE (tenant_id,project_id,id)=(s.tenant_id,s.project_id,p_job);
    snapshot:=jsonb_build_object('version',1,'kind','core.job','job_id',j.id,'job_kind',j.kind,
      'state',j.state,'cancel_requested',j.cancel_requested,'intent_payload_ref',j.payload_ref,
      'attempts',COALESCE((SELECT jsonb_agg(to_jsonb(a) ORDER BY a.attempt_number) FROM coordination.job_attempts a
        WHERE (a.tenant_id,a.project_id,a.job_id)=(s.tenant_id,s.project_id,p_job)),'[]'::jsonb),
      'results',COALESCE((SELECT jsonb_agg(to_jsonb(r) ORDER BY r.completed_at,r.attempt_id) FROM coordination.job_results r
        JOIN coordination.job_attempts a ON(a.tenant_id,a.project_id,a.id)=(r.tenant_id,r.project_id,r.attempt_id)
        WHERE (a.tenant_id,a.project_id,a.job_id)=(s.tenant_id,s.project_id,p_job)),'[]'::jsonb),
      'lease',(SELECT to_jsonb(l) FROM coordination.leases l
        WHERE (l.tenant_id,l.project_id,l.kind,l.resource_id)=(s.tenant_id,s.project_id,'job',p_job)));
    RETURN snapshot;
END;
$$;

CREATE FUNCTION coordination.capture_job(p_job uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; changed text; body bytea; payload uuid:=gen_random_uuid();
BEGIN
    SELECT * INTO s FROM auth.identity_scope(false);
    IF NOT FOUND OR s.action<>'write' THEN RAISE EXCEPTION 'outbox_forbidden'; END IF;
    SELECT business.xmin::text INTO STRICT changed FROM coordination.jobs business
      WHERE (business.tenant_id,business.project_id,business.id)=(s.tenant_id,s.project_id,p_job) FOR UPDATE;
    IF changed<>pg_current_xact_id()::text THEN RAISE EXCEPTION 'capture requires actual current job mutation' USING ERRCODE='23514'; END IF;
    body:=convert_to(coordination.c06_job_snapshot(p_job)::text,'UTF8');
    INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256)
      VALUES(s.tenant_id,s.project_id,payload,body,encode(sha256(body),'hex'));
    RETURN coordination.c06_fact('job',p_job,payload);
END;
$$;

-- Deferred guards make a raw business effect without its parent event uncommittable.
CREATE FUNCTION coordination.c06_job_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE job uuid;
BEGIN
    IF coordination.c06_bootstrap() THEN RETURN NULL; END IF;
    IF TG_OP='UPDATE' THEN
        IF TG_TABLE_NAME='jobs' THEN
            IF (NEW.tenant_id,NEW.project_id,NEW.id,NEW.kind,NEW.idempotency_key,NEW.created_at)<>
               (OLD.tenant_id,OLD.project_id,OLD.id,OLD.kind,OLD.idempotency_key,OLD.created_at)
              THEN RAISE EXCEPTION 'job identity is immutable' USING ERRCODE='23514'; END IF;
        ELSIF TG_TABLE_NAME='job_attempts' THEN
            IF (NEW.tenant_id,NEW.project_id,NEW.id,NEW.job_id,NEW.attempt_number,NEW.fence,NEW.worker_id,NEW.started_at)<>
               (OLD.tenant_id,OLD.project_id,OLD.id,OLD.job_id,OLD.attempt_number,OLD.fence,OLD.worker_id,OLD.started_at)
              THEN RAISE EXCEPTION 'attempt identity is immutable' USING ERRCODE='23514'; END IF;
        END IF;
    END IF;
    IF TG_TABLE_NAME='jobs' THEN job:=NEW.id;
    ELSIF TG_TABLE_NAME='job_attempts' THEN job:=NEW.job_id;
    ELSIF TG_TABLE_NAME='job_results' THEN
        SELECT job_id INTO job FROM coordination.job_attempts WHERE (tenant_id,project_id,id)=(NEW.tenant_id,NEW.project_id,NEW.attempt_id);
    ELSE
        IF TG_OP='UPDATE' AND (NEW.tenant_id,NEW.project_id,NEW.kind,NEW.resource_id)<>(OLD.tenant_id,OLD.project_id,OLD.kind,OLD.resource_id)
          THEN RAISE EXCEPTION 'lease identity is immutable' USING ERRCODE='23514'; END IF;
        IF NEW.kind<>'job' THEN RETURN NULL; END IF; job:=NEW.resource_id;
    END IF;
    IF NOT EXISTS(SELECT 1 FROM core.record_aliases a JOIN coordination.outbox o
        ON(o.tenant_id,o.project_id,o.aggregate_id)=(a.tenant_id,a.project_id,a.record_id)
        JOIN core.records h ON(h.tenant_id,h.project_id,h.id,h.current_revision)=(o.tenant_id,o.project_id,o.aggregate_id,o.aggregate_revision)
        JOIN core.payloads p ON(p.tenant_id,p.project_id,p.id)=(o.tenant_id,o.project_id,o.payload_ref)
        WHERE (a.tenant_id,a.project_id,a.source_namespace,a.external_id)=(NEW.tenant_id,NEW.project_id,'cortex.core.job',job::text)
          AND o.xmin::text=pg_current_xact_id()::text
          AND p.body=convert_to(coordination.c06_job_snapshot(job)::text,'UTF8'))
      THEN RAISE EXCEPTION 'job mutation requires captured event' USING ERRCODE='23514'; END IF;
    RETURN NULL;
END;
$$;
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['jobs','job_attempts','job_results','leases'] LOOP
        EXECUTE format('CREATE CONSTRAINT TRIGGER c06_job_guard AFTER INSERT OR UPDATE ON coordination.%I DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION coordination.c06_job_guard()',t);
    END LOOP;
END; $$;

CREATE OR REPLACE FUNCTION auth.identity_finish(p_key text,p_sha text,p_receipt jsonb,p_audit jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; audit_id uuid:=gen_random_uuid(); body bytea; receipt jsonb; target jsonb; event uuid; events jsonb:='[]';
BEGIN
    SELECT * INTO s FROM auth.identity_scope(true);
    IF NOT FOUND THEN RAISE EXCEPTION 'identity_forbidden'; END IF;
    body:=convert_to((p_audit||jsonb_build_object('actor',s.principal_id,'project',s.project_id,
                                               'request_sha256',p_sha))::text,'UTF8');
    INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256)
      VALUES(s.tenant_id,s.project_id,audit_id,body,encode(sha256(body),'hex'));
    IF p_audit->>'operation'='adopt' THEN
        FOR target IN SELECT * FROM jsonb_array_elements(p_audit->'manifest'->'agents') LOOP
            event:=coordination.c06_fact('identity',(target->>'principal_id')::uuid,audit_id);
            events:=events||jsonb_build_array(jsonb_build_object('principal_id',target->>'principal_id','event_id',event));
        END LOOP;
    ELSE
        event:=coordination.c06_fact('identity',(p_audit->>'principal_id')::uuid,audit_id);
        events:=jsonb_build_array(jsonb_build_object('principal_id',p_audit->>'principal_id','event_id',event));
    END IF;
    receipt:=p_receipt||jsonb_build_object('audit_id',audit_id,'replayed',false,'event_id',event,'events',events);
    IF p_audit->>'operation'='set_roles' THEN
        receipt:=receipt||jsonb_build_object('identity',(SELECT jsonb_build_object('principal_id',p.id,'name',p.name,
          'kind',p.kind,'adopted',p.identity_adopted,'roles',g.roles,'generation',gen.generation)
          FROM auth.principals p JOIN auth.project_grants g ON(g.tenant_id,g.principal_id)=(p.tenant_id,p.id)
          JOIN auth.permission_generations gen ON(gen.tenant_id,gen.project_id)=(g.tenant_id,g.project_id)
          WHERE p.tenant_id=s.tenant_id AND p.id=(p_audit->>'principal_id')::uuid AND g.project_id=s.project_id));
    END IF;
    INSERT INTO coordination.idempotency(tenant_id,project_id,principal_id,request_key,request_sha256,outcome,receipt)
      VALUES(s.tenant_id,s.project_id,s.principal_id,p_key,p_sha,'committed',receipt);
    RETURN receipt;
END;
$$;

CREATE FUNCTION coordination.c06_identity_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; target uuid;
BEGIN
    IF coordination.c06_bootstrap() THEN RETURN NULL; END IF;
    SELECT * INTO s FROM auth.identity_scope(true);
    IF NOT FOUND THEN RAISE EXCEPTION 'identity_forbidden'; END IF;
    IF TG_TABLE_NAME='principals' THEN target:=NEW.id; ELSE target:=NEW.principal_id; END IF;
    IF TG_TABLE_NAME='project_grants' THEN
        IF NEW.project_id<>s.project_id THEN RAISE EXCEPTION 'identity_forbidden'; END IF;
    END IF;
    IF NEW.tenant_id<>s.tenant_id OR NOT EXISTS(SELECT 1 FROM core.record_aliases a JOIN coordination.outbox o
        ON(o.tenant_id,o.project_id,o.aggregate_id)=(a.tenant_id,a.project_id,a.record_id)
        WHERE (a.tenant_id,a.project_id,a.source_namespace,a.external_id)=(s.tenant_id,s.project_id,'cortex.core.identity',target::text)
          AND o.xmin::text=pg_current_xact_id()::text)
      THEN RAISE EXCEPTION 'identity mutation requires captured event' USING ERRCODE='23514'; END IF;
    RETURN NULL;
END;
$$;
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['principals','project_grants','credentials'] LOOP
        EXECUTE format('CREATE CONSTRAINT TRIGGER c06_identity_guard AFTER INSERT OR UPDATE ON auth.%I DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION coordination.c06_identity_guard()',t);
    END LOOP;
END; $$;

CREATE FUNCTION coordination.publish_outbox(p_limit integer)
RETURNS TABLE(delivery_cursor bigint,envelope jsonb,payload bytea)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; state record; item record; cursor_value bigint;
BEGIN
    SELECT * INTO s FROM auth.identity_scope(true);
    IF NOT FOUND THEN RAISE EXCEPTION 'outbox_forbidden'; END IF;
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000 THEN RAISE EXCEPTION 'outbox_invalid_input'; END IF;
    INSERT INTO coordination.feed_state(installation_id) VALUES(s.installation_id) ON CONFLICT DO NOTHING;
    SELECT * INTO STRICT state FROM coordination.feed_state WHERE installation_id=s.installation_id FOR UPDATE;
    cursor_value:=state.last_published_cursor;
    FOR item IN SELECT o.* FROM coordination.outbox o
      WHERE (o.installation_id,o.tenant_id,o.project_id)=(s.installation_id,s.tenant_id,s.project_id)
        AND NOT EXISTS(SELECT 1 FROM coordination.published_events p WHERE p.event_id=o.event_id)
      ORDER BY o.occurred_at,o.event_id LIMIT p_limit LOOP
        cursor_value:=cursor_value+1;
        INSERT INTO coordination.published_events(installation_id,cursor,tenant_id,project_id,event_id,published_at)
          VALUES(s.installation_id,cursor_value,item.tenant_id,item.project_id,item.event_id,clock_timestamp());
        RETURN QUERY SELECT cursor_value,to_jsonb(item),p.body FROM core.payloads p
          WHERE (p.tenant_id,p.project_id,p.id)=(item.tenant_id,item.project_id,item.payload_ref);
    END LOOP;
    UPDATE coordination.feed_state SET last_published_cursor=cursor_value WHERE installation_id=s.installation_id;
END;
$$;

CREATE FUNCTION coordination.outbox_page(p_after bigint,p_limit integer)
RETURNS TABLE(delivery_cursor bigint,envelope jsonb,payload bytea,head bigint,floor bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; state record;
BEGIN
    SELECT * INTO s FROM auth.identity_scope(false);
    IF NOT FOUND OR s.action<>'read' THEN RAISE EXCEPTION 'outbox_forbidden'; END IF;
    IF p_after IS NULL OR p_after<0 OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000 THEN RAISE EXCEPTION 'outbox_invalid_input'; END IF;
    SELECT * INTO state FROM coordination.feed_state WHERE installation_id=s.installation_id FOR SHARE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT NULL::bigint,NULL::jsonb,NULL::bytea,0::bigint,0::bigint; RETURN;
    END IF;
    IF p_after<state.retained_floor THEN RAISE EXCEPTION 'outbox_expired'; END IF;
    RETURN QUERY SELECT e.cursor,e.env,e.body,state.last_published_cursor,state.retained_floor
      FROM (SELECT 1) seed LEFT JOIN LATERAL (
        SELECT p.cursor,to_jsonb(o) env,b.body FROM coordination.published_events p
        JOIN coordination.outbox o ON(o.installation_id,o.tenant_id,o.project_id,o.event_id)=(p.installation_id,p.tenant_id,p.project_id,p.event_id)
        JOIN core.payloads b ON(b.tenant_id,b.project_id,b.id)=(o.tenant_id,o.project_id,o.payload_ref)
        WHERE (p.installation_id,p.tenant_id,p.project_id)=(s.installation_id,s.tenant_id,s.project_id)
          AND p.cursor>p_after ORDER BY p.cursor LIMIT p_limit) e ON true;
END;
$$;

CREATE FUNCTION coordination.prune_outbox() RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; state record; item record; snapshot bigint; floor_value bigint; blocked text; removed integer:=0;
BEGIN
    SELECT * INTO s FROM auth.identity_scope(true);
    IF NOT FOUND THEN RAISE EXCEPTION 'outbox_forbidden'; END IF;
    INSERT INTO coordination.feed_state(installation_id) VALUES(s.installation_id) ON CONFLICT DO NOTHING;
    SELECT * INTO STRICT state FROM coordination.feed_state WHERE installation_id=s.installation_id FOR UPDATE;
    UPDATE coordination.consumer_checkpoints AS consumer SET state='expired'
      WHERE installation_id=s.installation_id AND expires_at<=clock_timestamp() AND consumer.state<>'expired';
    SELECT min(cursor) INTO snapshot FROM coordination.snapshot_floors
      WHERE installation_id=s.installation_id AND expires_at>clock_timestamp();
    floor_value:=state.retained_floor;
    FOR item IN SELECT * FROM coordination.published_events WHERE installation_id=s.installation_id
      AND cursor>floor_value ORDER BY cursor LIMIT 1000 LOOP
        IF item.cursor<>floor_value+1 THEN blocked:='journal_gap'; EXIT; END IF;
        IF (item.tenant_id,item.project_id)<>(s.tenant_id,s.project_id) THEN blocked:='foreign_scope'; EXIT; END IF;
        IF item.published_at>clock_timestamp()-make_interval(secs=>state.retention_seconds) THEN EXIT; END IF;
        IF snapshot IS NOT NULL AND item.cursor>snapshot THEN blocked:='snapshot_floor'; EXIT; END IF;
        IF EXISTS(SELECT 1 FROM coordination.quarantine q WHERE (q.tenant_id,q.project_id,q.event_id)=(item.tenant_id,item.project_id,item.event_id)
             AND q.resolved_at IS NULL) THEN blocked:='quarantine'; EXIT; END IF;
        DELETE FROM coordination.published_events WHERE (installation_id,cursor)=(s.installation_id,item.cursor);
        DELETE FROM coordination.quarantine WHERE (tenant_id,project_id,event_id)=(item.tenant_id,item.project_id,item.event_id);
        DELETE FROM coordination.outbox WHERE (tenant_id,project_id,event_id)=(item.tenant_id,item.project_id,item.event_id);
        floor_value:=item.cursor; removed:=removed+1;
    END LOOP;
    UPDATE coordination.feed_state SET retained_floor=floor_value WHERE installation_id=s.installation_id;
    UPDATE coordination.consumer_checkpoints AS consumer SET state='expired'
      WHERE installation_id=s.installation_id AND applied_cursor<floor_value AND consumer.state<>'expired';
    RETURN jsonb_build_object('removed',removed,'floor',floor_value,'head',state.last_published_cursor,'blocked',blocked);
END;
$$;

-- Preserve an append-only sequential record head and a current captured history.
CREATE FUNCTION coordination.c06_record_head() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
BEGIN
    IF coordination.c06_bootstrap() THEN RETURN NEW; END IF;
    IF (TG_OP='INSERT' AND NEW.current_revision<>1) OR (TG_OP='UPDATE' AND
        ((NEW.tenant_id,NEW.project_id,NEW.id,NEW.kind)<>(OLD.tenant_id,OLD.project_id,OLD.id,OLD.kind)
         OR NEW.current_revision<>OLD.current_revision+1))
      THEN RAISE EXCEPTION 'record head must advance exactly one revision' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER c06_record_head BEFORE INSERT OR UPDATE ON core.records
    FOR EACH ROW EXECUTE FUNCTION coordination.c06_record_head();
CREATE FUNCTION coordination.c06_revision_order() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE head record;
BEGIN
    IF coordination.c06_bootstrap() THEN RETURN NEW; END IF;
    SELECT current_revision,tombstone INTO STRICT head FROM core.records
      WHERE (tenant_id,project_id,id)=(NEW.tenant_id,NEW.project_id,NEW.record_id) FOR UPDATE;
    IF NEW.revision IS DISTINCT FROM head.current_revision OR NEW.tombstone IS DISTINCT FROM head.tombstone
       OR (NEW.revision>1 AND NOT EXISTS(SELECT 1 FROM core.record_revisions
           WHERE (tenant_id,project_id,record_id,revision)=(NEW.tenant_id,NEW.project_id,NEW.record_id,NEW.revision-1)))
      THEN RAISE EXCEPTION 'history must match sequential authoritative head' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER c06_revision_order BEFORE INSERT ON core.record_revisions
    FOR EACH ROW EXECUTE FUNCTION coordination.c06_revision_order();
CREATE FUNCTION coordination.c06_record_integrity() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE aggregate uuid; head bigint; maximum bigint;
BEGIN
    IF coordination.c06_bootstrap() THEN RETURN NULL; END IF;
    IF TG_TABLE_NAME='records' THEN aggregate:=NEW.id; ELSE aggregate:=NEW.record_id; END IF;
    SELECT current_revision INTO STRICT head FROM core.records WHERE (tenant_id,project_id,id)=(NEW.tenant_id,NEW.project_id,aggregate);
    SELECT max(revision) INTO maximum FROM core.record_revisions WHERE (tenant_id,project_id,record_id)=(NEW.tenant_id,NEW.project_id,aggregate);
    IF head IS DISTINCT FROM maximum OR NOT EXISTS(SELECT 1 FROM coordination.outbox
        WHERE (tenant_id,project_id,aggregate_id,aggregate_revision)=(NEW.tenant_id,NEW.project_id,aggregate,head)
          AND xmin::text=pg_current_xact_id()::text)
      THEN RAISE EXCEPTION 'record head requires matching current event' USING ERRCODE='23514'; END IF;
    IF TG_TABLE_NAME='record_revisions' THEN
        IF NEW.revision>1 AND NOT EXISTS(SELECT 1 FROM core.record_revisions
            WHERE (tenant_id,project_id,record_id,revision)=(NEW.tenant_id,NEW.project_id,aggregate,NEW.revision-1))
          THEN RAISE EXCEPTION 'history requires previous revision' USING ERRCODE='23514'; END IF;
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER c06_record_integrity AFTER INSERT OR UPDATE ON core.records
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION coordination.c06_record_integrity();
CREATE CONSTRAINT TRIGGER c06_history_integrity AFTER INSERT ON core.record_revisions
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION coordination.c06_record_integrity();

-- Exact request bytes bind the durable ACK to the captured port effect.
CREATE FUNCTION coordination.c06_validate_request(receipt jsonb,request_sha text,event jsonb,actor uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE raw text; request jsonb; operation text; arguments jsonb; snapshot jsonb;
  attempt jsonb; result jsonb; metadata jsonb; expected_state text; expected_reason text;
BEGIN
    raw:=receipt->>'request_json'; request:=raw::jsonb;
    IF raw IS NULL OR encode(sha256(convert_to(raw,'UTF8')),'hex') IS DISTINCT FROM request_sha
       OR jsonb_typeof(request) IS DISTINCT FROM 'array'
      THEN RAISE EXCEPTION 'receipt requires exact request contract' USING ERRCODE='23514'; END IF;
    operation:=request->>0;
    IF receipt ? 'record_id' THEN
        IF receipt ? 'job_id' OR receipt ? 'type' OR jsonb_array_length(request)<>5
           OR operation NOT IN ('put','delete') OR request->>1 IS DISTINCT FROM receipt->>'record_id'
           OR (request->>4)::bigint IS DISTINCT FROM (event->>'aggregate_revision')::bigint-1
           OR (receipt->>'tombstone')::boolean IS DISTINCT FROM (operation='delete')
           OR event->>'aggregate_kind'='core' OR event->>'aggregate_kind' LIKE 'core.%'
           OR (operation='put' AND (request->>2 IS DISTINCT FROM event->>'aggregate_kind'
                                  OR request->>3 IS DISTINCT FROM event->>'payload_sha256'))
           OR (operation='delete' AND (request->2 IS DISTINCT FROM 'null'::jsonb OR request->3 IS DISTINCT FROM 'null'::jsonb))
          THEN RAISE EXCEPTION 'record receipt request mismatch' USING ERRCODE='23514'; END IF;
        RETURN;
    END IF;
    IF NOT receipt ? 'job_id' OR receipt ? 'record_id' OR jsonb_array_length(request)<>3
       OR request->>1 IS DISTINCT FROM receipt->>'job_id' OR event->>'aggregate_kind' IS DISTINCT FROM 'core.job'
       OR jsonb_typeof(request->2) IS DISTINCT FROM 'array'
      THEN RAISE EXCEPTION 'job receipt request mismatch' USING ERRCODE='23514'; END IF;
    arguments:=request->2;
    SELECT convert_from(body,'UTF8')::jsonb INTO STRICT snapshot FROM core.payloads
      WHERE (tenant_id,project_id,id)=((event->>'tenant_id')::uuid,(event->>'project_id')::uuid,(event->>'payload_ref')::uuid);
    attempt:=snapshot->'attempts'->-1;
    SELECT convert_from(body,'UTF8')::jsonb INTO STRICT metadata FROM core.payloads
      WHERE (tenant_id,project_id,id)=((event->>'tenant_id')::uuid,(event->>'project_id')::uuid,(snapshot->>'intent_payload_ref')::uuid);
    IF operation='job.claim' THEN
        IF receipt->>'type' IS DISTINCT FROM 'claim' OR snapshot->>'state' IS DISTINCT FROM 'running'
           OR jsonb_array_length(arguments)<>1
           OR receipt->>'attempt_id' IS DISTINCT FROM attempt->>'id'
           OR receipt->'attempt_number' IS DISTINCT FROM attempt->'attempt_number'
           OR receipt->'fence' IS DISTINCT FROM attempt->'fence'
           OR receipt->>'holder' IS DISTINCT FROM attempt->>'worker_id'
           OR receipt->>'holder' IS DISTINCT FROM actor::text
           OR receipt->>'holder' IS DISTINCT FROM snapshot->'lease'->>'holder'
           OR receipt->'fence' IS DISTINCT FROM snapshot->'lease'->'fence'
           OR EXTRACT(epoch FROM (snapshot->'lease'->>'expires_at')::timestamptz-(attempt->>'started_at')::timestamptz) IS DISTINCT FROM (arguments->>0)::numeric
          THEN RAISE EXCEPTION 'claim receipt snapshot mismatch' USING ERRCODE='23514'; END IF;
        RETURN;
    END IF;
    expected_state:=CASE operation WHEN 'job.create' THEN 'pending' WHEN 'job.complete' THEN 'succeeded'
      WHEN 'job.cancel' THEN 'canceled' WHEN 'job.expire' THEN 'unresolved' WHEN 'job.retry' THEN 'pending'
      WHEN 'job.release' THEN 'unresolved' WHEN 'job.abandon' THEN 'canceled' WHEN 'job.fail' THEN 'failed'
      WHEN 'job.return' THEN 'running' WHEN 'job.accept' THEN 'succeeded' WHEN 'job.rework' THEN 'failed' END;
    expected_reason:=CASE operation WHEN 'job.create' THEN 'created' WHEN 'job.complete' THEN 'completed'
      WHEN 'job.cancel' THEN 'canceled' WHEN 'job.expire' THEN 'expired_unproven' WHEN 'job.retry' THEN 'explicit_retry'
      WHEN 'job.release' THEN 'released_unproven' WHEN 'job.abandon' THEN 'abandoned' WHEN 'job.fail' THEN 'failed'
      WHEN 'job.return' THEN 'returned' WHEN 'job.accept' THEN 'accepted' WHEN 'job.rework' THEN 'rework' END;
    IF expected_state IS NULL OR receipt->>'type' IS DISTINCT FROM 'job'
       OR receipt->>'state' IS DISTINCT FROM expected_state OR snapshot->>'state' IS DISTINCT FROM expected_state
       OR receipt->>'reason' IS DISTINCT FROM expected_reason
      THEN RAISE EXCEPTION 'job receipt state mismatch' USING ERRCODE='23514'; END IF;
    IF operation='job.create' THEN
        IF jsonb_array_length(arguments) NOT IN (3,4) OR arguments->>0 IS DISTINCT FROM snapshot->>'job_kind'
           OR arguments->2 IS DISTINCT FROM metadata->'recipient' OR metadata->>'creator' IS DISTINCT FROM actor::text
           OR jsonb_array_length(snapshot->'attempts')<>0
           OR (jsonb_array_length(arguments)=4 AND arguments->3 IS DISTINCT FROM jsonb_build_object(
                'recipient_role',metadata->'recipient_role','human_review',metadata->'human_review'))
           OR (jsonb_array_length(arguments)=3 AND (metadata->'recipient_role' IS DISTINCT FROM 'null'::jsonb OR metadata->'human_review' IS DISTINCT FROM 'false'::jsonb))
           OR NOT EXISTS(SELECT 1 FROM core.payloads WHERE (tenant_id,project_id,id)=
                ((event->>'tenant_id')::uuid,(event->>'project_id')::uuid,(metadata->>'body_payload')::uuid) AND sha256=arguments->>1)
          THEN RAISE EXCEPTION 'create receipt intent mismatch' USING ERRCODE='23514'; END IF;
    ELSIF operation IN ('job.cancel','job.expire','job.retry') THEN
        IF jsonb_array_length(arguments)<>0 THEN RAISE EXCEPTION 'job receipt argument mismatch' USING ERRCODE='23514'; END IF;
        IF operation='job.retry' AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(snapshot->'results') r
            WHERE r->>'attempt_id'=attempt->>'id' AND r->>'outcome' IN ('failed','unresolved'))
          THEN RAISE EXCEPTION 'retry requires prior terminal result' USING ERRCODE='23514'; END IF;
    ELSE
        IF operation IN ('job.complete','job.release','job.abandon','job.fail','job.return') AND
           (actor::text IS DISTINCT FROM attempt->>'worker_id' OR actor::text IS DISTINCT FROM snapshot->'lease'->>'holder')
          THEN RAISE EXCEPTION 'worker receipt actor mismatch' USING ERRCODE='23514'; END IF;
        IF operation IN ('job.accept','job.rework') AND actor::text=attempt->>'worker_id'
          THEN RAISE EXCEPTION 'review receipt requires independent actor' USING ERRCODE='23514'; END IF;
        IF jsonb_array_length(arguments) IS DISTINCT FROM (CASE WHEN operation IN ('job.accept','job.rework') THEN 2 ELSE 3 END)
           OR arguments->>0 IS DISTINCT FROM attempt->>'id' OR arguments->1 IS DISTINCT FROM attempt->'fence'
          THEN RAISE EXCEPTION 'job receipt attempt mismatch' USING ERRCODE='23514'; END IF;
        IF operation='job.return' THEN
            IF metadata->'review'->>'attempt' IS DISTINCT FROM arguments->>0
               OR metadata->'review'->'fence' IS DISTINCT FROM arguments->1
               OR metadata->'review'->>'holder' IS DISTINCT FROM actor::text
               OR NOT EXISTS(SELECT 1 FROM core.payloads WHERE (tenant_id,project_id,id)=
                    ((event->>'tenant_id')::uuid,(event->>'project_id')::uuid,(metadata->'review'->>'payload')::uuid) AND sha256=arguments->>2)
              THEN RAISE EXCEPTION 'return receipt payload mismatch' USING ERRCODE='23514'; END IF;
        ELSE
            SELECT r INTO result FROM jsonb_array_elements(snapshot->'results') r
              WHERE r->>'attempt_id'=arguments->>0 AND r->>'outcome'=expected_state;
            IF result IS NULL OR (jsonb_array_length(arguments)=3 AND NOT EXISTS(SELECT 1 FROM core.payloads WHERE
                (tenant_id,project_id,id)=((event->>'tenant_id')::uuid,(event->>'project_id')::uuid,(result->>'payload_ref')::uuid)
                  AND sha256=arguments->>2))
              THEN RAISE EXCEPTION 'terminal receipt result mismatch' USING ERRCODE='23514'; END IF;
        END IF;
    END IF;
END;
$$;

CREATE FUNCTION coordination.c06_receipt_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; e record; target jsonb;
BEGIN
    IF coordination.c06_bootstrap() THEN RETURN NEW; END IF;
    IF TG_OP='UPDATE' THEN RAISE EXCEPTION 'mutation receipts are immutable' USING ERRCODE='23514'; END IF;
    SELECT * INTO s FROM auth.identity_scope(false);
    IF NOT FOUND OR (NEW.tenant_id,NEW.project_id,NEW.principal_id)<>(s.tenant_id,s.project_id,s.principal_id)
       THEN RAISE EXCEPTION 'receipt scope is forbidden' USING ERRCODE='42501'; END IF;
    IF NEW.outcome<>'committed' THEN RETURN NEW; END IF;
    SELECT * INTO e FROM coordination.outbox WHERE (tenant_id,project_id,event_id)=
      (s.tenant_id,s.project_id,(NEW.receipt->>'event_id')::uuid) AND xmin::text=pg_current_xact_id()::text;
    IF NOT FOUND THEN RAISE EXCEPTION 'receipt requires captured mutation' USING ERRCODE='23514'; END IF;
    IF NEW.receipt ? 'record_id' OR NEW.receipt ? 'job_id' THEN
        PERFORM coordination.c06_validate_request(NEW.receipt,NEW.request_sha256,to_jsonb(e),s.principal_id);
    END IF;
    IF NEW.receipt ? 'record_id' THEN
        IF e.aggregate_id IS DISTINCT FROM (NEW.receipt->>'record_id')::uuid
           OR e.aggregate_revision IS DISTINCT FROM (NEW.receipt->>'revision')::bigint
           OR e.tombstone IS DISTINCT FROM (NEW.receipt->>'tombstone')::boolean
           OR e.payload_sha256 IS DISTINCT FROM NEW.receipt->>'payload_sha256'
          THEN RAISE EXCEPTION 'receipt does not match captured record' USING ERRCODE='23514'; END IF;
    ELSIF NEW.receipt ? 'job_id' THEN
        IF NOT EXISTS(SELECT 1 FROM core.record_aliases WHERE (tenant_id,project_id,source_namespace,external_id,record_id)=
             (s.tenant_id,s.project_id,'cortex.core.job',NEW.receipt->>'job_id',e.aggregate_id))
          THEN RAISE EXCEPTION 'receipt does not match captured job' USING ERRCODE='23514'; END IF;
    ELSE
        IF s.action<>'control' OR jsonb_typeof(NEW.receipt->'events') IS DISTINCT FROM 'array'
          OR jsonb_array_length(NEW.receipt->'events')<1 THEN RAISE EXCEPTION 'receipt does not match captured identity' USING ERRCODE='23514'; END IF;
        FOR target IN SELECT * FROM jsonb_array_elements(NEW.receipt->'events') LOOP
            IF NOT EXISTS(SELECT 1 FROM core.record_aliases a JOIN coordination.outbox o
                ON(o.tenant_id,o.project_id,o.aggregate_id)=(a.tenant_id,a.project_id,a.record_id)
                WHERE (a.tenant_id,a.project_id,a.source_namespace,a.external_id,o.event_id)=
                  (s.tenant_id,s.project_id,'cortex.core.identity',target->>'principal_id',(target->>'event_id')::uuid)
                  AND o.xmin::text=pg_current_xact_id()::text)
              THEN RAISE EXCEPTION 'receipt does not match captured identity' USING ERRCODE='23514'; END IF;
        END LOOP;
    END IF;
    RETURN NEW;
EXCEPTION WHEN invalid_text_representation THEN RAISE EXCEPTION 'receipt requires captured mutation' USING ERRCODE='23514';
END;
$$;
CREATE TRIGGER c06_receipt_guard BEFORE INSERT OR UPDATE ON coordination.idempotency
    FOR EACH ROW EXECUTE FUNCTION coordination.c06_receipt_guard();

-- Protection creation and pruning use the same installation serialization point.
-- Even trusted maintenance callers must take this lock; no bootstrap exception.
CREATE FUNCTION coordination.c06_protection_lock() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE installation uuid; state_row record;
BEGIN
    IF TG_TABLE_NAME='quarantine' THEN
        SELECT installation_id INTO STRICT installation FROM core.tenants WHERE id=NEW.tenant_id;
    ELSE installation:=NEW.installation_id; END IF;
    INSERT INTO coordination.feed_state(installation_id) VALUES(installation) ON CONFLICT DO NOTHING;
    SELECT * INTO STRICT state_row FROM coordination.feed_state WHERE installation_id=installation FOR UPDATE;
    IF TG_TABLE_NAME='snapshot_floors' THEN
        IF NEW.cursor<state_row.retained_floor THEN RAISE EXCEPTION 'outbox_expired'; END IF;
        IF NEW.cursor>state_row.last_published_cursor THEN RAISE EXCEPTION 'outbox_invalid_input'; END IF;
    ELSIF TG_TABLE_NAME='consumer_checkpoints' THEN
        IF TG_OP='UPDATE' THEN
            IF (OLD.state='expired' OR OLD.expires_at<=clock_timestamp()) AND
               (NEW.applied_cursor>OLD.applied_cursor OR NEW.state='active')
              THEN RAISE EXCEPTION 'outbox_expired'; END IF;
        END IF;
        IF NEW.state='active' AND NEW.applied_cursor<state_row.retained_floor THEN RAISE EXCEPTION 'outbox_expired'; END IF;
        IF NEW.applied_cursor>state_row.last_published_cursor THEN RAISE EXCEPTION 'outbox_invalid_input'; END IF;
    END IF;
    RETURN NEW;
END;
$$;
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['quarantine','snapshot_floors','consumer_checkpoints'] LOOP
        EXECUTE format('CREATE TRIGGER c06_protection_lock BEFORE INSERT OR UPDATE ON coordination.%I FOR EACH ROW EXECUTE FUNCTION coordination.c06_protection_lock()',t);
    END LOOP;
END; $$;

-- Private verifier capabilities; no runtime membership or table bypass is added.
GRANT SELECT,INSERT,UPDATE ON core.records,core.record_aliases TO "kaidera-runtime-core-verifier";
GRANT SELECT,INSERT ON core.record_revisions TO "kaidera-runtime-core-verifier";
GRANT SELECT,INSERT,DELETE ON coordination.outbox,coordination.published_events TO "kaidera-runtime-core-verifier";
GRANT SELECT,INSERT,UPDATE ON coordination.feed_state TO "kaidera-runtime-core-verifier";
GRANT SELECT,UPDATE ON coordination.consumer_checkpoints TO "kaidera-runtime-core-verifier";
GRANT SELECT ON coordination.snapshot_floors TO "kaidera-runtime-core-verifier";
GRANT SELECT,DELETE ON coordination.quarantine TO "kaidera-runtime-core-verifier";
GRANT SELECT,UPDATE ON coordination.jobs TO "kaidera-runtime-core-verifier";
GRANT SELECT ON coordination.job_attempts,coordination.job_results,coordination.leases TO "kaidera-runtime-core-verifier";
REVOKE INSERT,UPDATE ON coordination.outbox,core.record_aliases,core.blob_manifests,core.extraction_facts,core.analytics_facts FROM "kaidera-runtime-core-request";
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['core.records','core.record_aliases','core.record_revisions','coordination.outbox','coordination.published_events',
      'coordination.feed_state','coordination.consumer_checkpoints','coordination.snapshot_floors','coordination.quarantine',
      'coordination.jobs','coordination.job_attempts','coordination.job_results','coordination.leases'] LOOP
        EXECUTE format('CREATE POLICY c06_private ON %s TO "kaidera-runtime-core-verifier" USING(true) WITH CHECK(true)',t);
    END LOOP;
END; $$;
GRANT CREATE ON SCHEMA coordination TO "kaidera-runtime-core-verifier";
DO $$ DECLARE f record; BEGIN
    FOR f IN SELECT p.oid::regprocedure signature FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='coordination' AND (p.proname LIKE 'c06_%' OR p.proname IN ('capture_job','publish_outbox','outbox_page','prune_outbox')) LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO "kaidera-runtime-core-verifier"',f.signature);
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,"kaidera-runtime-core-request"',f.signature);
    END LOOP;
END; $$;
REVOKE CREATE ON SCHEMA coordination FROM "kaidera-runtime-core-verifier";
REVOKE ALL ON FUNCTION auth.identity_finish(text,text,jsonb,jsonb) FROM PUBLIC,"kaidera-runtime-core-request";
GRANT EXECUTE ON FUNCTION coordination.c06_bootstrap(),coordination.c06_reserved(),coordination.c06_record_reserved(uuid),coordination.capture_job(uuid),coordination.c06_record_event(uuid,bigint),
  coordination.publish_outbox(integer),coordination.outbox_page(bigint,integer),coordination.prune_outbox()
  TO "kaidera-runtime-core-request";
