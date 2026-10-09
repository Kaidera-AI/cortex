"""C03's actual mutation caller must use the shared receipt implementation."""
from pathlib import Path
import subprocess
import sys
import unittest

NEXT = Path(__file__).resolve().parents[2]


class C03ReceiptLocation(unittest.TestCase):
    def test_c03_caller_imports_shared_helper(self):
        source = '''import pathlib,sys
next_path = pathlib.Path(sys.argv[1])
sys.path[:0] = [str(next_path/'scripts'), str(next_path/'src'), *sys.argv[2:]]
import mutate_schema
print(pathlib.Path(mutate_schema.receipt_suite.__globals__['__file__']).resolve())
'''
        # Carry dependency locations into -I; no ambient PYTHONPATH or cwd imports.
        dependencies = [path for path in sys.path if path
                        and not Path(path).resolve().is_relative_to(NEXT)]
        result = subprocess.run([sys.executable, '-I', '-c', source, str(NEXT), *dependencies],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str((NEXT / 'tests/test_receipts.py').resolve()))

    def test_no_private_receipt_copy_remains(self):
        self.assertFalse((NEXT / 'scripts/test_receipts.py').exists())
