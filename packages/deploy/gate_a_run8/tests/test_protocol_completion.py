"""Close protocol producer and deadline edges with local, fail-capable controls."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from custody import Refusal,json_bytes
from observations import validate_payload
import test_protocol_paths as protocol_controls
import test_dispatch_receipts as controls
from support import INSTANCE


class ProtocolCompletionCase(unittest.TestCase):
    setUp=controls.DispatchReceiptCase.setUp
    spec=controls.DispatchReceiptCase.spec
    store=controls.DispatchReceiptCase.store
    attempt=controls.DispatchReceiptCase.attempt
    successful_output=controls.DispatchReceiptCase.successful_output
    execute=protocol_controls.ProtocolCase.execute
    prepare_manager=protocol_controls.ProtocolCase.prepare_manager
    response=protocol_controls.ProtocolCase.response

    def test_console_timeout_is_clamped_to_remaining_wait_budget(self):
        spec=self.prepare_manager();spec['timeout_sec']=900
        now=[0];budgets=[]
        def runner(*a,**kw):
            budgets.append(kw['timeout']);now[0]+=480 if len(budgets)==1 else 50
            return SimpleNamespace(returncode=0,stdout=json_bytes(self.response('' if len(budgets)==1 else 'ssh-ed25519 AAAA one\n')),stderr=b'')
        def sleep(n):now[0]+=n
        result=self.attempt(lambda:self.execute('manager',runner,lambda:now[0],sleep))
        self.assertFalse(isinstance(result,str),result)
        self.assertEqual(budgets,[600,60])

    def test_native_install_observer_follows_completed_literal_records(self):
        protocol_controls.ProtocolCase.test_persistent_recording_is_a_guarded_dispatch_protocol(self)
        spec=self.f.B['commands']['HOST_BOUND']['literal_recorder'];spec['output_contract']='native-facts-json-v2'
        spec['native_observer']={'role':'install_observer','argv':[self.f.B['verifier']['path']],'timeout_sec':60}
        called=[]
        def runner(*a,**kw):
            called.append(a);_,records=self.S.read()
            raw={'schema':'cortex.gate-a.run8.native-facts.v2',**self.f.A.context,'execution_scope':'native',
                'phase':'INSTALLED','from_phase':'HOST_BOUND','observation_predecessor':records[-1],
                'input_digests':{'authority':'9'*64},'facts':{'instance_id':INSTANCE,'kos_install_receipt':{},
                'initial_appdb_doctor':{'status':'ready'},'cortex_check':{'healthy':True},
                'container_identity_map':{},'cm1_sha256':'a'*64,'credential_metadata':{'mode':384}}}
            return SimpleNamespace(returncode=0,stdout=json_bytes(raw),stderr=b'')
        result=self.attempt(lambda:self.execute('literal_recorder',runner,lambda:0,lambda n:None))
        self.assertFalse(isinstance(result,str),result)
        self.assertEqual(len(called),1)

    def test_native_nested_capture_fields_refuse(self):
        payload={'instance_id':INSTANCE,'owner_uid':1001,'boot_id':'fixture-boot','engine':{},
                 'host_key_binding':{},'local_watchdog':{'state':'ARMED'},'guest_watchdog':{'state':'ARMED'}}
        for key in ['engine','local_watchdog','guest_watchdog']:
            with self.subTest(key=key):
                bad=copy.deepcopy(payload);bad[key]['diagnostic']='synthetic captured secret';refused=False
                try:validate_payload('HOST_BOUND',bad,[])
                except Refusal:refused=True
                self.assertTrue(refused,key)


if __name__=='__main__':unittest.main(verbosity=2)
