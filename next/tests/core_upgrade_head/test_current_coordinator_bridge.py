"""Host-only current-manifest/upgrade bridge. Recording DB is NOT PostgreSQL proof."""
import ast
from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import types
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from cortex_core.migrations import apply_migrations, MigrationError

ROOT = Path(__file__).resolve().parents[2]
FROZEN_ENTRIES = [{'id': 'core-0001', 'file': 'core/001-core.sql', 'sha256': 'e838f374daf79aa120a2b9a47563a221a946f16f26977873e3bfb9d447e40147'}, {'id': 'auth-0001', 'file': 'auth/001-auth.sql', 'sha256': '97c1b435d9c31d00df9089a45c289fef79d41f8709d5c27c598e15e00bb5167e'}, {'id': 'coordination-0001', 'file': 'coordination/001-coordination.sql', 'sha256': 'c7c8d5f2de33acc07fd7c719d9f84a7a015a2c99c4cfb7a606c1ba4af812d9b1'}, {'id': 'retrieval-0000', 'file': 'retrieval/000-canonical.sql', 'sha256': '5d5ec69e3cc5addc7052c36717ef60fb7e66d8a9c089de713d9936f387b8cfd2'}, {'id': 'auth-0002', 'file': 'auth/002-isolation.sql', 'sha256': 'dc60e5fb24b525d8722792a24e846b6b3c78b141ee7ebc1a3b5a2dae81a79681'}, {'id': 'coordination-0002', 'file': 'coordination/002-record-replay.sql', 'sha256': '683f362d4070b61c1df44837d066b2dcac4c83d9507e1f403ccfca46be84ce87'}, {'id': 'auth-0003', 'file': 'auth/003-identity.sql', 'sha256': '678a6d5ba058b10f5293f03ffb13480a77f6b77a00f329dcb9434074c8cf5fa8'}, {'id': 'outbox-0002', 'file': 'coordination/002-outbox.sql', 'sha256': '125d948ddd5f2360e039a057fc944fc23d0652e4c1367533d3081e8bd9066a8e'}, {'id': 'module-consumers-0003', 'file': 'coordination/003-module-consumers.sql', 'sha256': 'bbde558b050a8121fe63e6eba1002058a8ec47731158a1c8375fbf633f33451a'}, {'id': 'api-replay-0004', 'file': 'coordination/004-api-replay.sql', 'sha256': 'b8f7651132e71ac8080b8be2aa6a38c066a25a8afb794f4060c174bae3780771'}, {'id': 'retrieval-0001', 'file': 'retrieval/001-pg-search.sql', 'sha256': 'bf1a5c1b14d57da70013e9ba4c90126797ab1e85533ad1d04db7f7780f113d84'}, {'id': 'retrieval-0002', 'file': 'retrieval/002-query-cache.sql', 'sha256': '02f5baf51828ae920988e37aac19169f78039e79481e413a6e1b0907fc62d868'}, {'id': 'retrieval-0003', 'file': 'retrieval/003-pg-graph.sql', 'sha256': 'bc12fe454576f666879151425b8752ad0aaa9ffeb241b3fee252e1a9d58e48cb'}, {'id': 'session-source-0005', 'file': 'coordination/005-session-source.sql', 'sha256': '81e7955f1792852160e384408557c8476abde0830ca72d2863ef51b3f81e8af4'}, {'id': 'retrieval-0004', 'file': 'retrieval/004-request-authority.sql', 'sha256': '3e7034a2c110f015b78b8efb88943e86f1a44ebf3153603550c6d4e1b1acee0e'}, {'id': 'auth-0004', 'file': 'auth/004-first-identity-capture.sql', 'sha256': '6c2a630667f51af9452b28bd6e559464007ea15162722acdf1fcdd7d15e02705'}, {'id': 'auth-0005', 'file': 'auth/005-first-identity.sql', 'sha256': '22964015953d66eebcd31cf2a0b10deaef2e00029c1390673afd3cff4ae6d09d'}, {'id': 'auth-0006', 'file': 'auth/006-enrollment-readiness.sql', 'sha256': '848840260b90e3cd88dc77d25acad908c63bcf711452eb1669e7ae4e833cee72'}, {'id': 'session-ids-read-0007', 'file': 'coordination/007-session-ids-read.sql', 'sha256': '449c944cb57156b5ffcc1b6b58eec347e181adafc30fad3826aa74dbc90caa64'}]
FROZEN_TABLES = ('auth.credentials', 'auth.first_identity_receipts', 'auth.permission_generations', 'auth.principals', 'auth.project_grants', 'coordination.consumer_aggregate_heads', 'coordination.consumer_checkpoints', 'coordination.consumer_event_outcomes', 'coordination.consumer_project_checkpoints', 'coordination.feed_state', 'coordination.idempotency', 'coordination.job_attempts', 'coordination.job_results', 'coordination.jobs', 'coordination.leases', 'coordination.outbox', 'coordination.published_events', 'coordination.quarantine', 'coordination.session_sources', 'coordination.snapshot_floors', 'core.analytics_facts', 'core.blob_manifests', 'core.extraction_facts', 'core.installations', 'core.payloads', 'core.projects', 'core.record_aliases', 'core.record_revisions', 'core.records', 'core.tenants', 'retrieval.embedding_models', 'retrieval.embeddings', 'retrieval.generations', 'retrieval.graph_applied', 'retrieval.graph_edges', 'retrieval.graph_generations', 'retrieval.graph_nodes', 'retrieval.graph_state', 'retrieval.query_embeddings', 'retrieval.search_sources', 'retrieval.search_state', 'retrieval.search_vectors')
STEPS = ('auth-0004', 'auth-0005', 'auth-0006', 'session-ids-read-0007')
OLD_POSITION9 = '460399154d5e657a75916cf0cc6381921ad10a6204f7b6a00e71862198ade629'

