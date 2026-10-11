import contextlib,io,runpy,sys,unittest
from pathlib import Path
ROOT=sys.argv[1]
with contextlib.redirect_stdout(io.StringIO()):
    evidence=runpy.run_path(str(Path(__file__).with_name("page-boundary.py")),init_globals={})
sys.argv=sys.argv[:1]
class HeldContinuation(unittest.TestCase):
    def test_observed_held_successor_reaches_target_after_repair(self):
        actual=evidence["after_cycle"]
        self.assertEqual(actual["target"][str(evidence["UUID"](int=50))],2,actual)
        self.assertEqual(actual["applied_cursor"],1001,actual)
if __name__=="__main__":unittest.main(verbosity=2)
