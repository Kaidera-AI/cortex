"""Remove each new scanner guard independently; no resource is created."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[4]
SCANNER=ROOT/'next/scripts/verify_writer_inventory.py'
TESTS=ROOT/'next/tests/outbox_inventory'
RUNNER=ROOT/'next/tests/test_receipts.py'
LABEL=sys.argv[1]
PYTHON=sys.argv[2]
assert LABEL in ('py312','py314')
OUT=Path(__file__).with_name('pr51-002-r4-mutants-'+LABEL+'.json')
assert not OUT.exists()
original=SCANNER.read_bytes()
recipes=(
    ('whole_file_pin',
     'if file_pin is None or normalized_function_digest(tree)!=file_pin:',
     'if file_pin is None or False:',
     'test_private_alias_caller_fails_closed',1),
    ('cross_file_reference',
     'if symbol in EXEMPT_SYMBOL_FILES and file not in EXEMPT_SYMBOL_FILES[symbol]:',
     'if False:',
     'test_cross_file_private_import_alias_fails_closed',1),
    ('dynamic_name',
     'if label is not None and (len(node.args)<=index or static_text(node.args[index]) is None):',
     'if False:',
     'test_dynamic_name_primitives_fail_closed',6),
)


def suite():
    result=subprocess.run([PYTHON,str(RUNNER),str(TESTS)],capture_output=True,text=True,
                          env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
    receipt=json.loads(next(line.split('=',1)[1] for line in result.stdout.splitlines()
                            if line.startswith('CORTEX_TEST_RESULT=')))
    return {'exit_code':result.returncode,'receipt':receipt}


baseline=suite()
assert baseline=={'exit_code':0,'receipt':{'tests_run':17,'failures':[],'errors':[]}}
rows=[]
for label,before,after,expected,count in recipes:
    assert original.decode().count(before)==1,label
    try:
        SCANNER.write_text(original.decode().replace(before,after))
        mutant_sha256=hashlib.sha256(SCANNER.read_bytes()).hexdigest()
        result=suite()
    finally:
        SCANNER.write_bytes(original)
    failures=result['receipt']['failures']
    qualified=(result['exit_code']==1 and result['receipt']['tests_run']==17
               and not result['receipt']['errors'] and len(failures)==count
               and {row['id'].split(' (')[0].rsplit('.',1)[-1] for row in failures}=={expected}
               and all(row['phase']=='test' and row['is_assertion'] and
                       row['phase_attribution']=='traceback+active-unittest-v2' for row in failures))
    rows.append({'guard':label,'mutant_sha256':mutant_sha256,'qualified':qualified,'result':result})
restored=suite()
passed=(all(row['qualified'] for row in rows) and restored==baseline and SCANNER.read_bytes()==original)
OUT.write_text(json.dumps({'passed':passed,'python':LABEL,
    'source_before_sha256':hashlib.sha256(original).hexdigest(),
    'source_restored':SCANNER.read_bytes()==original,'baseline':baseline,
    'mutants':rows,'restored':restored,'resource_operations':0},indent=2)+'\n')
assert passed
