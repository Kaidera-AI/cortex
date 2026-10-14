"""Finite owner client phase contract: genuine sessions, catalogue effects."""
from uuid import UUID

import psycopg
from psycopg import sql

from support import Fixture,ROLE_NAMES,OPERATION,EMBEDDING_KIND,uid


class Lifecycle(Fixture):
    def prepare(self):
        client=self.client()
        with self.login('bootstrap') as owner:
            receipt=client.prepare_runtime_roles(owner,role_names=dict(ROLE_NAMES),operation_id=OPERATION)
        self.assertIsInstance(receipt,dict)
        self.assertIn('preimage',receipt)
        return client,receipt

    def test_prepare_reentry_preserves_actual_role_oid_and_preimage(self):
        self.client()  # absence is BODY surface RED before any fixture change
        self.root.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(ROLE_NAMES['probe'])))
        client,first=self.prepare()
        self.assertEqual(self.root.execute('SELECT rolcanlogin,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s',(ROLE_NAMES['probe'],)).fetchone(),(False,False,False))
        before=self.root.execute('SELECT oid,rolname FROM pg_roles WHERE rolname=ANY(%s) ORDER BY rolname',(list(ROLE_NAMES.values()),)).fetchall()
        with self.login('bootstrap') as owner:
            second=client.prepare_runtime_roles(owner,role_names=dict(ROLE_NAMES),operation_id=OPERATION)
        self.assertEqual(second,first)
        self.assertEqual(self.root.execute('SELECT oid,rolname FROM pg_roles WHERE rolname=ANY(%s) ORDER BY rolname',(list(ROLE_NAMES.values()),)).fetchall(),before)

    def test_prepare_rejects_foreign_privileged_service_instead_of_adopting_name(self):
        client=self.client()
        self.root.execute(sql.SQL('ALTER ROLE {} BYPASSRLS').format(sql.Identifier(ROLE_NAMES['probe'])))
        with self.login('bootstrap') as owner:
            with self.assertRaises((ValueError,psycopg.errors.InsufficientPrivilege,psycopg.errors.ObjectNotInPrerequisiteState)):
                client.prepare_runtime_roles(owner,role_names=dict(ROLE_NAMES),operation_id=OPERATION)

    def test_bind_same_operation_preserves_oid_revision_and_other_operation_refuses(self):
        self.candidate_if_present();self.require('auth.runtime_login_bindings')
        self.bind_current()
        before=self.root.execute('SELECT role_oid,role_name,installation_id,active,revision,operation_id FROM auth.runtime_login_bindings ORDER BY role_name').fetchall()
        self.bind_current()
        self.assertEqual(self.root.execute('SELECT role_oid,role_name,installation_id,active,revision,operation_id FROM auth.runtime_login_bindings ORDER BY role_name').fetchall(),before)
        with self.login('bootstrap') as owner:
            with self.assertRaises((ValueError,psycopg.errors.InsufficientPrivilege,psycopg.errors.ObjectNotInPrerequisiteState)):
                self.client().bind_runtime_roles(owner,installation_id=UUID(uid(20)),
                    role_names=dict(ROLE_NAMES),operation_id=OPERATION,
                    embedding_job_kind=EMBEDDING_KIND,activate=True)
        self.assertEqual(self.root.execute('SELECT role_oid,role_name,installation_id,active,revision,operation_id FROM auth.runtime_login_bindings ORDER BY role_name').fetchall(),before)
        with self.assertRaises((ValueError,psycopg.errors.InsufficientPrivilege,psycopg.errors.ObjectNotInPrerequisiteState)):
            self.bind_current(operation=UUID(uid(901)))
        self.assertEqual(self.root.execute('SELECT role_oid,role_name,installation_id,active,revision,operation_id FROM auth.runtime_login_bindings ORDER BY role_name').fetchall(),before)

    def test_close_disables_both_finite_roles_and_drains_real_sessions(self):
        self.candidate_if_present();client,receipt=self.prepare()
        owner=self.login('bootstrap');migrator=self.login('migrator')
        pids=(owner.info.backend_pid,migrator.info.backend_pid)
        # The actual bootstrap session must close itself safely at the end;
        # fixture-controller postgres is only the independent catalogue witness.
        try:
            client.close_finite_roles(owner,role_names=dict(ROLE_NAMES),operation_id=OPERATION,
                                      owned_backend_pids=pids)
        except (psycopg.errors.AdminShutdown,psycopg.OperationalError):
            if self.root.execute('SELECT count(*) FROM pg_stat_activity WHERE pid=ANY(%s)',(list(pids),)).fetchone()[0]:raise
        rows=self.root.execute('SELECT rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=ANY(%s) ORDER BY rolname',([ROLE_NAMES['bootstrap'],ROLE_NAMES['migrator']],)).fetchall()
        self.assertEqual(rows,[(name,False,False,False,False,False) for name in sorted((ROLE_NAMES['bootstrap'],ROLE_NAMES['migrator']))])
        self.assertEqual(self.root.execute('SELECT count(*) FROM pg_stat_activity WHERE pid=ANY(%s)',(list(pids),)).fetchone()[0],0)
        for logical in ('bootstrap','migrator'):
            with self.assertRaises(psycopg.OperationalError):self.login(logical)

    def test_restore_reconciles_cluster_role_preimage_separately_from_database(self):
        client,receipt=self.prepare()
        before=self.root.execute('SELECT oid,rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=ANY(%s) ORDER BY rolname',(list(ROLE_NAMES.values()),)).fetchall()
        self.root.execute(sql.SQL('ALTER ROLE {} LOGIN').format(sql.Identifier(ROLE_NAMES['request_group'])))
        # Recovery controller is an explicitly owned synthetic operator connection;
        # this does not attest production custody, HBA or credential carriers.
        client.restore_runtime_roles(self.root,role_names=dict(ROLE_NAMES),operation_id=OPERATION,
                                     preimage=receipt['preimage'])
        self.assertEqual(self.root.execute('SELECT oid,rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=ANY(%s) ORDER BY rolname',(list(ROLE_NAMES.values()),)).fetchall(),before)
