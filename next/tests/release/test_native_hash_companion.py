"""Length-preserving hash-only companion; existing frozen35 are untouched."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from unittest.mock import patch

import receipt_fixture as producer
import test_deciding_controls as deciding


def hash_only_case():
    original_tar=producer.tar
    layouts=[]
    def change(members):
        if 'app/module.py' in members:
            key=next(k for k in members if k.endswith('.pyc'))
            before=members[key]
            checksum=bytes([before[8]^1])+before[9:16]
            after=before[:8]+checksum+before[16:]
            layouts.append((before,after));members[key]=after
        return original_tar(members)
    with patch.object(producer,'tar',change):case=producer.receipt_fixture(checked_pyc=True)
    return case,layouts


class HashCompanion(unittest.TestCase):
    def test_valid_hash_positive_through_public_receipt(self):
        result=deciding.DecidingControls().verify(producer.receipt_fixture(checked_pyc=True))
        self.assertFalse(result.native_qualified)

    def test_hash_only_negative_has_exact_intended_reason(self):
        case,layouts=hash_only_case()
        self.assertTrue(layouts)
        for before,after in layouts:
            self.assertEqual(len(before),len(after))
            self.assertEqual(before[:8],after[:8])
            self.assertEqual(before[16:],after[16:])
            self.assertNotEqual(before[8:16],after[8:16])
        with self.assertRaisesRegex(ValueError,'^python_source_hash_policy$'):
            deciding.DecidingControls().verify(case)

    def test_sole_literal_hash_guard_removal_kills_unchanged_negative(self):
        root=Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix='r01-hash-guard-') as directory:
            stage=Path(directory)
            for rel in ['src/cortex_core','tests/release','contracts/release']:
                shutil.copytree(root/rel,stage/'next'/rel,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            artifact=stage/'next/src/cortex_core/release/artifacts.py'
            source=artifact.read_text()
            old="require(source in contents and raw[8:16] == _imp.source_hash(int.from_bytes(raw[:4],'little'),contents[source]), 'python_source_hash_policy')"
            new="require(source in contents, 'python_source_hash_policy')"
            self.assertEqual(source.count(old),1,'literal hash guard must be uniquely bound')
            artifact.write_text(source.replace(old,new))
            code="""import unittest,json,hashlib,sys,socket,subprocess
def denied(*args,**kwargs):raise AssertionError('HASH_CONTROL_NETWORK_PROCESS_DENIED')
socket.socket.connect=denied;socket.create_connection=denied;socket.getaddrinfo=denied;subprocess.Popen=denied
from pathlib import Path
import test_native_hash_companion as frozen
from cortex_core.release import artifacts
result=unittest.TestResult()
frozen.HashCompanion('test_hash_only_negative_has_exact_intended_reason').run(result)
print(json.dumps({'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'ids':[t.id() for t,_ in result.failures],'imported_artifact':artifacts.__file__,'sha256':hashlib.sha256(Path(artifacts.__file__).read_bytes()).hexdigest()}))
sys.exit(0 if result.testsRun==1 and len(result.failures)==1 and not result.errors else 1)
"""
            import os
            environment={k:v for k,v in os.environ.items() if k not in ('PYTHONHOME','PYTHONPATH')}
            environment['PYTHONPATH']=':'.join([str(stage/'next/src'),str(stage/'next/tests/release'),sysconfig.get_paths()['purelib']])
            child=subprocess.run([sys.executable,'-S','-B','-c',code],cwd=stage,env=environment,capture_output=True,text=True,timeout=20)
            self.assertEqual(child.returncode,0,child.stderr+child.stdout)
            receipt=json.loads(child.stdout)
            self.assertEqual(receipt['errors'],0)
            self.assertEqual(receipt['sha256'],hashlib.sha256(artifact.read_bytes()).hexdigest())
            self.assertEqual(Path(receipt['imported_artifact']).resolve(),artifact.resolve())


if __name__=='__main__':unittest.main(verbosity=2)
