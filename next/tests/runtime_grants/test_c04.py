"""Retain original C04 body oracles; amend only now-forbidden B-login reuse."""
import hashlib
from runtime_grants_fixture import Fixture,auth_fixture,READ_A,WRITE_A,uid
from cortex_core.auth import AuthError


class C04(Fixture):
    def test_pool_reuse_clears_scope_after_success_exception_and_refusal(self):
        self.candidate_if_present()
        with self.auth():self.assertEqual(self.request.execute('SELECT count(*) FROM core.records').fetchone()[0],1)
        self.assertEqual(self.request.execute('SELECT count(*) FROM core.records').fetchone()[0],0)
        with self.assertRaisesRegex(RuntimeError,'synthetic rollback'):
            with self.auth():raise RuntimeError('synthetic rollback')
        with self.assertRaises(AuthError):
            with self.auth(b'unknown'):pass
        self.assertEqual(self.request.execute('SELECT count(*) FROM core.records').fetchone()[0],0)

    def test_permission_generation_changes_cache_partition_and_revocation(self):
        self.candidate_if_present()
        with self.auth() as before:pass
        self.admin.execute("UPDATE auth.project_grants SET permissions=ARRAY['read','write'] WHERE tenant_id=%s AND principal_id=%s",(uid(2),uid(8)))
        with self.auth() as after:
            self.assertGreater(after.permission_generation,before.permission_generation)
            self.assertNotEqual(after.cache_partition,before.cache_partition)
        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s',(hashlib.sha256(READ_A).hexdigest(),))
        with self.assertRaises(AuthError):
            with self.auth():pass

    def test_canonical_embedding_check_helper_survives_public_closure(self):
        self.candidate_if_present()
        with self.auth(WRITE_A,action='write'):
            self.request.execute('''INSERT INTO retrieval.embedding_models
              (tenant_id,project_id,id,provider,model,model_version,dimensions,preprocessing_sha256)
              VALUES (%s,%s,%s,'fixture','fixture','1',3,%s)''',(*self.scope,uid(450),'1'*64))
            self.request.execute('''INSERT INTO retrieval.embeddings
              (tenant_id,project_id,record_id,source_revision,model_id,dimensions,embedding)
              VALUES (%s,%s,%s,1,%s,3,ARRAY[1,2,3]::real[])''',(*self.scope,uid(6),uid(450)))
        self.assertEqual(self.root.execute('SELECT count(*) FROM retrieval.embeddings').fetchone()[0],1)


AMENDED={'test_all_business_tables_force_rls_private_functions_stay_private',
         'test_scope_clears_on_commit_rollback_and_pool_reuse',
         'test_permission_generation_and_cache_partition_track_authority'}

def retained(original):
    def body(self):
        self.candidate_if_present()
        original(self)
    return body

for name,value in vars(auth_fixture.AuthorizationTests).items():
    if name.startswith('test_') and name not in AMENDED:
        setattr(C04,name,retained(value))
