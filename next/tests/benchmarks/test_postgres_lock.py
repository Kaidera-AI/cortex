"""Frozen admission controls: cleanup uncertainty cannot admit another lifecycle."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from vector_baseline import postgres
from test_postgres_cleanup import ResourceEngine


class CleanupAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='b01-lock-test-')
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.home_patch = patch.object(postgres.Path, 'home', return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.marker = self.home / '.cache/kaidera/b01-podman.pending.json'
        self.stacks = []
        self.engine = ResourceEngine(fault='container', error=subprocess.TimeoutExpired)
        self.addCleanup(self.finish)

    def stack(self):
        stack = postgres.DisposablePostgres(runner=self.engine)
        self.stacks.append(stack)
        return stack

    def finish(self):
        self.engine.remove_failures.clear()
        self.engine.inspect_failures.clear()
        self.engine.bad_inspection = False
        for stack in self.stacks:
            try:
                stack.close()
            except RuntimeError:
                pass

    def fail_cleanup(self, kind='container'):
        self.engine.remove_failures[kind] = 10
        stack = self.stack()
        with self.assertRaises(RuntimeError):
            stack.__enter__()
        self.assertFalse(stack.cleanup_verified)
        self.assertIsNone(stack.password)
        return stack

    def competing_acquire(self):
        before = copy.deepcopy(self.engine.resources)
        creates = list(self.engine.created)
        competitor = self.stack()
        error = None
        try:
            competitor.__enter__()
        except Exception as exc:
            error = exc
        self.assertEqual(self.engine.created, creates,
                         'cleanup uncertainty admitted another differently named lifecycle')
        self.assertEqual(self.engine.resources, before,
                         'refused admission must preserve prior external effects')
        self.assertIsInstance(error, RuntimeError)
        return competitor

    def test_failed_removal_blocks_competing_acquire_for_every_resource(self):
        for kind in ('container', 'volume', 'secret'):
            with self.subTest(kind=kind):
                self.fail_cleanup(kind)
                self.competing_acquire()
                self.finish()
                self.stacks.clear()

    def test_unknown_inspection_blocks_competing_acquire(self):
        self.engine.fault = 'volume'
        self.engine.inspect_failures['volume'] = 10
        first = self.stack()
        with self.assertRaises(RuntimeError):
            first.__enter__()
        self.assertFalse(first.cleanup_verified)
        self.competing_acquire()

    def test_pending_marker_survives_owner_unlock_and_competing_refusal(self):
        first = self.fail_cleanup()
        self.assertIsNone(first.lock, 'process mutex may close only with durable admission exclusion')
        self.competing_acquire()
        self.assertTrue(self.marker.exists(), 'refused contender cleared previous pending marker')
        data = json.loads(self.marker.read_text())
        self.assertEqual((data['name'], data['lifecycle']), (first.name, first.lifecycle))

    def test_retry_verified_cleanup_unblocks_a_fresh_lifecycle(self):
        first = self.fail_cleanup()
        self.engine.remove_failures.clear()
        first.close()
        self.assertTrue(first.cleanup_verified)
        self.assertFalse(self.marker.exists())
        before = len(self.engine.created)
        second = self.stack()
        with self.assertRaises(subprocess.TimeoutExpired):
            second.__enter__()
        self.assertGreater(len(self.engine.created), before)
        self.assertTrue(second.cleanup_verified)
        self.assertEqual(self.engine.resources, {kind: {} for kind in self.engine.resources})

    def test_acquire_reconciles_verified_absence_after_owner_is_gone(self):
        self.fail_cleanup()
        self.engine.resources = {kind: {} for kind in self.engine.resources}
        self.engine.remove_failures.clear()
        before = len(self.engine.created)
        second = self.stack()
        with self.assertRaises(subprocess.TimeoutExpired):
            second.__enter__()
        self.assertGreater(len(self.engine.created), before)
        self.assertTrue(second.cleanup_verified)
        self.assertFalse(self.marker.exists())

    def test_acquire_preserves_foreign_same_name_after_prior_owned_absence(self):
        first = self.fail_cleanup()
        self.engine.resources['container'][first.name]['labels'] = {'kaidera.b01.lifecycle': 'foreign'}
        foreign = copy.deepcopy(self.engine.resources['container'][first.name])
        self.engine.remove_failures.clear()
        second = self.stack()
        with self.assertRaises(subprocess.TimeoutExpired):
            second.__enter__()
        self.assertTrue(second.cleanup_verified)
        self.assertEqual(self.engine.resources['container'][first.name], foreign)
        self.assertNotIn(('container', first.name), self.engine.removed)
        self.assertFalse(self.marker.exists())

    def test_malformed_pending_marker_refuses_before_any_create(self):
        self.marker.parent.mkdir(parents=True)
        self.marker.write_text('{malformed')
        error = None
        try:
            self.stack().__enter__()
        except Exception as exc:
            error = exc
        self.assertEqual(self.engine.created, [], 'invalid prior identity must block admission')
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(self.marker.read_text(), '{malformed')

    def test_pending_marker_contains_identity_without_password(self):
        credentials = []
        engine = self.engine
        def runner(args, **kwargs):
            if args[:2] == ['secret', 'create']:
                credentials.append(kwargs['input'])
            return engine(args, **kwargs)
        engine.remove_failures['container'] = 1
        stack = postgres.DisposablePostgres(runner=runner)
        self.stacks.append(stack)
        with self.assertRaises(RuntimeError):
            stack.__enter__()
        self.assertTrue(self.marker.exists(), 'failed cleanup requires durable pending identity')
        content = self.marker.read_text()
        self.assertEqual(len(credentials), 1)
        self.assertNotIn(credentials[0], content)
        self.assertEqual(json.loads(content)['lifecycle'], stack.lifecycle)


if __name__ == '__main__':
    unittest.main()
