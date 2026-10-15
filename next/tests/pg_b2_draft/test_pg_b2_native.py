"""Same frozen lifecycle assertions on a synthetic P2-bound real-PG draft.

These supplemental nodes do not earn frozen181 candidate or owner/native credit.
"""
from uuid import UUID
from psycopg import sql
from native_owner import NativeFixture
from support import ROLE_NAMES,OPERATION,EMBEDDING_KIND,uid
import test_lifecycle
import test_ports
from cortex_core.install.owner import OwnerRefusal

class Finite(NativeFixture,test_lifecycle.Lifecycle):
    pass

class Ports(NativeFixture,test_ports.Ports):
    pass

class Restore(NativeFixture):
    def test_binding_has_exact_active_services_and_actual_connect(self):
        try:self.candidate_if_present()
        except OwnerRefusal as error:self.fail('SEMANTIC:owned-service-binding-refused:'+error.detail)
        rows=self.root.execute('SELECT logical_role,active FROM auth.runtime_login_bindings ORDER BY logical_role').fetchall()
        services=('api_request','installer_enrollment','observer','probe','supervisor')
        self.assertEqual(rows,[(name,True) for name in services])
        for logical in services:
            self.assertTrue(self.root.execute('SELECT has_database_privilege(%s,current_database(),%s)',(ROLE_NAMES[logical],'CONNECT')).fetchone()[0])

    def test_exact_five_originals_after_changed_live_state_and_history_reload(self):
        self.candidate_if_present()
        original=self.phase_owner.original
        self.root.execute(sql.SQL('ALTER ROLE {} LOGIN').format(sql.Identifier(ROLE_NAMES['request_group'])))
        self.root.execute('ALTER DATABASE '+sql.Identifier(self.root.execute('SELECT current_database()').fetchone()[0]).as_string(self.root)+' CONNECTION LIMIT 19')
        self.root.execute(sql.SQL('GRANT EXECUTE ON FUNCTION coordination.runtime_observe(uuid) TO {}').format(sql.Identifier(ROLE_NAMES['probe'])))
        self.root.execute(sql.SQL('ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA auth GRANT EXECUTE ON FUNCTIONS TO PUBLIC').format(sql.Identifier(ROLE_NAMES['bootstrap'])))
        for field in ('hba','carrier'):(self.phase_owner.materials.root/field).write_bytes(b'CHANGED '+field.encode())
        before=self.phase_owner.last_adapter.snapshot()
        self.assertTrue(all(before[k]!=original[k] for k in original),'all five fields genuinely changed')
        preimage=self.phase_owner.last_adapter.receipt()['preimage']
        try:self.phase_owner.restore_runtime_roles(self.root,role_names=dict(ROLE_NAMES),operation_id=OPERATION,preimage=preimage)
        except OwnerRefusal as error:self.fail('SEMANTIC:owned-original-restore-refused:'+error.detail)
        self.assertEqual(self.phase_owner.last_adapter.snapshot(),original)
        event=self.phase_owner.history.head()[1]
        self.assertEqual(event['phase'],'created')
        import json
        result=json.loads(self.phase_owner.history.read(event['result_ref']))
        measured={k:self.phase_owner.history.read(v) for k,v in result['observed']['originals'].items()}
        self.assertEqual(measured,original)

    def test_foreign_original_reference_never_restores(self):
        self.candidate_if_present();before=self.phase_owner.last_adapter.snapshot()
        with self.assertRaises(ValueError):self.phase_owner.restore_runtime_roles(self.root,role_names=dict(ROLE_NAMES),operation_id=OPERATION,preimage={'path':'facts/foreign','bytes':1,'sha256':'0'*64})
        self.assertEqual(self.phase_owner.last_adapter.snapshot(),before)

    def test_exact_restore_after_finite_self_close(self):
        self.candidate_if_present()
        original=self.phase_owner.original
        conn=self.owner_login();migrator=self.login('migrator')
        preimage=self.phase_owner.last_adapter.receipt()['preimage']
        self.phase_owner.close_finite_roles(conn,role_names=dict(ROLE_NAMES),operation_id=OPERATION,
            owned_backend_pids=(conn.info.backend_pid,migrator.info.backend_pid))
        self.assertTrue(conn.closed)
        self.phase_owner.restore_runtime_roles(self.root,role_names=dict(ROLE_NAMES),operation_id=OPERATION,preimage=preimage)
        self.assertEqual(self.phase_owner.last_adapter.snapshot(),original)

    def test_incomplete_finite_backend_set_refuses_before_pg_effects(self):
        self.candidate_if_present()
        before=self.phase_owner.last_adapter.snapshot()
        conn=self.owner_login();migrator=self.login('migrator')
        with self.assertRaises(OwnerRefusal):
            self.phase_owner.close_finite_roles(conn,role_names=dict(ROLE_NAMES),operation_id=OPERATION,
                owned_backend_pids=(conn.info.backend_pid,))
        self.assertEqual(conn.execute('SELECT 1').fetchone(),(1,))
        self.assertEqual(migrator.execute('SELECT 1').fetchone(),(1,))
        self.assertEqual(self.phase_owner.last_adapter.snapshot(),before)
