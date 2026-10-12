"""Freeze literal guard BODY contracts; unavailable guards remain RED, not kills."""
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest

import test_native_runtime_matrix as runtime
import test_native_layer_matrix as layers


class MatrixGuardCompanions(unittest.TestCase):
    def decisive(self, module_name, class_name, fault, reason):
        module=runtime if module_name=='test_native_runtime_matrix' else layers
        original=getattr(module,class_name)('test_negative_'+fault+'_api')
        baseline=unittest.TestResult();original.run(baseline)
        self.assertTrue(baseline.wasSuccessful(),'specific negative must first reach its intended guard; unavailable profile/guard is RED')
        root=Path(__file__).resolve().parents[2]
        matches=[]
        for path in (root/'src/cortex_core/release').rglob('*.py'):
            raw=path.read_bytes();tree=ast.parse(raw)
            for node in ast.walk(tree):
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='require' and len(node.args)>=2 and isinstance(node.args[1],ast.Constant) and node.args[1].value==reason:
                    matches.append((path,raw,node.args[0]))
        self.assertEqual(len(matches),1,'bind one literal relevant guard, never broad parser bypass')
        source_path,raw,condition=matches[0]
        lines=raw.splitlines(keepends=True)
        start=sum(map(len,lines[:condition.lineno-1]))+condition.col_offset
        end=sum(map(len,lines[:condition.end_lineno-1]))+condition.end_col_offset
        mutated=raw[:start]+b'True'+raw[end:]
        with tempfile.TemporaryDirectory(prefix='r01-matrix-guard-') as directory:
            stage=Path(directory)
            for rel in ['src/cortex_core','tests/release','contracts/release']:
                shutil.copytree(root/rel,stage/'next'/rel,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            target=stage/'next'/source_path.relative_to(root);target.write_bytes(mutated)
            code="""import unittest,json,sys,socket,subprocess,importlib,hashlib
from pathlib import Path
def denied(*a,**k):raise AssertionError('MATRIX_GUARD_NETWORK_PROCESS_DENIED')
socket.socket.connect=denied;socket.create_connection=denied;socket.getaddrinfo=denied;subprocess.Popen=denied
module=importlib.import_module(sys.argv[1]);result=unittest.TestResult()
getattr(module,sys.argv[2])(sys.argv[3]).run(result)
path=Path(sys.argv[4]);print(json.dumps({'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'ids':[t.id() for t,_ in result.failures],'mutated_source':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}))
sys.exit(0 if result.testsRun==1 and len(result.failures)==1 and not result.errors else 1)
"""
            environment={k:v for k,v in os.environ.items() if k not in ('PYTHONHOME','PYTHONPATH')}
            environment['PYTHONPATH']=':'.join([str(stage/'next/src'),str(stage/'next/tests/release'),sysconfig.get_paths()['purelib']])
            result=subprocess.run([sys.executable,'-S','-B','-c',code,module_name,class_name,'test_negative_'+fault+'_api',str(target)],cwd=stage,env=environment,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            proof=json.loads(result.stdout);self.assertEqual(proof['errors'],0);self.assertEqual(proof['sha256'],hashlib.sha256(mutated).hexdigest())


# One negative per distinct reason, with paired positives in the same frozen matrix.
for module_name,class_name,negatives in [('test_native_runtime_matrix','RuntimeMatrix',runtime.RUNTIME_NEGATIVES),('test_native_layer_matrix','LayerMatrix',layers.LAYER_NEGATIVES)]:
    seen=set()
    for fault,reason in negatives.items():
        if reason in seen:continue
        seen.add(reason)
        def decisive(self,module_name=module_name,class_name=class_name,fault=fault,reason=reason):self.decisive(module_name,class_name,fault,reason)
        setattr(MatrixGuardCompanions,'test_guard_'+reason,decisive)


if __name__=='__main__':unittest.main(verbosity=2)
