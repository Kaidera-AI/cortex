"""Disposable PG rehearsal of expand-only interruption and preservation."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'schema'))
import test_schema
from cortex_core.migrations import apply_migrations
from cortex_core.upgrade import AtomicUpgradeJournal, UpgradeCoordinator


class Rehearsal(unittest.TestCase):
    setUp = test_schema.SchemaTests.setUp
    reset = test_schema.SchemaTests.reset
    event = test_schema.SchemaTests.event

    def seed(self):
        uid = test_schema.uid
        self.event()
        self.db.execute("INSERT INTO core.blob_manifests (tenant_id,project_id,id,record_id,kind,object_key,sha256,byte_length,mime_type) VALUES (%s,%s,%s,%s,'original','objects/one',%s,%s,'application/json')",
                        (*self.scope, uid(20), uid(6), self.digest, len(self.body)))
        self.db.execute("INSERT INTO auth.principals (tenant_id,id,name) VALUES (%s,%s,'reader')", (uid(2),uid(21)))
        self.db.execute("INSERT INTO auth.project_grants (tenant_id,project_id,principal_id,permissions) VALUES (%s,%s,%s,ARRAY['read'])", (*self.scope,uid(21)))
        self.db.execute("UPDATE auth.permission_generations SET generation=7 WHERE tenant_id=%s AND project_id=%s", self.scope)
        self.db.execute("INSERT INTO coordination.feed_state (installation_id,last_published_cursor) VALUES (%s,9)", (uid(1),))
        self.db.execute("INSERT INTO coordination.published_events (installation_id,cursor,tenant_id,project_id,event_id) VALUES (%s,9,%s,%s,%s)", (uid(1),*self.scope,uid(7)))
        self.db.execute("INSERT INTO coordination.consumer_checkpoints (installation_id,module_id,applied_cursor) VALUES (%s,'search',9)", (uid(1),))
        self.db.execute("INSERT INTO retrieval.embedding_models (tenant_id,project_id,id,provider,model,model_version,dimensions,preprocessing_sha256) VALUES (%s,%s,%s,'fake','embed','1',3,%s)", (*self.scope,uid(22),self.digest))

    def snapshot(self):
        queries = {
            'payload': 'SELECT body,sha256 FROM core.payloads',
            'record': 'SELECT id,current_revision,tombstone FROM core.records',
            'revision': 'SELECT record_id,revision,payload_ref FROM core.record_revisions',
            'blob': 'SELECT id,object_key,sha256,byte_length FROM core.blob_manifests',
            'grant': 'SELECT principal_id,permissions FROM auth.project_grants',
            'generation': 'SELECT generation FROM auth.permission_generations',
            'outbox': 'SELECT event_id,payload_sha256,aggregate_revision FROM coordination.outbox',
            'published': 'SELECT cursor,event_id FROM coordination.published_events',
            'feed': 'SELECT last_published_cursor,retained_floor FROM coordination.feed_state',
            'consumer': 'SELECT module_id,applied_cursor,generation FROM coordination.consumer_checkpoints',
            'model': 'SELECT provider,model,model_version,dimensions,preprocessing_sha256 FROM retrieval.embedding_models',
        }
        return {name: self.db.execute(sql).fetchall() for name, sql in queries.items()}

    def test_two_step_crash_reentry_preserves_canonical_state(self):
        self.seed()
        before = self.snapshot()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)/'schema'
            shutil.copytree(test_schema.NEXT/'schema', directory)
            manifest_path = directory/'manifest.json'
            manifest = json.loads(manifest_path.read_text())
            steps = ('core-upgrade-probe-0001', 'core-upgrade-probe-0002')
            sqls = ('CREATE TABLE core.upgrade_rehearsal(id integer PRIMARY KEY);\n',
                    'ALTER TABLE core.upgrade_rehearsal ADD COLUMN note text;\n')
            for step, sql in zip(steps, sqls):
                filename = step+'.sql'
                data = sql.encode()
                (directory/filename).write_bytes(data)
                manifest['migrations'].append({'id':step, 'file':filename,
                                               'sha256':hashlib.sha256(data).hexdigest()})
            manifest_path.write_text(json.dumps(manifest))

            class Ports:
                applied_once = []
                fail_after = None
                restored = False
                def __init__(port):
                    port.journal = AtomicUpgradeJournal(Path(temporary)/'upgrade.json')
                    port.operation_lock_path = Path(temporary)/'upgrade.op.lock'
                def verified_backup(port): return True
                def fence_old(port): pass
                def fence_new(port): pass
                def restore(port): port.restored = True
                def validate_preservation(port): return self.snapshot() == before
                def load(port): return port.journal.load()
                def save(port, state, *, expected):
                    port.journal.save(state, expected=expected)
                def applied_steps(port):
                    rows = self.db.execute("SELECT migration_id FROM core.schema_migrations WHERE migration_id IN (%s,%s)", steps).fetchall()
                    return tuple(step for step in steps if (step,) in rows)
                def apply(port, through):
                    port.applied_once.extend(apply_migrations(self.db, directory, through=through))
                    if port.fail_after == through:
                        port.fail_after = None
                        raise InterruptedError(through)

            ports = Ports()
            admission = type('Admission', (), {
                'old_manifest_sha256':'1'*64, 'new_manifest_sha256':'2'*64,
                'old_release':'v0.1.002', 'new_release':'v0.1.020',
                'old_schema':1, 'new_schema':2, 'expand_steps':steps,
                'writer_allowed':lambda _self, release, schema: release == 'v0.1.020' and schema == 2,
            })()
            upgrade = UpgradeCoordinator(admission, ports)
            upgrade.prepare()
            for step in steps:
                ports.fail_after = step
                with self.assertRaises(InterruptedError):
                    upgrade.advance()
                self.assertEqual(upgrade.advance(), step)
                self.assertEqual(self.snapshot(), before)
            self.assertEqual(ports.applied_once, list(steps))
            self.assertEqual(ports.applied_steps(), steps)
            upgrade.accept()
            self.assertEqual(ports.load()['phase'], 'accepted')
            with self.assertRaisesRegex(ValueError, 'fix_forward_only'):
                UpgradeCoordinator(admission, ports).rollback()
            self.assertFalse(ports.restored)
            self.assertEqual(self.snapshot(), before)

    def test_real_pg_rollback_after_every_expand_prefix(self):
        self.seed()
        before = self.snapshot()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)/'schema'
            shutil.copytree(test_schema.NEXT/'schema', directory)
            manifest_path = directory/'manifest.json'
            manifest = json.loads(manifest_path.read_text())
            steps = ('core-upgrade-rollback-0001', 'core-upgrade-rollback-0002')
            sqls = ('CREATE TABLE core.upgrade_rollback_probe(id integer PRIMARY KEY);\n',
                    'ALTER TABLE core.upgrade_rollback_probe ADD COLUMN note text;\n')
            for step, sql in zip(steps, sqls):
                filename = step+'.sql'
                data = sql.encode()
                (directory/filename).write_bytes(data)
                manifest['migrations'].append({'id':step, 'file':filename,
                                               'sha256':hashlib.sha256(data).hexdigest()})
            manifest_path.write_text(json.dumps(manifest))

            for prefix in range(3):
                with self.subTest(prefix=prefix):
                    class Ports:
                        fenced = False
                        def __init__(port):
                            port.journal = AtomicUpgradeJournal(
                                Path(temporary)/f'rollback-{prefix}.json')
                            port.operation_lock_path = Path(temporary)/'upgrade.op.lock'
                        def load(port): return port.journal.load()
                        def save(port, state, *, expected):
                            port.journal.save(state, expected=expected)
                        def fence_old(port): port.fenced = True
                        def verified_backup(port):
                            self.assertTrue(port.fenced)
                            self.assertEqual(self.snapshot(), before)
                            return True
                        def applied_steps(port):
                            rows = self.db.execute(
                                'SELECT migration_id FROM core.schema_migrations '
                                'WHERE migration_id IN (%s,%s)', steps).fetchall()
                            return tuple(step for step in steps if (step,) in rows)
                        def apply(port, *, through):
                            apply_migrations(self.db, directory, through=through)
                        def fence_new(port):
                            self.assertEqual(port.load()['phase'], 'rolling_back')
                        def restore(port):
                            self.assertEqual(port.load()['phase'], 'rolling_back')
                            self.db.execute('DROP TABLE IF EXISTS core.upgrade_rollback_probe')
                            for step in steps:
                                self.db.execute('DELETE FROM core.schema_migrations '
                                                'WHERE migration_id=%s', (step,))
                            self.assertEqual(self.snapshot(), before)
                    admission = type('Admission', (), {
                        'old_manifest_sha256':'1'*64, 'new_manifest_sha256':'2'*64,
                        'old_release':'v0.1.003', 'new_release':'v0.1.020',
                        'old_schema':1, 'new_schema':2, 'expand_steps':steps,
                        'writer_allowed':lambda _self, release, schema: release == 'v0.1.020' and schema == 2,
                    })()
                    ports = Ports()
                    upgrade = UpgradeCoordinator(admission, ports)
                    upgrade.prepare()
                    for step in steps[:prefix]:
                        self.assertEqual(upgrade.advance(), step)
                    upgrade.rollback()
                    self.assertEqual(ports.load()['phase'], 'rolled_back')
                    self.assertEqual(ports.load()['steps'], steps[:prefix])
                    self.assertEqual(ports.applied_steps(), ())
                    self.assertEqual(self.snapshot(), before)

    def test_paused_real_pg_apply_drains_before_rollback(self):
        self.seed()
        before = self.snapshot()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)/'schema'
            shutil.copytree(test_schema.NEXT/'schema', directory)
            manifest_path = directory/'manifest.json'
            manifest = json.loads(manifest_path.read_text())
            step = 'core-upgrade-race-0001'
            data = b'CREATE TABLE core.upgrade_race_probe(id integer PRIMARY KEY);\n'
            (directory/(step+'.sql')).write_bytes(data)
            manifest['migrations'].append({'id':step, 'file':step+'.sql',
                                           'sha256':hashlib.sha256(data).hexdigest()})
            manifest_path.write_text(json.dumps(manifest))
            entered, release = threading.Event(), threading.Event()

            class Ports:
                fenced = False
                def __init__(port):
                    port.journal = AtomicUpgradeJournal(Path(temporary)/'race.json')
                    port.operation_lock_path = Path(temporary)/'upgrade.op.lock'
                def load(port): return port.journal.load()
                def save(port, state, *, expected):
                    port.journal.save(state, expected=expected)
                def fence_old(port): port.fenced = True
                def verified_backup(port):
                    self.assertTrue(port.fenced)
                    self.assertEqual(self.snapshot(), before)
                    return True
                def applied_steps(port):
                    rows = self.db.execute(
                        'SELECT migration_id FROM core.schema_migrations WHERE migration_id=%s',
                        (step,)).fetchall()
                    return (step,) if rows else ()
                def apply(port, *, through):
                    self.assertEqual(through, step)
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError('timed out waiting to release PostgreSQL apply')
                    apply_migrations(self.db, directory, through=through)
                def fence_new(port):
                    self.assertEqual(port.load()['phase'], 'rolling_back')
                def restore(port):
                    self.db.execute('DROP TABLE IF EXISTS core.upgrade_race_probe')
                    self.db.execute('DELETE FROM core.schema_migrations '
                                    'WHERE migration_id=%s', (step,))
                    self.assertEqual(self.snapshot(), before)

            admission = type('Admission', (), {
                'old_manifest_sha256':'1'*64, 'new_manifest_sha256':'2'*64,
                'old_release':'v0.1.003', 'new_release':'v0.1.020',
                'old_schema':1, 'new_schema':2, 'expand_steps':(step,),
                'writer_allowed':lambda _self, release, schema: release == 'v0.1.020' and schema == 2,
            })()
            ports = Ports()
            upgrade = UpgradeCoordinator(admission, ports)
            upgrade.prepare()
            errors = []
            rollback_started = threading.Event()

            def advance():
                try:
                    upgrade.advance()
                except Exception as error:
                    errors.append(error)

            def rollback():
                rollback_started.set()
                try:
                    UpgradeCoordinator(admission, ports).rollback()
                except Exception as error:
                    errors.append(error)

            writer = threading.Thread(target=advance)
            restorer = threading.Thread(target=rollback)
            writer.start()
            self.assertTrue(entered.wait(10))
            restorer.start()
            self.assertTrue(rollback_started.wait(10))
            time.sleep(0.2)
            rolled_back_while_apply_paused = not restorer.is_alive()
            release.set()
            writer.join(10)
            restorer.join(10)
            self.assertFalse(writer.is_alive() or restorer.is_alive())
            self.assertFalse(errors, [str(error) for error in errors])
            self.assertFalse(rolled_back_while_apply_paused)
            self.assertEqual(ports.load()['phase'], 'rolled_back')
            self.assertEqual(ports.applied_steps(), ())
            self.assertIsNone(self.db.execute(
                "SELECT to_regclass('core.upgrade_race_probe')").fetchone()[0])
            self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
