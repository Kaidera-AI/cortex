"""Admission refusal cuts, actual default CLI, and custody controls."""
import copy
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from admission import validate_release,validate_rules
from custody import Refusal,bound_file,json_bytes,measured_file
from dispatch import checked_command
from observations import ObservationStore
from support import Fixture


class AuthorityCutsCase(unittest.TestCase):
    def setUp(self):self.f=Fixture();self.addCleanup(self.f.close)
    def refuses(self,fn):
        with self.assertRaises(Refusal):fn()

    def test_go_source_operation_scope_and_budget_each_refuse(self):
        for key,value in [('source_revision','a'*40),('run_id','another-run'),('client_token','another-token'),
                          ('owner_tag','another-owner'),('operation','BUILD'),('literal','old GO'),
                          ('scope','anything'),('static_binding_sha256','b'*64)]:
            with self.subTest(key=key):
                G=copy.deepcopy(self.f.G);G[key]=value
                self.refuses(lambda:validate_rules(self.f.B,G,self.f.binding_row['sha256'],self.f.go_row['sha256']))
        for key in self.f.B['envelope']:
            with self.subTest(envelope=key):
                B=copy.deepcopy(self.f.B);B['envelope'][key]+=1
                self.refuses(lambda:validate_rules(B,self.f.G,self.f.binding_row['sha256'],self.f.go_row['sha256']))

    def test_signature_failure_and_asset_mutation_refuse(self):
        from types import SimpleNamespace
        self.refuses(lambda:validate_release(self.f.A,signature_runner=lambda *a,**k:SimpleNamespace(returncode=1)))
        artifact=next(iter(self.f.B['signed_artifacts'].values()));Path(artifact['path']).write_bytes(b'changed bytes')
        self.refuses(lambda:validate_release(self.f.A,signature_runner=self.f.signature_spy))

    def test_file_alias_link_writable_and_digest_mutation_refuse(self):
        original=self.f.file('custody/file',b'bound bytes\n');bound_file(original)
        alias=self.f.root/'custody/alias';alias.symlink_to('file')
        self.refuses(lambda:measured_file(alias))
        link=self.f.root/'custody/hardlink';os.link(original['path'],link)
        self.refuses(lambda:bound_file(original));link.unlink()
        Path(original['path']).chmod(0o622);self.refuses(lambda:bound_file(original))
        Path(original['path']).chmod(0o600);Path(original['path']).write_bytes(b'other bytes\n')
        self.refuses(lambda:bound_file(original))

    def test_wrong_phase_missing_inputs_shell_and_forbidden_effect_refuse(self):
        role='launcher';spec={'phase':'PRE_ALLOC_CHECKED','argv':[self.f.B['verifier']['path']],
            'shell':False,'timeout_sec':30,'input_digests':{'source':'a'*64}}
        self.f.B['commands']={'PRE_ALLOC_CHECKED':{role:spec}};self.f.refresh();self.f.checks=self.f.measured_checks()
        S=ObservationStore(self.f.root/'observations',self.f.A,create=True)
        S.append('PRE_ALLOC_CHECKED',{'checks':self.f.checks,'admitted_command_inputs':{role:spec['input_digests']}})
        self.assertEqual(checked_command(self.f.A,S,role)[1],30)
        self.refuses(lambda:checked_command(self.f.A,S,'manager'))
        spec['shell']=True;self.refuses(lambda:checked_command(self.f.A,S,role));spec['shell']=False
        spec['argv'].append('--all');self.refuses(lambda:checked_command(self.f.A,S,role));spec['argv'].pop()
        spec['input_digests']={'wrong':'b'*64};self.refuses(lambda:checked_command(self.f.A,S,role))

    def test_actual_default_preflight_refuses_without_output_or_effect(self):
        B=ROOT/'templates/static-binding.template.json';G=ROOT/'templates/GO.template.md'
        checks=self.f.file('empty-checks.json',b'{}\n');observations=self.f.root/'must-not-exist'
        result=subprocess.run([sys.executable,str(ROOT/'run8.py'),'preflight','--binding',str(B),
            '--binding-sha256',hashlib.sha256(B.read_bytes()).hexdigest(),'--go',str(G),
            '--go-sha256',hashlib.sha256(G.read_bytes()).hexdigest(),'--checks',checks['path'],
            '--observations',str(observations)],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertIn('unbound_slot_source_revision',result.stderr)
        self.assertEqual(result.stdout,'')
        self.assertFalse(observations.exists())


if __name__=='__main__':unittest.main(verbosity=2)
