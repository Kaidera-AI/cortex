"""The move keeps one implementation and makes C01's real caller use it."""
from pathlib import Path
import subprocess
import sys
import unittest

NEXT = Path(__file__).resolve().parents[2]


class SharedLocation(unittest.TestCase):
    def test_old_copy_is_removed(self):
        self.assertFalse((NEXT / 'scripts/test_receipts.py').exists())

    def test_isolated_c01_caller_imports_shared_helper(self):
        source = """import pathlib,sys
next_path = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(next_path/'scripts'))
import mutate_contracts
print(pathlib.Path(mutate_contracts.receipt_suite.__globals__['__file__']).resolve())
"""
        result = subprocess.run([sys.executable, '-I', '-c', source, str(NEXT)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str((NEXT / 'tests/test_receipts.py').resolve()))
