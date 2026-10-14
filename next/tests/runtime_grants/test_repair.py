"""Hale F01–F10 delta. Frozen for Cox's next admitted PG-A lab session.

No fixture or missing-kind admission can stand in for the deciding operation.
These bodies are NOT executed against PostgreSQL by the offline repair row.
"""
from uuid import UUID
import psycopg
from psycopg import sql

import test_ports as ports
import test_upgrade as upgrade
import test_lifecycle as lifecycle
from cortex_core.migrations import apply_migrations, MigrationError
from support import Fixture, ROLE_NAMES, MATRIX, OPERATION, STEP, uid


class Repair(Fixture):
    seed_jobs = ports.Ports.seed_jobs

    def _closed_box(self, box):
        if 'error' in box:
            error=box['error']
            if not isinstance(error,psycopg.Error) or error.sqlstate not in ('42501','55000'):
                raise error
        else:
            self.fail('SEMANTIC:stale/faulted private read returned successful payload')

    def _revision_barrier(self, function):
        self.private('coordination.'+function+'(uuid)')
        self.seed_jobs()
        table='coordination.jobs' if function=='runtime_observe' else 'coordination.supervisor_leases'
        locker=self.login('bootstrap');locker.execute('BEGIN')
        locker.execute('LOCK TABLE '+table+' IN ACCESS EXCLUSIVE MODE')
        observer,pid,box,thread=self.background('observer','SELECT coordination.'+function+'(%s)',(uid(1),),readonly=True)
        try:
            self.wait_blocked(pid,locker.info.backend_pid,box)
            revoke=self.login('bootstrap');revoke.execute("SET statement_timeout='250ms'")
            # Keep OID/name/install/active unchanged. An active-only final check
            # cannot detect this stale admitted revision (the ABA distinction).
            before=revoke.execute('SELECT role_oid,role_name,installation_id,active,revision FROM auth.runtime_login_bindings WHERE role_name=%s',
                                  (ROLE_NAMES['observer'],)).fetchone()
            self.assertIs(before[3],True)
            try:
                changed=revoke.execute('UPDATE auth.runtime_login_bindings SET revision=revision+1 WHERE role_name=%s',
                                       (ROLE_NAMES['observer'],)).rowcount
            except psycopg.errors.QueryCanceled:
                self.fail('SEMANTIC:read-only port locked map against revision change')
            self.assertEqual(changed,1)
            after=revoke.execute('SELECT role_oid,role_name,installation_id,active,revision FROM auth.runtime_login_bindings WHERE role_name=%s',
                                 (ROLE_NAMES['observer'],)).fetchone()
            self.assertEqual(after[:4],before[:4]);self.assertEqual(after[4],before[4]+1)
        finally:
            locker.execute('COMMIT')
        self.joined(thread,box);self._closed_box(box);observer.execute('ROLLBACK')

    def test_observer_final_revision_only_barrier(self):
        self._revision_barrier('runtime_observe')

    def test_health_final_revision_only_barrier(self):
        self._revision_barrier('runtime_lease_health')

    def test_health_revoked_map_refuses_at_entry(self):
        self.private('coordination.runtime_lease_health(uuid)')
        self.root.execute('UPDATE auth.runtime_login_bindings SET active=false,revision=revision+1 WHERE role_name=%s',
                          (ROLE_NAMES['observer'],))
        conn=self.login('observer')
        self.refusal(conn,'SELECT coordination.runtime_lease_health(%s)',(uid(1),))

    def _snapshot_refusal(self,function,isolation):
        self.private('coordination.'+function+'(uuid)')
        conn=self.login('observer');conn.execute('BEGIN READ ONLY ISOLATION LEVEL '+isolation)
        try:
            self.refusal(conn,'SELECT coordination.'+function+'(%s)',(uid(1),))
        finally:conn.execute('ROLLBACK')

    def _map_order(self,function):
        _,fence=self.lease()
        if function=='acquire':
            self.root.execute("UPDATE coordination.supervisor_leases SET expires_at=clock_timestamp()-interval '1 second'")
        args=(uid(1),uid(100),1000) if function=='acquire' else (
            (uid(1),uid(100),fence,1000) if function=='renew' else (uid(1),uid(100),fence))
        mapping=self.login('bootstrap');lease=self.login('bootstrap')
        mapping.execute('BEGIN');lease.execute('BEGIN')
        mapping.execute('SELECT role_oid FROM auth.runtime_login_bindings WHERE role_name=%s FOR UPDATE',
                        (ROLE_NAMES['supervisor'],))
        lease.execute('SELECT * FROM coordination.supervisor_leases WHERE installation_id=%s FOR UPDATE',(uid(1),))
        _,pid,box,thread=self.background('supervisor','SELECT coordination.runtime_lease_'+function+'('+','.join(['%s']*len(args))+')',args)
        try:
            self.wait_blocked(pid,mapping.info.backend_pid,box)
            self.assertNotIn(lease.info.backend_pid,self.root.execute('SELECT pg_blocking_pids(%s)',(pid,)).fetchone()[0])
            mapping.execute('COMMIT');self.wait_blocked(pid,lease.info.backend_pid,box)
        finally:
            mapping.execute('ROLLBACK');lease.execute('COMMIT')
        self.joined(thread,box)
        if 'error' in box:raise box['error']
        if function=='acquire':self.assertTrue(type(box.get('value')) is int and box['value']>fence)
        else:self.assertIs(box.get('value'),True)

    def _queued_expiry(self,function):
        _,fence=self.lease(ttl=1000)
        locker=self.login('bootstrap');locker.execute('BEGIN')
        locker.execute('SELECT * FROM coordination.supervisor_leases WHERE installation_id=%s FOR UPDATE',(uid(1),))
        if not self.root.execute('SELECT expires_at>clock_timestamp() FROM coordination.supervisor_leases').fetchone()[0]:
            raise RuntimeError('fixture lease expired before dispatch; INCONCLUSIVE')
        args=(uid(1),uid(100),fence)
        _,pid,box,thread=self.background('supervisor','SELECT coordination.runtime_lease_'+function+'(%s,%s,%s)',args)
        try:
            self.wait_blocked(pid,locker.info.backend_pid,box)
            self.root.execute('SELECT pg_sleep(1.1)')
        finally:locker.execute('COMMIT')
        self.joined(thread,box)
        if 'error' in box:
            error=box['error']
            if not isinstance(error,psycopg.Error) or error.sqlstate not in ('42501','55000'):raise error
        else:self.assertIs(box.get('value'),False,'SEMANTIC:queued expired '+function+' admitted')
        self.assertEqual(self.root.execute('SELECT fence,expires_at<=clock_timestamp() FROM coordination.supervisor_leases').fetchone(),(fence,True))

    def test_expired_matching_holder_release_has_no_effect(self):
        conn,fence=self.lease()
        self.root.execute("UPDATE coordination.supervisor_leases SET expires_at=clock_timestamp()-interval '1 second'")
        before=self.root.execute('SELECT holder,fence,expires_at FROM coordination.supervisor_leases').fetchall()
        self.refusal(conn,'SELECT coordination.runtime_lease_release(%s,%s,%s)',(uid(1),uid(100),fence),false_ok=True)
        self.assertEqual(self.root.execute('SELECT holder,fence,expires_at FROM coordination.supervisor_leases').fetchall(),before)

    def test_assert_live_itself_holds_row_before_any_later_write(self):
        conn,fence=self.lease();conn.execute('BEGIN')
        try:
            self.assertIs(self.allowed(conn,'SELECT coordination.runtime_lease_assert_live(%s,%s,%s)',(uid(1),uid(100),fence)),True)
            # No renew or write occurs between the assertion and competitor.
            _,pid,box,thread=self.background('bootstrap',
                'SELECT fence FROM coordination.supervisor_leases WHERE installation_id=%s FOR UPDATE',(uid(1),))
            self.wait_blocked(pid,conn.info.backend_pid,box)
        finally:conn.execute('ROLLBACK')
        self.joined(thread,box)
        if 'error' in box:raise box['error']
        self.assertEqual(box.get('value'),fence)

    def test_one_ms_acquire_is_valid_without_sleep_dependent_health(self):
        self.private('coordination.runtime_lease_acquire(uuid,uuid,integer)')
        conn=self.login('supervisor')
        value=self.allowed(conn,'SELECT coordination.runtime_lease_acquire(%s,%s,1)',(uid(1),uid(100)))
        self.assertTrue(type(value) is int and value>0)
        self.assertEqual(self.root.execute('SELECT fence FROM coordination.supervisor_leases').fetchone(),(value,))

    def test_supervisor_own_session_admits_positive_lease(self):
        conn,fence=self.lease()
        self.assertTrue(type(fence) is int and fence>0)
        self.assertIs(self.allowed(conn,'SELECT coordination.runtime_lease_assert_live(%s,%s,%s)',(uid(1),uid(100),fence)),True)

    def _observer_fault(self,mode):
        self.private('coordination.runtime_observe(uuid)');self.seed_jobs()
        self.bind_current()  # valid kind, never bind(None) short circuit
        conn=self.login('observer')
        locker=None
        try:
            if mode=='aggregate':
                # Owned test-only schema fault, restored in finally. Existing
                # prepared plans must be invalidated by the column rename.
                self.root.execute('ALTER TABLE coordination.jobs RENAME COLUMN kind TO pgo_hidden_kind')
            else:
                locker=self.login('bootstrap');locker.execute('BEGIN')
                locker.execute('LOCK TABLE coordination.jobs IN ACCESS EXCLUSIVE MODE')
                conn.execute("SET statement_timeout='50ms'")
            refused=False
            try:
                conn.execute('SELECT coordination.runtime_observe(%s)',(uid(1),)).fetchone()
            except psycopg.Error as error:
                # Timeout itself is a typed PG unavailable boundary. A handled
                # runtime fault may use the existing closed 42501/55000 states.
                allowed=('42501','55000','57014') if mode=='timeout' else ('42501','55000')
                if error.sqlstate not in allowed:raise
                refused=True
            self.assertTrue(refused,'SEMANTIC:valid-bound observer fault/timeout returned success (including zero)')
        finally:
            if mode=='aggregate':self.root.execute('ALTER TABLE coordination.jobs RENAME COLUMN pgo_hidden_kind TO kind')
            elif locker is not None:locker.execute('ROLLBACK')

    def test_valid_bound_observer_aggregate_error_never_success_zero(self):
        self._observer_fault('aggregate')

    def test_valid_bound_observer_timeout_never_success_zero(self):
        self._observer_fault('timeout')

    def _ledger_fault(self,mode):
        self.require_candidate()
        before=upgrade.Upgrade.snapshot(self)
        if mode=='unknown':
            self.root.execute("INSERT INTO core.schema_migrations(migration_id,sha256) VALUES ('pgo-unknown-applied','0000000000000000000000000000000000000000000000000000000000000000')")
        else:self.root.execute("UPDATE core.schema_migrations SET sha256=%s WHERE migration_id='core-0001'",('0'*64,))
        altered=upgrade.Upgrade.ledger(self)
        with self.login('migrator') as conn:
            with self.assertRaises(MigrationError):apply_migrations(conn,through=STEP)
        self.assertEqual(upgrade.Upgrade.ledger(self),altered)
        self.assertEqual(upgrade.Upgrade.snapshot(self),before)
        self.assertFalse(self.has('auth.runtime_login_bindings'))

    def test_unknown_applied_ledger_row_refuses_unchanged(self):
        self._ledger_fault('unknown')

    def test_changed_applied_checksum_refuses_unchanged(self):
        self._ledger_fault('checksum')

    def test_prepare_all_nine_phase_flags_come_from_client_effects(self):
        lifecycle.Lifecycle.prepare(self)
        for logical,policy in MATRIX['roles'].items():
            row=self.root.execute('SELECT rolcanlogin,rolsuper,rolbypassrls,rolinherit,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname=%s',
                                  (ROLE_NAMES[logical],)).fetchone()
            values=[policy[k] for k in ('login','superuser','bypassrls','inherit','createdb','createrole','replication')]
            if logical not in ('bootstrap','migrator'):values[0]=False
            self.assertEqual(row,tuple(values),'SEMANTIC:client prepare phase flags:'+logical)

    def test_close_unknown_finite_session_holds_and_preserves_sentinel(self):
        client,_=lifecycle.Lifecycle.prepare(self)
        owner=self.login('bootstrap');migrator=self.login('migrator');sentinel=self.login('bootstrap')
        pids=(owner.info.backend_pid,migrator.info.backend_pid)
        with self.assertRaises((ValueError,psycopg.errors.InsufficientPrivilege,psycopg.errors.ObjectNotInPrerequisiteState)):
            client.close_finite_roles(owner,role_names=dict(ROLE_NAMES),operation_id=OPERATION,owned_backend_pids=pids)
        self.assertEqual(self.root.execute('SELECT count(*) FROM pg_stat_activity WHERE pid=%s',(sentinel.info.backend_pid,)).fetchone(),(1,))
        self.assertEqual(sentinel.execute('SELECT 1').fetchone(),(1,))

    def _unknown_close(self,mode):
        client,_=lifecycle.Lifecycle.prepare(self)
        owner=self.login('bootstrap');pids=(owner.info.backend_pid,)
        roles=dict(ROLE_NAMES)
        if mode=='unknown':roles['migrator']=roles['migrator']+'-unknown'
        operation=UUID(uid(901)) if mode=='operation' else OPERATION
        with self.assertRaises((ValueError,psycopg.errors.InsufficientPrivilege,psycopg.errors.ObjectNotInPrerequisiteState)):
            client.close_finite_roles(owner,role_names=roles,operation_id=operation,owned_backend_pids=pids)
        self.assertEqual(owner.execute('SELECT 1').fetchone(),(1,))

    def test_close_wrong_operation_holds(self):self._unknown_close('operation')
    def test_close_unknown_role_state_holds(self):self._unknown_close('unknown')

    def test_restrictive_request_policy_remains_restrictive(self):
        self.candidate_if_present()
        for command in ('a','w'):
            rows=self.root.execute("""SELECT polpermissive FROM pg_policy
                WHERE polrelid='core.records'::regclass AND polcmd IN (%s,'*')
                AND (0=ANY(polroles) OR (SELECT oid FROM pg_roles WHERE rolname=%s)=ANY(polroles))""",
                (command,ROLE_NAMES['request_group'])).fetchall()
            self.assertTrue(rows,'SEMANTIC:no deciding request write policy:'+command)
            self.assertTrue(any(value is False for value, in rows),'SEMANTIC:all request write policies became permissive:'+command)


