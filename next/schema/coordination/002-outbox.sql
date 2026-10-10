-- C06 additive capture and committed publication. Bootstrap is not runtime admission.
CREATE FUNCTION coordination.c06_bootstrap() RETURNS boolean
LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
    SELECT rolsuper FROM pg_roles WHERE rolname=session_user;
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
        SELECT kind='core' OR kind LIKE 'core.%' INTO reserved FROM core.records
          WHERE (tenant_id,project_id,id)=(NEW.tenant_id,NEW.project_id,NEW.record_id);
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

CREATE FUNCTION coordination.capture_job(p_job uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,auth,core,coordination,pg_temp AS $$
DECLARE s record; j record; snapshot jsonb; body bytea; payload uuid:=gen_random_uuid();
BEGIN
    SELECT * INTO s FROM auth.identity_scope(false);
    IF NOT FOUND OR s.action<>'write' THEN RAISE EXCEPTION 'outbox_forbidden'; END IF;
    SELECT * INTO STRICT j FROM coordination.jobs WHERE (tenant_id,project_id,id)=(s.tenant_id,s.project_id,p_job) FOR UPDATE;
    snapshot:=jsonb_build_object('version',1,'kind','core.job','job_id',j.id,'job_kind',j.kind,
      'state',j.state,'cancel_requested',j.cancel_requested,'intent_payload_ref',j.payload_ref,
      'attempts',COALESCE((SELECT jsonb_agg(to_jsonb(a) ORDER BY a.attempt_number) FROM coordination.job_attempts a
        WHERE (a.tenant_id,a.project_id,a.job_id)=(s.tenant_id,s.project_id,p_job)),'[]'::jsonb),
      'results',COALESCE((SELECT jsonb_agg(to_jsonb(r) ORDER BY r.completed_at,r.attempt_id) FROM coordination.job_results r
        JOIN coordination.job_attempts a ON(a.tenant_id,a.project_id,a.id)=(r.tenant_id,r.project_id,r.attempt_id)
        WHERE (a.tenant_id,a.project_id,a.job_id)=(s.tenant_id,s.project_id,p_job)),'[]'::jsonb),
      'lease',(SELECT to_jsonb(l) FROM coordination.leases l
        WHERE (l.tenant_id,l.project_id,l.kind,l.resource_id)=(s.tenant_id,s.project_id,'job',p_job)));
    body:=convert_to(snapshot::text,'UTF8');
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
    IF TG_TABLE_NAME='jobs' THEN job:=NEW.id;
    ELSIF TG_TABLE_NAME='job_attempts' THEN job:=NEW.job_id;
    ELSIF TG_TABLE_NAME='job_results' THEN
        SELECT job_id INTO job FROM coordination.job_attempts WHERE (tenant_id,project_id,id)=(NEW.tenant_id,NEW.project_id,NEW.attempt_id);
    ELSE
        IF NEW.kind<>'job' THEN RETURN NULL; END IF; job:=NEW.resource_id;
    END IF;
    IF NOT EXISTS(SELECT 1 FROM core.record_aliases a JOIN coordination.outbox o
        ON(o.tenant_id,o.project_id,o.aggregate_id)=(a.tenant_id,a.project_id,a.record_id)
        WHERE (a.tenant_id,a.project_id,a.source_namespace,a.external_id)=(NEW.tenant_id,NEW.project_id,'cortex.core.job',job::text)
          AND o.xmin::text=pg_current_xact_id()::text)
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
    UPDATE coordination.consumer_checkpoints SET state='expired'
      WHERE installation_id=s.installation_id AND expires_at<=clock_timestamp() AND state<>'expired';
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
    UPDATE coordination.consumer_checkpoints SET state='expired'
      WHERE installation_id=s.installation_id AND applied_cursor<floor_value AND state<>'expired';
    RETURN jsonb_build_object('removed',removed,'floor',floor_value,'head',state.last_published_cursor,'blocked',blocked);
END;
$$;

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
GRANT EXECUTE ON FUNCTION coordination.c06_bootstrap(),coordination.c06_reserved(),coordination.capture_job(uuid),
  coordination.publish_outbox(integer),coordination.outbox_page(bigint,integer),coordination.prune_outbox()
  TO "kaidera-runtime-core-request";
