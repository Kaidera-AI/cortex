"""Actual typed classifier CLI decode failure must exit76, without a traceback."""
from pathlib import Path
import subprocess
import sys
import json
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from support import Fixture


class DecodeCLICase(unittest.TestCase):
    def test_decode_failure_is_typed_76(self):
        f=Fixture();self.addCleanup(f.close)
        row=f.file('bad-console.txt',b'not base64 !\n')
        result=subprocess.run([sys.executable,str(ROOT/'run8.py'),'classify-console','--input',row['path']],capture_output=True,text=True)
        self.assertEqual(result.returncode,76)
        self.assertEqual(json.loads(result.stdout)['state'],'DECODE_STOP')
        self.assertFalse(json.loads(result.stdout)['retryable'])
        self.assertNotIn('Traceback',result.stderr)


if __name__=='__main__':unittest.main(verbosity=2)
