"""Final failure cuts: public output, signed extraction and recorder lifetime."""
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from custody import Refusal,bound_json,json_bytes,public_data
from payload import pre_load
from recorder import PersistentRecorder
from support import Fixture
import test_dispatch_receipts as dispatch_controls
import keys


class NativeOutputCase(unittest.TestCase):
    setUp=dispatch_controls.DispatchReceiptCase.setUp
    spec=dispatch_controls.DispatchReceiptCase.spec
    store=dispatch_controls.DispatchReceiptCase.store
    call=dispatch_controls.DispatchReceiptCase.call
    attempt=dispatch_controls.DispatchReceiptCase.attempt
    def test_bad_destination_or_missing_payload_stops_after_effect(self):
        for destination,payload in [('ALLOCATED',{}),('HOST_BOUND',{})]:
            with self.subTest(destination=destination):
                if hasattr(self,'S'):self.f.close();self.setUp()
                self.spec('manager','ALLOCATED');self.f.refresh();self.store('ALLOCATED')
                _,records=self.S.read()
                raw={'schema':'cortex.gate-a.run8.native-facts.v2',**self.f.A.context,
                    'execution_scope':'native','phase':destination,'from_phase':'ALLOCATED',
                    'observation_predecessor':records[-1],'input_digests':{'authority':'9'*64},'facts':payload}
                self.attempt(lambda:self.call('manager',lambda *a,**k:SimpleNamespace(returncode=0,stdout=json_bytes(raw),stderr=b'')))
                rows,_=self.S.read();self.assertEqual(rows[-1]['phase'],'FAILURE')


class LastBoundariesCase(unittest.TestCase):
    def setUp(self):self.f=Fixture();self.addCleanup(self.f.close)

    def test_extracted_compose_and_self_checksums_cannot_escape_signed_map(self):
        # Valid source policy, same image locks/source, but different signed bytes.
        compose=copy.deepcopy(self.f.compose);compose['services']['cortex-api']['environment']={'UNREVIEWED_DELIVERY_VALUE':'changed'}
        row=self.f.file('payload/packages/deploy/install-compose.json',json_bytes(compose))
        install=copy.deepcopy(self.f.install);install['files']['packages/deploy/install-compose.json']=row['sha256']
        self.f.file('payload/packages/deploy/install.json',json_bytes(install))
        refused=False
        try:pre_load(self.f.A,self.f.manifest,self.f.literals(),self.f.B['delivery_binding'])
        except Refusal:refused=True
        self.assertTrue(refused,'self-updated checksums hid different signed delivery bytes')

    def test_secret_field_variants_refuse_public_persistence(self):
        for name in ['api_key','access_token','secret','secret_key','authorization_header']:
            with self.subTest(name=name):
                refused=False
                try:public_data({name:'synthetic secret marker'})
                except Refusal:refused=True
                self.assertTrue(refused,name)

    def test_console_key_arriving_at_deadline_is_never_accepted(self):
        now=[0];calls=[];retired=[]
        def invoke():
            calls.append(now[0]);return 'ssh-ed25519 AAAA late\n' if now[0]>=600 else ''
        def sleep(seconds):now[0]+=seconds
        result=keys.bind_console(invoke,lambda:retired.append(True),lambda:now[0],sleep,watchdog_armed=True)
        self.assertNotEqual(result['exit'],0)
        self.assertEqual(retired,[True])
        self.assertTrue(all(stamp<600 for stamp in calls))

    def test_recorder_input_delivery_obeys_execution_deadline(self):
        literal=self.f.file('large-literal.sh',b'sleep 30\n'+b'# filler\n'*30000)
        program='''import sys,time\nsys.path.insert(0,sys.argv[1])\nfrom recorder import PersistentRecorder\nfrom custody import measured_file,Refusal\nr=PersistentRecorder(['/bin/bash','-s'],sys.argv[2],{'PATH':'/usr/bin:/bin'})\ntry:r.run('large',measured_file(sys.argv[3]),timeout=1,observer=lambda o,e:{})\nexcept Refusal:print('refused')\nfinally:r.close()\n'''
        worker=subprocess.Popen([sys.executable,'-c',program,str(ROOT),str(self.f.root),literal['path']],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        timed_out=False
        try:out,err=worker.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            timed_out=True;os.killpg(worker.pid,signal.SIGKILL);out,err=worker.communicate()
        self.assertFalse(timed_out,'stdin blocked before deadline loop')
        self.assertEqual(worker.returncode,0,err)
        self.assertEqual(out.strip(),b'refused')

    def test_recorder_close_stops_its_owned_descendant(self):
        R=PersistentRecorder(['/bin/bash','-s'],str(self.f.root),{'PATH':'/usr/bin:/bin'});self.addCleanup(R.close)
        literal=self.f.file('background-literal.sh',b'sleep 30 &\nprintf "%s\\n" "$!"\n')
        pids=[]
        R.run('background',literal,observer=lambda out,err:pids.append(int(out.strip())) or {})
        R.close();pid=pids[0]
        try:
            state=subprocess.check_output(['ps','-o','state=','-p',str(pid)],text=True).strip()
        except subprocess.CalledProcessError:state=''
        if state and state!='Z':
            # Cleanup only this test-owned sleep, after measuring the regression.
            os.kill(pid,signal.SIGTERM)
        self.assertIn(state,['','Z'],'descendant is still executing')


if __name__=='__main__':unittest.main(verbosity=2)
