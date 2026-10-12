"""Reusable recovery RED: exact source probe fixture, unchanged recovery expectations.
No product imports or native/database/engine operations.
"""
import ast,contextlib,hashlib,io,sys,unittest
from pathlib import Path
PROBE=Path('/Volumes/WD-B-4TB/DevVault/helix/output/Cortex/pr71-c07-repair-mike-review-20261011/page-boundary.py')
assert hashlib.sha256(PROBE.read_bytes()).hexdigest()=='9465118df9004ac0c8ee5f4bc5f931df34daf29d0071b8a9abd9f2a9706116d2'
# Preserve the fixture and exact consumer AST replay. Remove only the original
# diagnostic assertions of the known-broken observations; otherwise a repaired
# source would abort fixture loading before its desired assertions can run.
tree=ast.parse(PROBE.read_text())
tree.body=[node for node in tree.body if not isinstance(node,ast.Assert)]
ns={'__file__':str(PROBE),'__name__':'mike_boundary_fixture'}
with contextlib.redirect_stdout(io.StringIO()):exec(compile(tree,str(PROBE),'exec'),ns)
sys.argv=sys.argv[:1]
class HeldContinuation(unittest.TestCase):
 def test_observed_held_successor_reaches_target_after_repair(self):
  actual=ns['after_cycle']
  self.assertEqual(actual['target'][str(ns['UUID'](int=50))],2,actual)
  self.assertEqual(actual['applied_cursor'],1001,actual)
if __name__=='__main__':unittest.main(verbosity=2)
