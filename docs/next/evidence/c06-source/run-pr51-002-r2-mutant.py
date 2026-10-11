"""Source-bound C06 allowlist guard removal, host stdlib and fixed classifier."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[4]
SCANNER = ROOT/'next/scripts/verify_writer_inventory.py'
TESTS = ROOT/'next/tests/outbox_inventory'
RUNNER = ROOT/'next/tests/test_receipts.py'
OUT = Path(__file__).with_name('pr51-002-r2-mutant.json')
assert not OUT.exists()
original = SCANNER.read_bytes()
before = "return parameters.count(name)==1 and not stores and not declarations and bool(calls) and all(len(n.args)>1 and"
after = "return parameters.count(name)==1 and True and not declarations and bool(calls) and all(len(n.args)>1 and"
assert original.decode().count(before) == 1


def suite():
    result = subprocess.run([sys.executable,str(RUNNER),str(TESTS)],capture_output=True,text=True)
    receipt = json.loads(next(line.split('=',1)[1] for line in result.stdout.splitlines()
                              if line.startswith('CORTEX_TEST_RESULT=')))
    return {'exit_code':result.returncode,'receipt':receipt,'stdout':result.stdout,'stderr':result.stderr}


baseline = suite()
assert baseline['exit_code']==0 and baseline['receipt']=={'tests_run':12,'failures':[],'errors':[]}
try:
    SCANNER.write_text(original.decode().replace(before,after))
    mutant_sha = hashlib.sha256(SCANNER.read_bytes()).hexdigest()
    mutant = suite()
finally:
    SCANNER.write_bytes(original)
restored = suite()
expected = {'test_outbox_inventory.WriterInventory.test_private_query_walrus_rebind_fails_closed',
            'test_outbox_inventory.WriterInventory.test_private_query_augassign_rebind_fails_closed'}
failures = mutant['receipt']['failures']
qualified = (mutant['exit_code']==1 and mutant['receipt']['tests_run']==12
             and not mutant['receipt']['errors'] and {row['id'] for row in failures}==expected
             and all(row['phase']=='test' and row['is_assertion']
                     and row['phase_attribution']=='traceback+active-unittest-v2' for row in failures)
             and restored['exit_code']==0 and not restored['receipt']['failures']
             and SCANNER.read_bytes()==original)
OUT.write_text(json.dumps({'passed':qualified,'source_before_sha256':hashlib.sha256(original).hexdigest(),
    'mutant_sha256':mutant_sha,'source_restored':SCANNER.read_bytes()==original,
    'anchor_before':before,'anchor_after':after,'baseline':baseline,'mutant':mutant,
    'restored':restored,'resource_operations':0},indent=2)+'\n')
assert qualified
