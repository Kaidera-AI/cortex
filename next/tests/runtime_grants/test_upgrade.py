"""Real PostgreSQL U13 one-suffix atomicity and database restore receipt."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

import psycopg
from psycopg.conninfo import make_conninfo

from cortex_core.migrations import apply_migrations,MigrationError
from cortex_core.upgrade import Admission,AtomicUpgradeJournal,UpgradeCoordinator
from runtime_grants_fixture import Fixture,ORIGINAL,SCHEMA,STEP,ROLE_NAMES,APP_SCHEMAS


class Upgrade(Fixture):
    def ledger(self):
        return self.root.execute('SELECT migration_id,sha256 FROM core.schema_migrations ORDER BY ctid').fetchall()

    def snapshot(self):
        tables=self.root.execute("SELECT n.nspname||'.'||c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY(%s) AND c.relkind='r' AND c.relname<>'schema_migrations' ORDER BY 1",(list(APP_SCHEMAS),)).fetchall()
        return {name:self.root.execute('SELECT row_to_json(t)::text FROM '+name+' t ORDER BY row_to_json(t)::text').fetchall() for name, in tables}

    def control(self,action):
        root=Path(os.environ['PGO_CONTROL_DIR']);ack=root/(action+'.json')
        ack.unlink(missing_ok=True)
        print('CORTEX_CONTROL='+action,flush=True)
        end=time.monotonic()+30
        while time.monotonic()<end:
            if ack.exists():
                receipt=json.loads(ack.read_text())
                if receipt.get('action')!=action or receipt.get('exit_code')!=0:raise RuntimeError('owned database control failed')
                return receipt
            time.sleep(.02)
        raise RuntimeError('owned database control acknowledgement missing')

    def ports(self):
        temp=tempfile.TemporaryDirectory(prefix='pgo-u13-',dir=os.environ['PGO_CONTROL_DIR'])
        self.addCleanup(temp.cleanup);outer=self
        class Ports:
            def __init__(self):
                self.journal=AtomicUpgradeJournal(Path(temp.name)/'journal.json')
                self.operation_lock_path=Path(temp.name)/'operation.lock'
                self.fail_after_commit=False;self.backup=None;self.calls=[]
            def load(self):return self.journal.load()
            def save(self,state,*,expected):self.journal.save(state,expected=expected)
            def fence_old(self):self.calls.append('fence_old')
            def fence_new(self):self.calls.append('fence_new')
            def verified_backup(self):
                self.backup=outer.snapshot();receipt=outer.control('DUMP')
                return receipt.get('bytes',0)>0
            def restore(self):
                outer.request.close();outer.db.close();outer.root.close()
                outer.control('RESTORE')
                outer.root=psycopg.connect(outer.dsn,autocommit=True);outer.addCleanup(outer.root.close)
                outer.db=outer.root
                outer.request=outer.login('api_request',request_role=True)
            def applied_steps(self):return (STEP,) if any(x[0]==STEP for x in outer.ledger()) else ()
            def apply(self,*,through):
                with outer.login('migrator') as conn:apply_migrations(conn,through=through)
                if self.fail_after_commit:
                    self.fail_after_commit=False
                    raise InterruptedError('real DB commit before journal')
            def validate_preservation(self):
                after=outer.snapshot()
                return all(after.get(k)==v for k,v in self.backup.items())
        return Ports()

    def coordinator(self,ports):
        # Explicit synthetic admitted pair: this is U13 mechanics, not R01
        # signature/custody or a released/production schema compatibility claim.
        return UpgradeCoordinator(Admission('v0.1.011','v0.1.012','1'*64,'2'*64,
            1,2,3,4,(STEP,),(('v0.1.011',1),('v0.1.012',2)),
            (('v0.1.011',1),('v0.1.012',2))),ports)

    def test_original_19_checksums_real_42_tables_64_functions_and_named_reentry(self):
        manifest=json.loads((SCHEMA/'manifest.json').read_bytes())
        self.assertEqual(manifest['migrations'][:19],ORIGINAL['migrations'])
        for row in ORIGINAL['migrations']:
            self.assertEqual(hashlib.sha256((SCHEMA/row['file']).read_bytes()).hexdigest(),row['sha256'])
        self.assertEqual(len(self.snapshot()),42)
        self.assertEqual(self.root.execute('SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=ANY(%s)',(list(APP_SCHEMAS),)).fetchone()[0],64)
        with self.login('migrator') as conn:self.assertEqual(apply_migrations(conn,through=ORIGINAL['migrations'][-1]['id']),[])
        self.assertEqual(self.ledger(),[(x['id'],x['sha256']) for x in ORIGINAL['migrations']])

    def test_single_final_suffix_exact_20_44_73_and_reentry(self):
        manifest=self.require_candidate();before=self.snapshot()
        with self.login('migrator') as conn:
            self.assertEqual(apply_migrations(conn,through=STEP),[STEP])
            self.assertEqual(apply_migrations(conn,through=STEP),[])
            # through is not a ceiling on an already applied known prefix.
            self.assertEqual(apply_migrations(conn,through=ORIGINAL['migrations'][-1]['id']),[])
        self.assertEqual(len(self.snapshot()),44)
        self.assertEqual(self.root.execute('SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=ANY(%s)',(list(APP_SCHEMAS),)).fetchone()[0],73)
        self.assertEqual(self.ledger(),[(x['id'],x['sha256']) for x in manifest['migrations']])
        self.assertTrue(all(self.snapshot()[k]==v for k,v in before.items()))
        self.assertFalse(self.has('coordination.supervisor_fence_no_regression()','function'))

    def test_crash_inside_combined_DDL_rolls_back_all_new_objects_and_ledger(self):
        manifest=self.require_candidate();before=self.snapshot();ledger=self.ledger()
        with tempfile.TemporaryDirectory(dir=os.environ['PGO_CONTROL_DIR']) as tmp:
            target=Path(tmp)/'schema';shutil.copytree(SCHEMA,target)
            row=manifest['migrations'][-1];path=target/row['file']
            path.write_bytes(path.read_bytes()+b'\nSELECT pg_advisory_xact_lock(6215001);\n')
            row['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
            (target/'manifest.json').write_text(json.dumps(manifest))
            conn=self.login('migrator');box={}
            def applying():
                try:box['value']=apply_migrations(conn,target,through=STEP)
                except BaseException as error:box['error']=error
            thread=threading.Thread(target=applying,daemon=True)
            self.root.execute('SELECT pg_advisory_lock(6215001)')
            try:
                thread.start()
                self.wait_blocked(conn.info.backend_pid,self.root.info.backend_pid,box)
                self.assertIs(self.root.execute('SELECT pg_terminate_backend(%s,1000)',(conn.info.backend_pid,)).fetchone()[0],True)
            finally:
                self.root.execute('SELECT pg_advisory_unlock(6215001)')
                self.joined(thread,box)
            self.assertIsInstance(box.get('error'),psycopg.OperationalError)
        self.assertEqual(self.ledger(),ledger);self.assertEqual(self.snapshot(),before)
        self.assertFalse(self.has('auth.runtime_login_bindings'))
        self.assertFalse(self.has('coordination.runtime_observe(uuid)','function'))

    def test_commit_before_journal_reconciles_once_and_accept_is_fix_forward(self):
        self.require_candidate();ports=self.ports();upgrade=self.coordinator(ports)
        upgrade.prepare();ports.fail_after_commit=True
        with self.assertRaises(InterruptedError):upgrade.advance()
        self.assertEqual(tuple(ports.load()['steps']),())
        self.assertEqual(self.coordinator(ports).advance(),STEP)
        self.assertEqual(ports.applied_steps(),(STEP,));self.assertIsNone(upgrade.advance())
        self.assertEqual(sum(x[0]==STEP for x in self.ledger()),1)
        upgrade.accept();self.assertEqual(ports.load()['phase'],'accepted')
        with self.assertRaisesRegex(ValueError,'fix_forward_only'):self.coordinator(ports).rollback()

    def test_preaccept_database_restore_is_real_and_does_not_restore_cluster_roles(self):
        self.require_candidate();before=self.snapshot();ledger=self.ledger()
        ports=self.ports();upgrade=self.coordinator(ports);upgrade.prepare();upgrade.advance()
        from psycopg import sql
        name=ROLE_NAMES['probe']
        self.root.execute(sql.SQL('ALTER ROLE {} CREATEDB').format(sql.Identifier(name)))
        upgrade.rollback()
        self.assertEqual(self.snapshot(),before);self.assertEqual(self.ledger(),ledger)
        self.assertEqual(ports.load()['phase'],'rolled_back')
        self.assertTrue(self.root.execute('SELECT rolcreatedb FROM pg_roles WHERE rolname=%s',(name,)).fetchone()[0],
                        'DB restore must not pretend to restore cluster roles')
        self.assertIn('fence_new',ports.calls)

    def test_unknown_and_holey_ledger_refuse_before_new_suffix(self):
        self.require_candidate();before=self.snapshot()
        with self.login('migrator') as conn:
            with self.assertRaises(MigrationError):apply_migrations(conn,through='runtime-ports-0002')
        with self.root.transaction():
            self.root.execute("DELETE FROM core.schema_migrations WHERE migration_id='core-0001'")
        with self.login('migrator') as conn:
            with self.assertRaisesRegex(MigrationError,'not a prefix'):apply_migrations(conn,through=STEP)
        self.assertEqual(self.snapshot(),before);self.assertFalse(self.has('auth.runtime_login_bindings'))
