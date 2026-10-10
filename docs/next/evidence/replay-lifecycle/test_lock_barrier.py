"""Frozen REPLAY-LOCK-001 competing-producer control; copied native fixture only."""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest

from replay_lifecycle import Lifecycle
from test_lifecycle_pattern import Engine


class LockBarrier(unittest.TestCase):
    def test_failed_cleanup_blocks_competing_admission_until_exact_absence(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = Engine()
            first = Lifecycle(Path(folder), engine)
            first.acquire()
            name = 'kaidera-test-lock-first-1'
            first.create('container', name, ['podman', 'run', '--name', name, *first.labels()])
            engine.fail_remove = 2
            with self.assertRaises(RuntimeError):
                first.close()
            self.assertIsNone(first.lock)  # Frozen unchanged PR40 close contract.
            self.assertIn(name, engine.rows)

            second = Lifecycle(Path(folder), engine)
            try:
                with self.assertRaises(RuntimeError):
                    second.acquire()  # Must reconcile under lock or refuse admission.
                self.assertIsNone(second.lock)
                self.assertIn(name, engine.rows)
            finally:
                if second.lock is not None:
                    second.lock.close()
                    second.lock = None
                engine.fail_remove = 0
                first.close()

            third = Lifecycle(Path(folder), engine)
            third.acquire()
            self.assertFalse(engine.rows)
            third.close()

    def test_dirty_owned_marker_requires_immutable_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = Engine()
            first = Lifecycle(Path(folder), engine)
            first.acquire()
            name = 'kaidera-test-lock-bound-1'
            first.create('container', name, ['podman', 'run', '--name', name, *first.labels()])
            engine.fail_remove = 1
            with self.assertRaises(RuntimeError):
                first.close()
            marker = Path(folder) / 'tmp/cox-podman.lifecycle.json'
            original = marker.read_bytes()
            tampered = json.loads(original)
            tampered['binding'] = []
            marker.write_text(json.dumps(tampered) + '\n')
            second = Lifecycle(Path(folder), engine)
            try:
                with self.assertRaises(RuntimeError):
                    second.acquire()
                self.assertIn(name, engine.rows)
                self.assertIsNone(second.lock)
            finally:
                if second.lock is not None:
                    second.lock.close()
                    second.lock = None
                marker.write_bytes(original)
                marker.chmod(0o600)
                engine.fail_remove = 0
                first.close()

    def test_renamed_bound_id_prevents_name_only_absence(self):
        class IdAwareEngine(Engine):
            def __call__(self, command):
                args = command[1:]
                if len(args) == 3 and args[1] == 'exists' and len(args[2]) == 64:
                    present = any(row['Id'] == args[2] for row in self.rows.values())
                    return subprocess.CompletedProcess(command, 0 if present else 1, '', '')
                return super().__call__(command)

        with tempfile.TemporaryDirectory() as folder:
            engine = IdAwareEngine()
            first = Lifecycle(Path(folder), engine)
            first.acquire()
            name = 'kaidera-test-lock-renamed-1'
            changed = 'kaidera-test-lock-renamed-2'
            first.create('container', name, ['podman', 'run', '--name', name, *first.labels()])
            row = engine.rows.pop(name)
            row['Name'] = changed
            engine.rows[changed] = row
            try:
                with self.assertRaises(RuntimeError):
                    first.close()
                second = Lifecycle(Path(folder), engine)
                try:
                    with self.assertRaises(RuntimeError):
                        second.acquire()
                    self.assertIsNone(second.lock)
                finally:
                    if second.lock is not None:
                        second.lock.close()
                        second.lock = None
            finally:
                engine.rows.clear()  # Simulate external exact-ID removal before retry.
                first.close()


if __name__ == '__main__':
    unittest.main()