def snapshot_case(function,isolation):
    def body(self):self._snapshot_refusal(function,isolation)
    return body


def order_case(function):
    def body(self):self._map_order(function)
    return body


def expiry_case(function):
    def body(self):self._queued_expiry(function)
    return body


def unsafe_case(logical,flag):
    def body(self):
        client=self.client()
        name=ROLE_NAMES[logical]
        # SUPERUSER is tested with NOBYPASSRLS, so the old single BYPASSRLS
        # negative cannot accidentally decide this independent unsafe preimage.
        self.root.execute(sql.SQL('ALTER ROLE {} '+flag+(' NOBYPASSRLS' if flag=='SUPERUSER' else '')).format(sql.Identifier(name)))
        before=self.root.execute('SELECT oid,rolcanlogin,rolsuper,rolbypassrls,rolinherit,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname=%s',(name,)).fetchone()
        with self.login('bootstrap') as owner:
            with self.assertRaises((ValueError,psycopg.errors.InsufficientPrivilege,psycopg.errors.ObjectNotInPrerequisiteState)):
                client.prepare_runtime_roles(owner,role_names=dict(ROLE_NAMES),operation_id=OPERATION)
        self.assertEqual(self.root.execute('SELECT oid,rolcanlogin,rolsuper,rolbypassrls,rolinherit,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname=%s',(name,)).fetchone(),before)
    return body


for function in ('runtime_lease_health','runtime_observe'):
    for isolation in ('REPEATABLE READ','SERIALIZABLE'):
        setattr(Repair,'test_snapshot__'+function+'__'+isolation.lower().replace(' ','_'),snapshot_case(function,isolation))
for function in ('acquire','renew','release','assert_live'):
    setattr(Repair,'test_map_before_lease__'+function,order_case(function))
for function in ('release','assert_live'):
    setattr(Repair,'test_queued_expiry__'+function,expiry_case(function))
for logical in ('request_group','verifier_owner','api_request','probe','supervisor','observer','installer_enrollment'):
    for flag in ('SUPERUSER','CREATEDB','CREATEROLE','REPLICATION','BYPASSRLS','INHERIT'):
        setattr(Repair,'test_unsafe_prepare__'+logical+'__'+flag.lower(),unsafe_case(logical,flag))
for logical in ('request_group','verifier_owner'):
    setattr(Repair,'test_unsafe_prepare__'+logical+'__login',unsafe_case(logical,'LOGIN'))
