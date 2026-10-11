"""Host-only RED and pin-removal fault, with exact source restoration."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[4]
SCANNER=ROOT/'next/scripts/verify_writer_inventory.py'
TESTS=ROOT/'next/tests/outbox_inventory'
RUNNER=ROOT/'next/tests/test_receipts.py'
MODE=sys.argv[1]
assert MODE in ('red','mutant')
OUT=Path(__file__).with_name('pr51-002-r3-'+MODE+'.json')
assert not OUT.exists()
original=SCANNER.read_bytes()
expected={'test_outbox_inventory.WriterInventory.test_private_query_match_capture_fails_closed',
          'test_outbox_inventory.WriterInventory.test_private_query_harmless_statement_fails_closed'}
if MODE=='mutant':
    expected.update({'test_outbox_inventory.WriterInventory.test_private_query_walrus_rebind_fails_closed',
                     'test_outbox_inventory.WriterInventory.test_private_query_augassign_rebind_fails_closed'})


def suite():
    run=subprocess.run([sys.executable,str(RUNNER),str(TESTS)],capture_output=True,text=True)
    report=json.loads(next(line.split('=',1)[1] for line in run.stdout.splitlines()
                           if line.startswith('CORTEX_TEST_RESULT=')))
    return {'exit_code':run.returncode,'receipt':report,'stdout':run.stdout,'stderr':run.stderr}


baseline=None
if MODE=='red':
    replacement=subprocess.check_output(['git','-C',str(ROOT),'show',
                                         '943c3d4:next/scripts/verify_writer_inventory.py'])
else:
    baseline=suite()
    assert baseline['exit_code']==0 and baseline['receipt']=={'tests_run':14,'failures':[],'errors':[]}
    before="or normalized_function_digest(function)!=pin:"
    assert original.decode().count(before)==1
    replacement=original.decode().replace(before,'or False:').encode()
try:
    SCANNER.write_bytes(replacement)
    result=suite()
finally:
    SCANNER.write_bytes(original)
restored=suite()
failures=result['receipt']['failures']
passed=(result['exit_code']==1 and result['receipt']['tests_run']==14
        and not result['receipt']['errors'] and {row['id'] for row in failures}==expected
        and all(row['phase']=='test' and row['is_assertion']
                and row['phase_attribution']=='traceback+active-unittest-v2' for row in failures)
        and restored['exit_code']==0 and restored['receipt']=={'tests_run':14,'failures':[],'errors':[]}
        and SCANNER.read_bytes()==original)
OUT.write_text(json.dumps({'passed':passed,'mode':MODE,
    'source_before_sha256':hashlib.sha256(original).hexdigest(),
    'replacement_sha256':hashlib.sha256(replacement).hexdigest(),
    'source_restored':SCANNER.read_bytes()==original,'baseline':baseline,'result':result,
    'restored':restored,'resource_operations':0},indent=2)+'\n')
assert passed
