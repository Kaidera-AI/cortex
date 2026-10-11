"""Downstream SSH consumes the observed console-bound private known-host file."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from custody import Refusal
from observations import ObservationStore
import dispatch
import test_dispatch_receipts as controls
from support import INSTANCE


class SSHBindingCase(unittest.TestCase):
    setUp=controls.DispatchReceiptCase.setUp
    spec=controls.DispatchReceiptCase.spec
    store=controls.DispatchReceiptCase.store
    attempt=controls.DispatchReceiptCase.attempt
    def test_strict_ssh_options_and_actual_known_hosts_are_bound_before_effect(self):
        spec=self.spec('admin_preparer','HOST_BOUND');spec.pop('output_contract');spec['ssh_transport']=True
        spec['argv'] += ['-F','/dev/null','-o','StrictHostKeyChecking=yes','-o','BatchMode=yes',
            '-o','ControlMaster=no','-o','ControlPath=none','-o','UserKnownHostsFile=@RUN8_KNOWN_HOSTS@','@RUN8_SSH_HOST@','true']
        hostfile=self.f.file('known-hosts/run8.known_hosts',b'fixture-host ssh-ed25519 AAAA\n')
        self.f.refresh();self.store('ALLOCATED')
        self.S.append('HOST_BOUND',{'instance_id':INSTANCE,'owner_uid':1001,'boot_id':'fixture-boot','engine':{},
            'host_key_binding':{'state':'BOUND','known_hosts_file':hostfile,'scan_host':'fixture-host',
                'strict_host_key_checking_required':True},'local_watchdog':{'state':'ARMED'},'guest_watchdog':{'state':'ARMED'},
            'admitted_command_inputs':self.f.B['phase_command_inputs']['HOST_BOUND']})
        calls=[]
        def run():return dispatch.execute(self.f.A,self.S,'admin_preparer',runner=lambda argv,**k:calls.append(argv) or SimpleNamespace(returncode=0,stdout=b'',stderr=b''),invocation_directory=self.invocations)
        result=self.attempt(run);self.assertFalse(isinstance(result,str),result)
        self.assertIn('UserKnownHostsFile='+hostfile['path'],calls[0]);self.assertIn('fixture-host',calls[0])
        self.assertFalse(any('@RUN8_' in x for x in calls[0]))
        spec['argv'][spec['argv'].index('StrictHostKeyChecking=yes')]='StrictHostKeyChecking=no';calls.clear()
        self.assertEqual(self.attempt(run),'ssh_strict_options')
        self.assertEqual(calls,[])


if __name__=='__main__':unittest.main(verbosity=2)
