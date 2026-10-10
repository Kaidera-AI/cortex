"""H-D44910:26 current rule; original epoch tests are kept byte-frozen."""
import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('argv_producer',Path(__file__).with_name('build-manual-linux.py'))
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
class ArgvTests(unittest.TestCase):
 def test_all_seven_plans_keep_timestamp_without_implicit_conflicting_clock(self):
  rows=p.make_plan(Path('/fixture'),'9d509a3ddf8dbe21f41e44b50275d410a53a905c')['images']
  self.assertEqual(len(rows),7)
  for row in rows:
   a=row['argv'];self.assertEqual(a[a.index('--timestamp')+1],'1791586380')
   self.assertFalse(any(x.startswith('SOURCE_DATE_EPOCH=') for x in a),row['role'])
   self.assertNotIn('--source-date-epoch',a);self.assertNotIn('--rewrite-timestamp',a)
if __name__=='__main__':unittest.main()
