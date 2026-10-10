"""Accepted-pattern controls; these run only inside the bounded test container."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


class Engine:
    def __init__(self):
        self.rows = {}
        self.removed = []
        self.fail_remove = 0
        self.fail_inspect = 0
        self.lost_ack = False
        self.foreign = False

    def __call__(self, command):
        args = command[1:]
        code, output = 0, ''
        if args[:2] == ['pod', 'create'] or args[0] == 'run':
            kind = 'pod' if args[0] == 'pod' else 'container'
            name = args[args.index('--name') + 1]
            labels = dict(args[i+1].split('=', 1) for i, a in enumerate(args) if a == '--label')
            if self.foreign:
                labels['kaidera.cox.lifecycle'] = 'foreign'
            self.rows[name] = {'kind': kind, 'Name': name, 'Id': 'a'*64, 'Labels': labels,
                               'Config': {'Labels': labels}, 'Containers': []}
            code = 1 if self.lost_ack else 0
        elif args[1] == 'exists':
            code = 0 if args[-1] in self.rows else 1
        elif args[1] == 'inspect':
            if self.fail_inspect:
                self.fail_inspect -= 1
                code = 125
            else:
                output = json.dumps([self.rows[args[-1]]])
        elif args[0] == 'rm' or args[:2] == ['pod', 'rm']:
            if self.fail_remove:
                self.fail_remove -= 1
                code = 125
            else:
                row = next(r for r in self.rows.values() if r['Id'] == args[-1])
                self.removed.append(args[-1])
                del self.rows[row['Name']]
        return subprocess.CompletedProcess(command, code, output, '')


class LifecyclePattern(unittest.TestCase):
    def stack(self, engine):
        from replay_lifecycle import Lifecycle
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return Lifecycle(Path(folder.name), engine)

    def create(self, stack, kind='container'):
        args = ['podman', 'pod', 'create'] if kind == 'pod' else ['podman', 'run']
        return stack.create(kind, 'kaidera-test-fixture-1', args + ['--name', 'kaidera-test-fixture-1'] + stack.labels())

    def test_completed_effect_before_ack_is_reconciled_for_both_kinds(self):
        for kind in ('pod', 'container'):
            with self.subTest(kind=kind):
                engine = Engine(); engine.lost_ack = True
                stack = self.stack(engine)
                with self.assertRaises(RuntimeError): self.create(stack, kind)
                self.assertTrue(stack.pending)
                stack.close()
                self.assertFalse(engine.rows)
                self.assertEqual(engine.removed, ['a'*64])

    def test_foreign_lifecycle_is_preserved_for_both_kinds(self):
        for kind in ('pod', 'container'):
            with self.subTest(kind=kind):
                engine = Engine(); engine.foreign = True
                stack = self.stack(engine)
                with self.assertRaises(RuntimeError): self.create(stack, kind)
                stack.close()
                self.assertTrue(engine.rows)
                self.assertFalse(engine.removed)

    def test_failed_removal_remains_pending_and_retries(self):
        engine = Engine(); stack = self.stack(engine); self.create(stack)
        engine.fail_remove = 1
        with self.assertRaises(RuntimeError): stack.close()
        self.assertTrue(stack.pending or stack.owned)
        stack.close()
        self.assertTrue(stack.cleanup_verified)
        self.assertFalse(engine.rows)

    def test_failed_inspection_remains_pending_and_retries(self):
        engine = Engine(); stack = self.stack(engine); self.create(stack)
        engine.fail_inspect = 1
        with self.assertRaises(RuntimeError): stack.close()
        self.assertTrue(stack.pending or stack.owned)
        stack.close()
        self.assertFalse(engine.rows)

    def test_fresh_lifecycle_and_owner_are_both_required(self):
        engine = Engine(); stack = self.stack(engine); other = self.stack(Engine())
        self.assertRegex(stack.lifecycle, r'^[0-9a-f]{32}$')
        self.assertNotEqual(stack.lifecycle, other.lifecycle)
        self.create(stack)
        engine.rows['kaidera-test-fixture-1']['Labels']['owner'] = 'another-worker'
        stack.close()
        self.assertTrue(engine.rows)
        self.assertFalse(engine.removed)

    def test_wrong_inspected_name_is_never_deleted(self):
        engine = Engine(); stack = self.stack(engine); self.create(stack)
        engine.rows['kaidera-test-fixture-1']['Name'] = 'another-name'
        with self.assertRaises(RuntimeError): stack.close()
        self.assertTrue(engine.rows)
        self.assertFalse(engine.removed)

    def test_pod_with_foreign_child_is_never_deleted(self):
        engine = Engine(); stack = self.stack(engine); self.create(stack, 'pod')
        engine.rows['kaidera-test-fixture-1']['Containers'] = [{'Id': 'b'*64}]
        with self.assertRaises(RuntimeError): stack.close()
        self.assertTrue(engine.rows)
        self.assertFalse(engine.removed)

    def test_password_discard_and_lock_release_even_on_cleanup_error(self):
        engine = Engine(); stack = self.stack(engine); stack.acquire(); self.create(stack)
        stack.password = 'synthetic-test-only'
        lock = stack.lock; engine.fail_remove = 1
        with self.assertRaises(RuntimeError): stack.close()
        self.assertIsNone(stack.password)
        self.assertIsNone(stack.lock)
        self.assertTrue(lock.closed)
        stack.close()


if __name__ == '__main__':
    unittest.main()
