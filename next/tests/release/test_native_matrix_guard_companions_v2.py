"""Version 2: bind effective used-module/source and unchanged negative BODY; v1 retained."""
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


def effective_binding(module_key, expected_path, expected_sha256, executed_files):
    """Observe only an already-used module; never import to manufacture use."""
    loaded=sys.modules.get(module_key)
    actual=getattr(loaded,'__file__',None) if loaded is not None else None
    expected=Path(expected_path).resolve()
    resolved=Path(actual).resolve() if actual is not None else None
    used=resolved is not None and str(resolved) in {str(Path(name).resolve()) for name in executed_files}
    digest=hashlib.sha256(resolved.read_bytes()).hexdigest() if resolved==expected and used else None
    return {'module_key':module_key,'loaded_file':str(resolved) if resolved else None,
            'sha256':digest,'executed':used,'bound':resolved==expected and used and digest==expected_sha256}


class MatrixGuardCompanionsV2(unittest.TestCase):
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
            guard_parts=list(source_path.relative_to(root/'src').with_suffix('').parts)
            if guard_parts[-1]=='__init__':guard_parts.pop()
            guard_key='.'.join(guard_parts)
            expected_id=original.id()
            expected_test_sha=hashlib.sha256((root/'tests/release'/(module_name+'.py')).read_bytes()).hexdigest()
            code="""import unittest,json,sys,socket,subprocess,importlib,hashlib
from pathlib import Path
def denied(*a,**k):raise AssertionError('MATRIX_GUARD_NETWORK_PROCESS_DENIED')
socket.socket.connect=denied;socket.create_connection=denied;socket.getaddrinfo=denied;subprocess.Popen=denied
module=importlib.import_module(sys.argv[1]);result=unittest.TestResult();executed=set()
def observe(frame,event,arg):
 if event=='call':executed.add(frame.f_code.co_filename)
sys.setprofile(observe)
try:getattr(module,sys.argv[2])(sys.argv[3]).run(result)
finally:sys.setprofile(None)
# The guard module is looked up after the actual case, never imported here.
loaded=sys.modules.get(sys.argv[5]);actual=getattr(loaded,'__file__',None) if loaded else None
actual_path=Path(actual).resolve() if actual else None;expected=Path(sys.argv[4]).resolve()
used=actual_path is not None and str(actual_path) in {str(Path(name).resolve()) for name in executed}
actual_sha=hashlib.sha256(actual_path.read_bytes()).hexdigest() if actual_path==expected and used else None
ids=[t.id() for t,_ in result.failures];test_path=Path(module.__file__).resolve();test_sha=hashlib.sha256(test_path.read_bytes()).hexdigest()
proof={'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'ids':ids,'guard_module':sys.argv[5],'actually_used_file':str(actual_path) if actual_path else None,'actually_used_sha256':actual_sha,'executed':used,'negative_test_file':str(test_path),'negative_test_sha256':test_sha}
print(json.dumps(proof))
valid=result.testsRun==1 and len(result.failures)==1 and not result.errors and ids==[sys.argv[7]] and actual_path==expected and used and actual_sha==sys.argv[6] and test_sha==sys.argv[8]
sys.exit(0 if valid else 1)
"""
            environment={k:v for k,v in os.environ.items() if k not in ('PYTHONHOME','PYTHONPATH')}
            environment['PYTHONPATH']=':'.join([str(stage/'next/src'),str(stage/'next/tests/release'),sysconfig.get_paths()['purelib']])
            result=subprocess.run([sys.executable,'-S','-B','-c',code,module_name,class_name,'test_negative_'+fault+'_api',str(target),guard_key,hashlib.sha256(mutated).hexdigest(),expected_id,expected_test_sha],cwd=stage,env=environment,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            proof=json.loads(result.stdout);self.assertEqual(proof['errors'],0);self.assertTrue(proof['executed']);self.assertEqual(proof['ids'],[expected_id]);self.assertEqual(Path(proof['actually_used_file']).resolve(),target.resolve());self.assertEqual(proof['actually_used_sha256'],hashlib.sha256(mutated).hexdigest());self.assertEqual(proof['negative_test_sha256'],expected_test_sha)


# One negative per distinct reason, with paired positives in the same frozen matrix.
for module_name,class_name,negatives in [('test_native_runtime_matrix','RuntimeMatrix',runtime.RUNTIME_NEGATIVES),('test_native_layer_matrix','LayerMatrix',layers.LAYER_NEGATIVES)]:
    seen=set()
    for fault,reason in negatives.items():
        if reason in seen:continue
        seen.add(reason)
        def decisive(self,module_name=module_name,class_name=class_name,fault=fault,reason=reason):self.decisive(module_name,class_name,fault,reason)
        setattr(MatrixGuardCompanionsV2,'test_guard_'+reason,decisive)


if __name__=='__main__':unittest.main(verbosity=2)
