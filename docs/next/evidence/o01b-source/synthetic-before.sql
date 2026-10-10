BEGIN;
INSERT INTO core.installations(id) VALUES ('00000000-0000-4000-8000-000000000001');
INSERT INTO core.tenants(id,installation_id) VALUES
  ('00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000001');
INSERT INTO core.projects(tenant_id,id,name) VALUES
  ('00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000003','synthetic');
INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) VALUES
  ('00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000003',
   '00000000-0000-4000-8000-000000000011',convert_to('record-before','UTF8'),
   encode(sha256(convert_to('record-before','UTF8')),'hex'));
INSERT INTO core.records(tenant_id,project_id,id,kind,current_revision,tombstone) VALUES
  ('00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000003',
   '00000000-0000-4000-8000-000000000010','memory',1,false);
INSERT INTO core.record_revisions(tenant_id,project_id,record_id,revision,payload_ref,tombstone) VALUES
  ('00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000003',
   '00000000-0000-4000-8000-000000000010',1,'00000000-0000-4000-8000-000000000011',false);
INSERT INTO retrieval.embedding_models(tenant_id,project_id,id,provider,model,model_version,dimensions,preprocessing_sha256) VALUES
  ('00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000003',
   '00000000-0000-4000-8000-000000000020','fixture','synthetic-1','1',3,
   encode(sha256(convert_to('synthetic-preprocessing','UTF8')),'hex'));
INSERT INTO retrieval.embeddings(tenant_id,project_id,record_id,source_revision,model_id,dimensions,embedding) VALUES
  ('00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000003',
   '00000000-0000-4000-8000-000000000010',1,'00000000-0000-4000-8000-000000000020',3,
   ARRAY[0.1,0.2,0.3]::real[]);
INSERT INTO core.blob_manifests(tenant_id,project_id,id,record_id,kind,object_key,sha256,byte_length,mime_type) VALUES
  ('00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000003',
   '00000000-0000-4000-8000-000000000030','00000000-0000-4000-8000-000000000010',
   'original','synthetic/before',encode(sha256(convert_to('before blob','UTF8')),'hex'),
   octet_length('before blob'),'text/plain');
INSERT INTO coordination.consumer_project_checkpoints(
  installation_id,module_id,tenant_id,project_id,generation,scan_cursor,applied_cursor,state,snapshot_cursor)
VALUES ('00000000-0000-4000-8000-000000000001','graph',
        '00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000003',
        1,0,0,'active',0);
COMMIT;
