"""Unchanged reviewer controls, explicit constructor-state fixture adapter."""
import hashlib,json,runpy,subprocess,sys,unittest
from pathlib import Path
root=Path(sys.argv[1]);control=root/'docs/next/evidence/c07-source/frozen/mike-repair-manifest.py'
ns=runpy.run_path(str(control))
ns['Harness'].module_id='graph'
ns['Harness']._state=lambda self:dict(generation=1,applied_cursor=0,scan_cursor=max(e.cursor for e in self.feed.page().events))
result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ns['ReviewRed']))
print(json.dumps(dict(head=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip(),control_sha256=hashlib.sha256(control.read_bytes()).hexdigest(),source_sha256={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'next/src/cortex_core/modules/consumer.py',root/'next/src/cortex_core/modules/protocol.py')},tests_run=result.testsRun,failures=len(result.failures),errors=len(result.errors),adapter='constructor module_id plus observed scan cursor; unchanged test/control bodies')))
raise SystemExit(0 if result.wasSuccessful() else 1)
