"""Public receipt allowlists reject copied capture and undeclared facts."""
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from custody import Refusal,json_bytes
from observations import validate_payload
from recorder import PersistentRecorder
from support import Fixture,INSTANCE,VOLUME


class PublicContractCase(unittest.TestCase):
    def setUp(self):self.f=Fixture();self.addCleanup(self.f.close)
    def test_extra_native_fact_cannot_carry_capture(self):
        refused=False
        try:validate_payload('ALLOCATED',{'instance_id':INSTANCE,'volume_id':VOLUME,'diagnostic':'synthetic captured secret'},[])
        except Refusal:refused=True
        self.assertTrue(refused)
    def test_recorder_observer_cannot_export_arbitrary_effect_string(self):
        R=PersistentRecorder(['/bin/bash','-s'],str(self.f.root),{'PATH':'/usr/bin:/bin'});self.addCleanup(R.close)
        literal=self.f.file('privacy-literal.sh',b"printf 'synthetic captured secret\\n'\n")
        refused=False
        try:R.run('privacy',literal,observer=lambda out,err:{'effect':out.decode().strip()})
        except Refusal:refused=True
        self.assertTrue(refused)


if __name__=='__main__':unittest.main(verbosity=2)
