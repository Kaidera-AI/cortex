"""Frozen REPLAY-LOCK-001 competing-producer control; copied native fixture only."""
from pathlib import Path
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


if __name__ == '__main__':
    unittest.main()
