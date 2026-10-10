BEGIN;
INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) VALUES
  ('00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000003',
   '00000000-0000-4000-8000-000000000012',convert_to('record-after','UTF8'),
   encode(sha256(convert_to('record-after','UTF8')),'hex'));
UPDATE core.records SET current_revision=2 WHERE id='00000000-0000-4000-8000-000000000010';
INSERT INTO core.record_revisions(tenant_id,project_id,record_id,revision,payload_ref,tombstone) VALUES
  ('00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000003',
   '00000000-0000-4000-8000-000000000010',2,'00000000-0000-4000-8000-000000000012',false);
INSERT INTO retrieval.embeddings(tenant_id,project_id,record_id,source_revision,model_id,dimensions,embedding) VALUES
  ('00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000003',
   '00000000-0000-4000-8000-000000000010',2,'00000000-0000-4000-8000-000000000020',3,
   ARRAY[0.4,0.5,0.6]::real[]);
INSERT INTO core.blob_manifests(tenant_id,project_id,id,record_id,kind,object_key,sha256,byte_length,mime_type) VALUES
  ('00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000003',
   '00000000-0000-4000-8000-000000000031','00000000-0000-4000-8000-000000000010',
   'generated','synthetic/after',encode(sha256(convert_to('after blob','UTF8')),'hex'),
   octet_length('after blob'),'text/plain');
UPDATE coordination.consumer_project_checkpoints SET scan_cursor=1,applied_cursor=1
WHERE module_id='graph' AND project_id='00000000-0000-4000-8000-000000000003';
COMMIT;
