"""Stdlib synthetic admission refusals; never starts a resource."""
import contextlib,io,json,runpy,subprocess,sys,unittest
from pathlib import Path
from unittest.mock import patch
GATE=Path(sys.argv[1])
sys.argv=sys.argv[:1]
class Admission(unittest.TestCase):
    def run_gate(self,mac,vm,active=True):
        calls=[]
        def read(args,**kw):
            calls.append(args)
            if args==['memory_pressure','-Q']:return f'System-wide memory free percentage: {mac}%\n'
            if args==['podman','machine','list','--format','json']:return json.dumps([dict(Name='synthetic-vm',Running=active,Default=True)])
            if args==['podman','machine','ssh','synthetic-vm','cat /proc/meminfo']:return f'MemTotal: 1000 kB\nMemAvailable: {vm*10} kB\n'
            self.fail('unexpected command: '+repr(args))
        output=io.StringIO()
        with patch('subprocess.check_output',side_effect=read),contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as caught:runpy.run_path(str(GATE),run_name='__main__')
        self.assertEqual(len(calls),3)
        return caught.exception.code,json.loads(output.getvalue())
    def test_low_mac_refuses(self):
        code,j=self.run_gate(34,70);self.assertEqual(code,1);self.assertFalse(j['passed']);self.assertEqual(j['resource_operations'],0)
    def test_low_vm_refuses(self):
        code,j=self.run_gate(70,34);self.assertEqual(code,1);self.assertFalse(j['passed']);self.assertEqual(j['resource_operations'],0)
    def test_both_at_floor_admit(self):
        code,j=self.run_gate(35,35);self.assertEqual(code,0);self.assertTrue(j['passed']);self.assertEqual(j['resource_operations'],0)
if __name__=='__main__':unittest.main(verbosity=2)
