"""Per-file causal witnesses; reuse the shared expected-body classifier."""
import hashlib
import importlib.util
import json
import marshal
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

NEXT=Path(__file__).resolve().parents[4]
SPEC=[
 ('src/cortex_core/runtime/bootstrap_config.py',"if value['ai_mode'] != 'off':","if False:",'runtime','test_bootstrap_config.ConfigTests.test_closed_shapes_and_explicit_ai'),
 ('src/cortex_core/runtime/credentials.py','or before.st_mode & 0o077','or False','runtime','test_bootstrap_config.CredentialTests.test_unsafe_or_missing_secret_has_closed_error'),
 ('src/cortex_core/runtime/contracts.py','if TYPE_CHECKING:','if True:','runtime','test_bootstrap_config.ConfigTests.test_stdlib_only_host_decoder'),
 ('src/cortex_core/release/codec.py','import hashlib','import jsonschema\nimport hashlib','runtime','test_bootstrap_config.ConfigTests.test_stdlib_only_host_decoder'),
 ('src/cortex_core/migrations.py','if hashlib.sha256(data).hexdigest() != entry["sha256"]:','if False:','schema','test_readiness.ReadinessTests.test_prefix_byte_change_before_sql'),
 ('src/cortex_core/schema/migrate.py','return migrations.apply_migrations(connection, directory)','return []','schema','test_readiness.ReadinessTests.test_existing_runner_is_only_migration_engine'),
 ('src/cortex_core/schema/readiness.py','if len(rows) != len(expected) or dict(rows) != dict(expected):','if False:','schema','test_readiness.ReadinessTests.test_wrong_applied_order_identity_and_digest_refuse'),
]


def receipt_module():
    spec=importlib.util.spec_from_file_location('receipts',NEXT/'tests/test_receipts.py')
    receipts=importlib.util.module_from_spec(spec);spec.loader.exec_module(receipts)
    return receipts


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def admit_phase(result, expected, phase, source_hash, test_hash):
    """Narrow admission around the existing shared BODY classifier/report."""
    receipts=receipt_module();value=receipts.report(result)
    if (phase not in ('healthy','mutant','restored') or value is None
            or value['tests_run']!=len(expected) or value.get('skipped')!=0
            or not value.get('executed_code_fingerprints')
            or value.get('source_sha256')!=source_hash or value.get('test_sha256')!=test_hash):
        return False
    status=receipts.classify(result,expected)
    if phase!='mutant':
        return result.returncode==0 and status=='survived' and not value['failures']
    failed={row['id'].split(' (')[0] for row in value['failures']}
    return result.returncode==1 and status=='killed' and failed==expected


def phase_main():
    # Exact nodes and the shared AssertionResult retain fixture/BODY attribution.
    source,group,node,target=sys.argv[2:]
    source=Path(source);target=Path(target).resolve()
    target_name=str(target)
    testdir=source/'cortex_core'/group/'tests'
    sys.path.insert(0,str(source));sys.path.insert(0,str(testdir))
    testfile=testdir/(node.split('.')[0]+'.py')
    seen={}
    def witness(frame,event,arg):
        if event=='call' and frame.f_code.co_filename==target_name:
            key=frame.f_code.co_name+':'+str(frame.f_code.co_firstlineno)
            seen[key]=hashlib.sha256(marshal.dumps(frame.f_code)).hexdigest()
        return None
    receipts=receipt_module()
    sys.settrace(witness)
    tests=unittest.defaultTestLoader.loadTestsFromName(node)
    result=unittest.TextTestRunner(verbosity=2,resultclass=receipts.AssertionResult).run(tests)
    sys.settrace(None)
    value=dict(tests_run=result.testsRun,failures=result.assertions,
               errors=[dict(id=test.id(),traceback=traceback) for test,traceback in result.errors],
               skipped=len(result.skipped),source_sha256=digest(target),test_sha256=digest(testfile),
               executed_code_fingerprints=seen)
    print(receipts.MARKER+json.dumps(value),flush=True)
    return 0 if result.wasSuccessful() else 1


def main():
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
    receipts=receipt_module();rows=[]
    for index,(relative,old,new,group,expected) in enumerate(SPEC):
        with tempfile.TemporaryDirectory(prefix='mutant-',dir=out) as tmp:
            case=Path(tmp)/'next'
            shutil.copytree(NEXT/'src',case/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            shutil.copytree(NEXT/'schema',case/'schema')
            path=case/relative;before=path.read_bytes();text=before.decode()
            if text.count(old)!=1:raise ValueError('mutation anchor must be unique: '+relative)
            testfile=case/'src/cortex_core'/group/'tests'/(expected.split('.')[0]+'.py')
            test_hash=digest(testfile);phases=[]
            try:
                for phase in ('healthy','mutant','restored'):
                    path.write_text(text.replace(old,new,1)) if phase=='mutant' else path.write_bytes(before)
                    source_hash=digest(path)
                    command=[sys.executable,'-B',str(Path(__file__).resolve()),'--phase',
                             str(case/'src'),group,expected,str(path)]
                    result=subprocess.run(command,cwd=case,
                        env={**os.environ,'PYTHONPATH':str(case/'src'),'PYTHONDONTWRITEBYTECODE':'1'},
                        capture_output=True,text=True,timeout=90)
                    stem=f'{index+1:02d}-{phase}'
                    (out/(stem+'.stdout')).write_text(result.stdout)
                    (out/(stem+'.stderr')).write_text(result.stderr)
                    phases.append(dict(phase=phase,command=command,exit_code=result.returncode,
                        valid=admit_phase(result,{expected},phase,source_hash,test_hash),
                        source_sha256=source_hash,test_sha256=test_hash,
                        test_result=receipts.report(result),stdout=stem+'.stdout',stderr=stem+'.stderr'))
            finally:
                path.write_bytes(before)
            restored=digest(path)==hashlib.sha256(before).hexdigest()
            valid=len(phases)==3 and all(p['valid'] for p in phases) and restored
            row=dict(path=relative,expected_test=expected,phases=phases,
                     restored_source_matches=restored,classification='killed' if valid else 'inconclusive')
            rows.append(row);print(relative,row['classification'],flush=True)
    summary={'schema':'cortex.foundation.causal.v2','source_root':str(NEXT),
             'runner_sha256':digest(__file__),'classifier_sha256':digest(NEXT/'tests/test_receipts.py'),
             'mutants':rows,'killed':sum(r['classification']=='killed' for r in rows),
             'inconclusive':sum(r['classification']=='inconclusive' for r in rows),
             'phases_valid':sum(p['valid'] for r in rows for p in r['phases'])}
    (out/'RESULT.json').write_text(json.dumps(summary,indent=2)+'\n')
    return 0 if summary['killed']==len(SPEC) else 1


if __name__=='__main__':
    raise SystemExit(phase_main() if sys.argv[1:2]==['--phase'] else main())
