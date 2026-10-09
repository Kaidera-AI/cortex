"""A final compilation must refuse a leftover from an earlier creating RUN."""
import os
from pathlib import Path
import subprocess
import unittest

HERE = Path(__file__).resolve().parents[1]
STALE = Path('/home/kaidera/.cache/Microsoft/DeveloperTools/.onnxruntime/deviceid')


class FinalCompilationGuardTests(unittest.TestCase):
    def test_final_entrypoint_refuses_late_cleanup(self):
        self.assertEqual(os.getuid(), 10001)
        STALE.parent.mkdir(parents=True, exist_ok=True)
        STALE.write_bytes(b'earlier-layer-leftover')
        try:
            result = subprocess.run(['/usr/local/bin/python', '-I', str(HERE / 'finalize-build.py')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('RuntimeError: byproduct remained after creating RUN:', result.stderr)
            self.assertEqual(STALE.read_bytes(), b'earlier-layer-leftover')
        finally:
            # The fixture's creator owns its own removal, even on assertion failure.
            STALE.unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