class Cursor:
    def __init__(self, rows=()): self.rows = rows
    def fetchall(self): return list(self.rows)

class RecordingDB:
    """Record real migration-port decisions against frozen bytes, with no DB engine."""
    autocommit = True
    def __init__(self):
        self.ledger = {x['id']: x['sha256'] for x in FROZEN_ENTRIES[:15]}
        self.migration_calls = []
        self.duplicate_ledger_inserts = []
        self.trace = []
        self.data = {'canonical': b'synthetic retained bytes', 'grant_generation': 7,
                     'model': ('fixture', 768), 'outbox': (1,), 'consumer': 1}
        self.sql_ids = {hashlib.sha256((ROOT/'schema'/x['file']).read_bytes()).hexdigest():
                        x['id'] for x in FROZEN_ENTRIES}

    @contextmanager
    def transaction(self):
        before = copy.deepcopy((self.ledger, self.data))
        try: yield
        except BaseException:
            self.ledger, self.data = before
            raise

    def execute(self, sql, args=None, **kwargs):
        self.trace.append(sql)
        if sql.startswith('SELECT migration_id,sha256'):
            return Cursor(self.ledger.items())
        if sql.startswith('INSERT INTO core.schema_migrations'):
            identity, digest = args
            if identity in self.ledger: self.duplicate_ledger_inserts.append(identity)
            self.ledger[identity] = digest
            return Cursor()
        digest = hashlib.sha256(sql.encode()).hexdigest()
        if digest in self.sql_ids:
            self.migration_calls.append(self.sql_ids[digest])
        elif not (sql.startswith('SELECT pg_advisory_xact_lock') or
                  sql.startswith('CREATE SCHEMA IF NOT EXISTS core') or
                  sql.startswith('CREATE TABLE IF NOT EXISTS core.schema_migrations')):
            raise AssertionError('unexpected migration SQL in recording boundary')
        return Cursor()

class CurrentBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['CORTEX_U13_CASE_ROOT'],
                                               prefix='current-bridge-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = RecordingDB()

    def api(self):
        try:
            from cortex_core.upgrade import Admission, AtomicUpgradeJournal, UpgradeCoordinator
        except ImportError:
            self.fail('current Core coordinator/journal/admission surface is absent')
        return Admission, AtomicUpgradeJournal, UpgradeCoordinator

    def staged(self, through):
        try: return apply_migrations(self.db, through=through)
        except TypeError as error:
            self.fail(f'current migration port lacks explicit through: {error}')

    def ports(self):
        _, Journal, _ = self.api()
        outer = self
        class Ports:
            def __init__(self):
                self.journal = Journal(outer.root/'journal.json')
                self.operation_lock_path = outer.root/'operation.lock'
                self.backup = None
                self.calls = []
                self.fail_after_commit = None
                self.preservation = True
            def load(self): return self.journal.load()
            def save(self, state, *, expected): self.journal.save(state, expected=expected)
            def fence_old(self): self.calls.append('fence_old')
            def fence_new(self): self.calls.append('fence_new')
            def verified_backup(self):
                self.calls.append('backup')
                self.backup = copy.deepcopy((outer.db.ledger, outer.db.data))
                return True
            def restore(self):
                self.calls.append('restore')
                outer.db.ledger, outer.db.data = copy.deepcopy(self.backup)
            def applied_steps(self):
                return tuple(x for x in STEPS if x in outer.db.ledger)
            def apply(self, *, through):
                outer.staged(through)
                if self.fail_after_commit == through:
                    self.fail_after_commit = None
                    raise InterruptedError('recorded commit before journal save')
            def validate_preservation(self):
                return self.preservation and outer.db.data == self.backup[1]
        return Ports()

    def admission(self, edition='standalone'):
        Admission, _, _ = self.api()
        # Explicit synthetic policy facts; never a released old/native pair.
        return Admission(old_release='v0.1.011', new_release='v0.1.012',
                         old_manifest_sha256='1'*64 if edition=='standalone' else '3'*64,
                         new_manifest_sha256='2'*64 if edition=='standalone' else '4'*64,
                         old_schema=1, new_schema=2, old_event=3, new_event=4,
                         expand_steps=STEPS,
                         readers=(('v0.1.011',1),('v0.1.012',2)),
                         writers=(('v0.1.011',1),('v0.1.012',2)))

    def test_current_fixture_exact_19_hashes_42_tables_and_donor_differences(self):
        manifest = json.loads((ROOT/'schema/manifest.json').read_bytes())
        self.assertEqual(manifest['migrations'], FROZEN_ENTRIES)
        self.assertEqual(len(FROZEN_ENTRIES), 19)
        self.assertEqual(tuple(x['id'] for x in FROZEN_ENTRIES[15:]), STEPS)
        self.assertNotEqual(FROZEN_ENTRIES[8]['sha256'], OLD_POSITION9)
        self.assertEqual(FROZEN_ENTRIES[8]['sha256'],
                         'bbde558b050a8121fe63e6eba1002058a8ec47731158a1c8375fbf633f33451a')
        tables = set()
        for entry in FROZEN_ENTRIES:
            raw = (ROOT/'schema'/entry['file']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), entry['sha256'])
            tables.update(re.findall(r'CREATE TABLE\s+(?:IF NOT EXISTS\s+)?([a-z_]+\.[a-z_]+)',
                                     raw.decode(), re.I))
        self.assertEqual(tables, set(FROZEN_TABLES))
        self.assertEqual(len(tables), 42)  # Static DDL, not a native catalog receipt.

    def test_staged_each_current_suffix_and_reentry_uses_full_current_ledger(self):
        for step in STEPS:
            self.assertEqual(self.staged(step), [step])
            self.assertEqual(self.staged(step), [])
            self.assertEqual(self.staged(STEPS[0]), [])
        self.assertEqual(self.db.migration_calls, list(STEPS))
        self.assertEqual(list(self.db.ledger.items()),
                         [(x['id'],x['sha256']) for x in FROZEN_ENTRIES])

    def test_unknown_target_and_donor_checksum_refuse_before_later_sql(self):
        try:
            with self.assertRaises(MigrationError): self.staged('ingest-head-0006')
        except TypeError as error: self.fail(str(error))
        self.assertEqual(self.db.migration_calls, [])
        self.db.ledger['module-consumers-0003'] = OLD_POSITION9
        with self.assertRaisesRegex(MigrationError, 'checksum differs'):
            self.staged(STEPS[0])
        self.assertEqual(self.db.migration_calls, [])

    def test_nonprefix_current_ledger_refuses_before_current_suffix(self):
        del self.db.ledger['core-0001']
        with self.assertRaisesRegex(MigrationError, 'not a prefix'):
            self.staged(STEPS[0])
        self.assertEqual(self.db.migration_calls, [])

    def test_event_bearing_admission_and_writer_guard_for_both_edition_fixtures(self):
        for edition in ('standalone','production'):
            with self.subTest(edition=edition):
                value = self.admission(edition)
                self.assertEqual((value.old_event,value.new_event),(3,4))
                self.assertTrue(value.writer_allowed(value.new_release,value.new_schema))
                self.assertFalse(value.writer_allowed(value.new_release,value.old_schema))

    def test_current_suffix_commit_before_journal_reentry_and_durable_acceptance(self):
        _, Journal, Coordinator = self.api()
        ports = self.ports()
        upgrade = Coordinator(self.admission(), ports)
        upgrade.prepare()
        self.assertEqual(ports.calls[:2], ['fence_old','backup'])
        before = copy.deepcopy(self.db.data)
        for step in STEPS:
            ports.fail_after_commit = step
            with self.assertRaises(InterruptedError): upgrade.advance()
            self.assertEqual(Coordinator(self.admission(), ports).advance(), step)
            self.assertEqual(self.db.data, before)
        self.assertEqual(self.db.migration_calls, list(STEPS))
        self.assertEqual(ports.applied_steps(), STEPS)
        upgrade.accept()
        self.assertEqual(Journal(ports.journal.path).load()['phase'], 'accepted')
        with self.assertRaisesRegex(ValueError, 'fix_forward_only'):
            Coordinator(self.admission(), ports).rollback()
        self.assertNotIn('restore', ports.calls)

    def test_current_suffix_rollback_after_every_prefix_retains_journal_history(self):
        _, _, Coordinator = self.api()
        for count in range(5):
            with self.subTest(count=count):
                self.db = RecordingDB()
                # A new owned journal per synthetic operation, never reset a real installation.
                self.root = Path(self.tmp.name)/str(count)
                ports = self.ports()
                upgrade = Coordinator(self.admission(), ports)
                upgrade.prepare()
                for _ in range(count): upgrade.advance()
                upgrade.rollback()
                self.assertEqual(ports.load()['phase'], 'rolled_back')
                self.assertEqual(ports.load()['steps'], STEPS[:count])
                self.assertEqual(ports.applied_steps(), ())
                self.assertEqual(self.db.ledger,
                                 {x['id']:x['sha256'] for x in FROZEN_ENTRIES[:15]})
                with self.assertRaises(ValueError): upgrade.advance()

    def test_preservation_failure_keeps_current_prefix_prepared(self):
        _, _, Coordinator = self.api()
        ports = self.ports()
        upgrade = Coordinator(self.admission(), ports)
        upgrade.prepare()
        for _ in STEPS: upgrade.advance()
        ports.preservation = False
        with self.assertRaisesRegex(ValueError, 'preservation_failed'): upgrade.accept()
        self.assertEqual(ports.load()['phase'], 'prepared')

    def test_current_apply_excludes_rollback_restore_until_operation_drains(self):
        _, _, Coordinator = self.api()
        ports = self.ports()
        upgrade = Coordinator(self.admission(), ports)
        upgrade.prepare()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = ports.apply
        errors = []
        def paused_apply(*, through):
            entered.set()
            if not release.wait(3): raise AssertionError('fixture apply release timeout')
            original(through=through)
        ports.apply = paused_apply
        def advance():
            try: upgrade.advance()
            except BaseException as error: errors.append(error)
        def rollback():
            try: Coordinator(self.admission(), ports).rollback()
            except BaseException as error: errors.append(error)
            finally: finished.set()
        a, b = threading.Thread(target=advance), threading.Thread(target=rollback)
        a.start()
        try:
            self.assertTrue(entered.wait(3))
            b.start()
            completed_while_apply = finished.wait(.15)
        finally:
            release.set()
            a.join(3)
            if b.ident is not None: b.join(3)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertFalse(completed_while_apply,
                         'rollback restored while current apply was in flight')
        self.assertFalse(errors, [str(x) for x in errors])
        self.assertEqual(ports.load()['phase'], 'rolled_back')
        self.assertEqual(ports.applied_steps(), ())

    def test_current_rollback_excludes_acceptance_until_restore_finishes(self):
        _, _, Coordinator = self.api()
        ports = self.ports()
        upgrade = Coordinator(self.admission(), ports)
        upgrade.prepare()
        for _ in STEPS: upgrade.advance()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []
        accepts = []
        def paused_fence():
            entered.set()
            if not release.wait(3): raise AssertionError('fixture fence release timeout')
        ports.fence_new = paused_fence
        def rollback():
            try: Coordinator(self.admission(), ports).rollback()
            except BaseException as error: errors.append(error)
        def accept():
            try: Coordinator(self.admission(), ports).accept()
            except BaseException as error: accepts.append(error)
            finally: finished.set()
        a, b = threading.Thread(target=rollback), threading.Thread(target=accept)
        a.start()
        try:
            self.assertTrue(entered.wait(3))
            b.start()
            accepted_while_fence = finished.wait(.15)
        finally:
            release.set()
            a.join(3)
            if b.ident is not None: b.join(3)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertFalse(accepted_while_fence,
                         'acceptance escaped the current rollback operation lock')
        self.assertFalse(errors, [str(x) for x in errors])
        self.assertEqual([getattr(x,'code',None) for x in accepts], ['not_prepared'])
        self.assertEqual(ports.load()['phase'], 'rolled_back')

    def test_loaded_code_witness_compares_values_and_rejects_changed_code(self):
        path = ROOT/'tests/core_upgrade_signed/run_host.py'
        spec = importlib.util.spec_from_file_location('u13_witness_helper', path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        fingerprint = getattr(helper, 'code_fingerprint', None)
        self.assertTrue(callable(fingerprint), 'value-bound loaded-code witness is absent')
        first = compile('def probe(x): return x+1', 'witness-control.py', 'exec').co_consts[0]
        equal = first.replace()
        different = compile('def probe(x): return x+2', 'witness-control.py', 'exec').co_consts[0]
        self.assertEqual(first, equal)
        self.assertEqual(fingerprint(first), fingerprint(equal))
        self.assertNotEqual(fingerprint(first), fingerprint(different))
        # Actual imported production primitive and its fresh source compile are equal.
        def codes(code):
            yield code
            for value in code.co_consts:
                if isinstance(value, types.CodeType): yield from codes(value)
        source = ROOT/'src/cortex_core/migrations.py'
        expected = next(x for x in codes(compile(source.read_bytes(), str(source), 'exec'))
                        if x.co_qualname=='apply_migrations')
        self.assertEqual(apply_migrations.__code__, expected)
        self.assertEqual(fingerprint(apply_migrations.__code__), fingerprint(expected))

if __name__ == '__main__':
    unittest.main()
