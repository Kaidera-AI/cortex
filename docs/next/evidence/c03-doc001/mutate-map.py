from pathlib import Path
import sys,json,hashlib
sys.path.insert(0,'/tmp/next/scripts')
from test_receipts import classify,report,suite
path=Path('/tmp/next/scripts/check_legacy_map.py');original=path.read_bytes();before='if actual != fields:';after='if False:'
baseline=suite('/tmp/next/tests/artifact');assert classify(baseline,set())=='survived'
try:
 assert original.decode().count(before)==1
 path.write_text(original.decode().replace(before,after,1))
 result=suite('/tmp/next/tests/artifact');expected='test_legacy_map.LegacyMap.test_neighbor_declaration_cannot_be_borrowed'
 status=classify(result,{expected})
 print(json.dumps({'mutation':'neighbor field declarations admitted','source':'scripts/check_legacy_map.py','expected_test':expected,'status':status,'exit_code':result.returncode,'receipt':report(result),'stdout':result.stdout,'stderr':result.stderr}),flush=True)
 assert status=='killed'
finally:path.write_bytes(original)
restored=suite('/tmp/next/tests/artifact');assert classify(restored,set())=='survived'
print(json.dumps({'mutants':1,'killed':1,'inconclusive':[],'survivors':[],'restored_baseline_exit':restored.returncode,'source_restored_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'stdout':restored.stdout,'stderr':restored.stderr}))
