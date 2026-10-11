"""Actual dispatcher protocols; external console/retirement calls are spies."""
import inspect
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import dispatch
from custody import Refusal,bound_json,json_bytes,measured_file
import test_dispatch_receipts as controls
from support import INSTANCE


class ProtocolCase(unittest.TestCase):
    setUp=controls.DispatchReceiptCase.setUp
    spec=controls.DispatchReceiptCase.spec
    store=controls.DispatchReceiptCase.store
    attempt=controls.DispatchReceiptCase.attempt
    successful_output=controls.DispatchReceiptCase.successful_output

    def execute(self,role,runner,clock,sleep):
        extras={'clock':clock,'sleep':sleep} if 'clock' in inspect.signature(dispatch.execute).parameters else {}
        return dispatch.execute(self.f.A,self.S,role,runner=runner,invocation_directory=self.invocations,**extras)

    def prepare_manager(self):
        spec=self.spec('manager','ALLOCATED');spec['protocol']='bounded-console-v2'
        for role in ['retirer','cleanup_verifier']:self.spec(role,'FAILURE')
        self.f.B['commands']['FAILURE']['retirer'].pop('output_contract')
        self.f.refresh();self.store('ALLOCATED')
        return spec

    def response(self,console):
        native=__import__('json').loads(self.successful_output('manager').stdout)
        return {'schema':'cortex.gate-a.run8.console-attempt.v2',**self.f.A.context,'instance_id':INSTANCE,
                'local_watchdog':{'state':'ARMED'},'console_output':console,
                'scan_host':'fixture-host','scan_text':'fixture-host ssh-ed25519 AAAA\n','native_output':native}

    def test_manager_dispatch_retries_zero_then_binds_shared_key(self):
        self.prepare_manager();now=[0];calls=[];replies=iter(['','ssh-ed25519 AAAA one\n'])
        def runner(*a,**k):calls.append(now[0]);return SimpleNamespace(returncode=0,stdout=json_bytes(self.response(next(replies))),stderr=b'')
        def sleep(n):now[0]+=n
        result=self.attempt(lambda:self.execute('manager',runner,lambda:now[0],sleep))
        self.assertFalse(isinstance(result,str),result)
        self.assertEqual(calls,[0,60])
        outputs=list((self.invocations/'public-output').glob('*.json'))
        self.assertEqual(bound_json(measured_file(outputs[0]))['phase'],'HOST_BOUND')

    def test_manager_conflict_dispatch_immediately_retires_and_verifies(self):
        self.prepare_manager();calls=[]
        def runner(argv,**kw):
            rows,_=self.S.read();calls.append(rows[-1]['phase'])
            if rows[-1]['phase']=='ALLOCATED':
                return SimpleNamespace(returncode=0,stdout=json_bytes(self.response('ssh-ed25519 AAAA one\nssh-ed25519 BBBB two\n')),stderr=b'')
            if len(calls)==2:return SimpleNamespace(returncode=0,stdout=b'',stderr=b'')
            _,records=self.S.read()
            raw={'schema':'cortex.gate-a.run8.native-facts.v2',**self.f.A.context,'execution_scope':'native',
                 'phase':'RETIRED','from_phase':'FAILURE','observation_predecessor':records[-1],
                 'input_digests':{'authority':'9'*64},'facts':{'retirement_receipt':{'fixture':True},'custody_complete':False}}
            return SimpleNamespace(returncode=0,stdout=json_bytes(raw),stderr=b'')
        result=self.attempt(lambda:self.execute('manager',runner,lambda:0,lambda n:self.fail('conflict slept')))
        self.assertEqual(result,'checked_command_failed')
        self.assertEqual(calls,['ALLOCATED','FAILURE','FAILURE'])
        rows,_=self.S.read();self.assertEqual(rows[-1]['phase'],'RETIRED')

    def test_persistent_recording_is_a_guarded_dispatch_protocol(self):
        import hashlib
        from support import VOLUME
        shell=measured_file(Path('/bin/bash'))
        self.f.B['sealed_files'][shell['path']]=shell
        inventory=self.f.B['command_inventory'];inventory['required_executable_paths'].append(shell['path']);inventory['root_roles']['literal_recorder']=shell['path']
        one=self.f.file('literal-one.sh',b'run8_guarded_value=42\n')
        two=self.f.file('literal-two.sh',b'test "$run8_guarded_value" = 42\nprintf "{\\\"initialized\\\":true}\\n"\n')
        for row in [one,two]:
            self.f.B['sealed_files'][row['path']]=row;inventory['required_executable_paths'].append(row['path'])
        self.f.B['command_inventory_file']=self.f.file('command-inventory.json',json_bytes(inventory))
        spec=self.spec('literal_recorder','HOST_BOUND');spec.pop('output_contract')
        spec.update(protocol='persistent-literals-v2',argv=[shell['path'],'-s'],cwd=str(self.f.root),
            literals=[{'name':'one','file':one,'public_contract':{},'expected_facts':{}},
                      {'name':'two','file':two,'public_contract':{'initialized':{'type':'boolean'}},'expected_facts':{'initialized':True}}])
        self.f.refresh();self.store('ALLOCATED')
        self.S.append('HOST_BOUND',{'instance_id':INSTANCE,'owner_uid':1001,'boot_id':'fixture-boot','engine':{},
            'host_key_binding':{},'local_watchdog':{'state':'ARMED'},'guest_watchdog':{'state':'ARMED'},
            'admitted_command_inputs':self.f.B['phase_command_inputs']['HOST_BOUND']})
        result=self.attempt(lambda:self.execute('literal_recorder',lambda *a,**k:SimpleNamespace(returncode=0,stdout=b'',stderr=b''),lambda:0,lambda n:None))
        self.assertFalse(isinstance(result,str),result)
        receipts=sorted((self.invocations/'literal-observations').glob('*.json')) if (self.invocations/'literal-observations').exists() else []
        self.assertEqual(len(receipts),2)
        self.assertEqual(bound_json(measured_file(receipts[-1]))['post_effect'],{'initialized':True})


if __name__=='__main__':unittest.main(verbosity=2)
