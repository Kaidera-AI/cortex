"""Actual private-port effects and two-session barriers; absent surfaces labelled."""
import time
from uuid import UUID

import psycopg

from support import Fixture,ROLE_NAMES,uid,EMBEDDING_KIND


class Ports(Fixture):
    def test_lease_rejects_live_takeover_and_cross_installation(self):
        conn,fence=self.lease()
        other=self.login('supervisor')
        self.refusal(other,'SELECT coordination.runtime_lease_acquire(%s,%s,1000)',(uid(1),uid(101)),false_ok=True)
        self.refusal(conn,'SELECT coordination.runtime_lease_acquire(%s,%s,1000)',(uid(20),uid(100)),false_ok=True)
        self.assertEqual(self.root.execute('SELECT installation_id::text,fence FROM coordination.supervisor_leases').fetchall(),[(uid(1),fence)])

    def test_release_retains_row_and_next_acquire_increases_fence(self):
        conn,fence=self.lease()
        self.assertIs(self.allowed(conn,'SELECT coordination.runtime_lease_release(%s,%s,%s)',(uid(1),uid(100),fence)),True)
        row=self.root.execute('SELECT fence,expires_at<=clock_timestamp() FROM coordination.supervisor_leases WHERE installation_id=%s',(uid(1),)).fetchone()
        self.assertEqual(row,(fence,True))
        next_fence=self.allowed(conn,'SELECT coordination.runtime_lease_acquire(%s,%s,1000)',(uid(1),uid(101)))
        self.assertGreater(next_fence,fence)

    def test_renew_and_release_reject_wrong_holder_and_stale_fence(self):
        conn,fence=self.lease()
        before=self.root.execute('SELECT holder,fence,expires_at FROM coordination.supervisor_leases').fetchall()
        for fn,args in (('runtime_lease_renew',(uid(1),uid(101),fence,1000)),
                        ('runtime_lease_renew',(uid(1),uid(100),fence+1,1000)),
                        ('runtime_lease_release',(uid(1),uid(101),fence)),
                        ('runtime_lease_release',(uid(1),uid(100),fence+1))):
            self.refusal(conn,'SELECT coordination.'+fn+'('+','.join(['%s']*len(args))+')',args,false_ok=True)
        self.assertEqual(self.root.execute('SELECT holder,fence,expires_at FROM coordination.supervisor_leases').fetchall(),before)

    def test_expiry_is_rechecked_after_real_lease_lock_wait(self):
        conn,fence=self.lease(ttl=1000)
        locker=self.login('bootstrap');locker.execute('BEGIN')
        locker.execute('SELECT * FROM coordination.supervisor_leases WHERE installation_id=%s FOR UPDATE',(uid(1),))
        if not self.root.execute('SELECT expires_at>clock_timestamp() FROM coordination.supervisor_leases').fetchone()[0]:
            raise RuntimeError('fixture lease expired before dispatch; INCONCLUSIVE')
        _,pid,box,thread=self.background('supervisor','SELECT coordination.runtime_lease_renew(%s,%s,%s,1000)',(uid(1),uid(100),fence))
        try:
            self.wait_blocked(pid,locker.info.backend_pid)
            self.root.execute('SELECT pg_sleep(1.1)')
        finally:locker.execute('COMMIT')
        self.joined(thread,box)
        if 'error' in box:
            error=box['error']
            if not isinstance(error,psycopg.Error) or error.sqlstate not in ('42501','55000'):raise error
        else:self.assertIs(box.get('value'),False,'SEMANTIC:expired queued renew resurrected lease')
        self.assertEqual(self.root.execute('SELECT fence,expires_at<=clock_timestamp() FROM coordination.supervisor_leases').fetchone(),(fence,True))

    def test_writable_port_locks_map_before_lease(self):
        _,fence=self.lease()
        mapping=self.login('bootstrap');lease=self.login('bootstrap')
        mapping.execute('BEGIN');lease.execute('BEGIN')
        self.assertIsNotNone(mapping.execute('SELECT role_oid FROM auth.runtime_login_bindings WHERE role_name=%s FOR UPDATE',(ROLE_NAMES['supervisor'],)).fetchone())
        lease.execute('SELECT * FROM coordination.supervisor_leases WHERE installation_id=%s FOR UPDATE',(uid(1),))
        _,pid,box,thread=self.background('supervisor','SELECT coordination.runtime_lease_renew(%s,%s,%s,1000)',(uid(1),uid(100),fence))
        try:
            self.wait_blocked(pid,mapping.info.backend_pid)
            self.assertNotIn(lease.info.backend_pid,self.root.execute('SELECT pg_blocking_pids(%s)',(pid,)).fetchone()[0])
            mapping.execute('COMMIT')
            self.wait_blocked(pid,lease.info.backend_pid)
        finally:
            mapping.execute('ROLLBACK');lease.execute('COMMIT')
        self.joined(thread,box)
        if 'error' in box:raise box['error']
        self.assertIs(box.get('value'),True)

    def test_final_assert_live_expires_and_aborts_bounded_PG_action(self):
        conn,fence=self.lease()
        before=self.root.execute('SELECT holder,fence,expires_at FROM coordination.supervisor_leases').fetchall()
        class Abort(Exception):pass
        try:
            with conn.transaction():
                self.assertIs(self.allowed(conn,'SELECT coordination.runtime_lease_assert_live(%s,%s,%s)',(uid(1),uid(100),fence)),True)
                self.assertIs(self.allowed(conn,'SELECT coordination.runtime_lease_renew(%s,%s,%s,80)',(uid(1),uid(100),fence)),True)
                conn.execute('SELECT pg_sleep(.12)')
                self.refusal(conn,'SELECT coordination.runtime_lease_assert_live(%s,%s,%s)',(uid(1),uid(100),fence),false_ok=True)
                raise Abort()
        except Abort:pass
        self.assertEqual(self.root.execute('SELECT holder,fence,expires_at FROM coordination.supervisor_leases').fetchall(),before)

    def test_generic_fence_trigger_is_attached_and_rejects_positive_regression(self):
        conn,first=self.lease()
        self.allowed(conn,'SELECT coordination.runtime_lease_release(%s,%s,%s)',(uid(1),uid(100),first))
        second=self.allowed(conn,'SELECT coordination.runtime_lease_acquire(%s,%s,1000)',(uid(1),uid(101)))
        self.assertGreater(second,1)  # decrement stays positive: CHECK cannot mask missing trigger
        trigger=self.root.execute('''SELECT t.tgfoid::regprocedure::text,t.tgenabled FROM pg_trigger t
            WHERE t.tgrelid='coordination.supervisor_leases'::regclass
              AND t.tgname='supervisor_fence_monotonic' AND NOT t.tgisinternal''').fetchone()
        with self.subTest(gate='actual-trigger-attachment'):
            self.assertEqual(trigger,('coordination.refuse_fence_regression()','O'))
        rejection=None
        try:
            self.root.execute('UPDATE coordination.supervisor_leases SET fence=%s WHERE installation_id=%s',(second-1,uid(1)))
        except psycopg.errors.ObjectNotInPrerequisiteState as error:rejection=error.sqlstate
        with self.subTest(gate='positive-decrease-rejected'):
            self.assertEqual(rejection,'55000')
        with self.subTest(gate='state-unchanged'):
            self.assertEqual(self.root.execute('SELECT fence FROM coordination.supervisor_leases WHERE installation_id=%s',(uid(1),)).fetchone()[0],second)

    def test_lease_health_is_closed_read_only_and_independent_of_job_sample(self):
        self.private('coordination.runtime_lease_health(uuid)')
        locker=self.login('bootstrap');locker.execute('BEGIN');locker.execute('LOCK TABLE coordination.jobs IN ACCESS EXCLUSIVE MODE')
        conn=self.login('observer');conn.execute("SET statement_timeout='250ms'")
        try:
            conn.execute('BEGIN READ ONLY ISOLATION LEVEL READ COMMITTED')
            try:value=conn.execute('SELECT coordination.runtime_lease_health(%s)',(uid(1),)).fetchone()[0]
            except psycopg.errors.QueryCanceled:self.fail('SEMANTIC:lease health coupled to locked job aggregate')
            self.assertEqual(value,{'state':'absent','fence':None})
            conn.execute('COMMIT')
        finally:locker.execute('COMMIT')

    def test_observer_pending_embedding_jobs_is_not_total_jobs_or_foreign(self):
        self.private('coordination.runtime_observe(uuid)');self.seed_jobs()
        conn=self.login('observer');conn.execute('BEGIN READ ONLY ISOLATION LEVEL READ COMMITTED')
        value=self.allowed(conn,'SELECT coordination.runtime_observe(%s)',(uid(1),))
        self.assertEqual(set(value),{'database_bytes','pending_jobs','pending_embedding_jobs','lease'})
        self.assertTrue(type(value['database_bytes']) is int and value['database_bytes']>0)
        self.assertEqual((value['pending_jobs'],value['pending_embedding_jobs']),(3,1))
        self.assertEqual(value['lease'],{'state':'absent','fence':None});conn.execute('COMMIT')
        self.refusal(conn,'SELECT coordination.runtime_observe(%s)',(uid(20),))
        for table in ('auth.runtime_login_bindings','coordination.jobs','auth.credentials','coordination.supervisor_leases'):
            self.refusal(conn,'SELECT count(*) FROM '+table)

    def test_observer_missing_kind_or_fault_is_unavailable_never_zero(self):
        self.private('coordination.runtime_observe(uuid)');self.seed_jobs()
        try:self.bind_current(embedding_kind=None)
        except (ValueError,psycopg.errors.ObjectNotInPrerequisiteState,psycopg.errors.InsufficientPrivilege):return
        conn=self.login('observer')
        self.refusal(conn,'SELECT coordination.runtime_observe(%s)',(uid(1),))

    def test_observer_read_only_final_revision_recheck_after_concurrent_revocation(self):
        self.private('coordination.runtime_observe(uuid)');self.seed_jobs()
        locker=self.login('bootstrap');locker.execute('BEGIN');locker.execute('LOCK TABLE coordination.jobs IN ACCESS EXCLUSIVE MODE')
        observer,pid,box,thread=self.background('observer','SELECT coordination.runtime_observe(%s)',(uid(1),),readonly=True)
        try:
            self.wait_blocked(pid,locker.info.backend_pid)
            revoke=self.login('bootstrap');revoke.execute("SET statement_timeout='250ms'")
            try:
                changed=revoke.execute('UPDATE auth.runtime_login_bindings SET active=false,revision=revision+1 WHERE role_name=%s',(ROLE_NAMES['observer'],)).rowcount
            except psycopg.errors.QueryCanceled:self.fail('SEMANTIC:read-only observer locked mapping against revocation')
            self.assertEqual(changed,1)
        finally:locker.execute('COMMIT')
        self.joined(thread,box)
        if 'error' in box:
            error=box['error']
            if not isinstance(error,psycopg.Error) or error.sqlstate not in ('42501','55000'):raise error
        else:self.fail('SEMANTIC:observer published stale revision after revocation')
        observer.execute('ROLLBACK')

    def seed_jobs(self):
        # Same installation:1 embedding pending,2 other pending,1 embedding done.
        # Foreign installation:1 embedding pending. Valid parent payloads exist.
        for n,tenant,kind,state in ((410,2,EMBEDDING_KIND,'pending'),(411,2,'pgo.other','pending'),
              (412,2,'pgo.other','pending'),(413,2,EMBEDDING_KIND,'succeeded'),
              (414,21,EMBEDDING_KIND,'pending')):
            self.root.execute('''INSERT INTO coordination.jobs
              (tenant_id,project_id,id,kind,payload_ref,idempotency_key,state)
              VALUES (%s,%s,%s,%s,%s,%s,%s)''',(uid(tenant),uid(3),uid(n),kind,uid(5),str(n),state))
