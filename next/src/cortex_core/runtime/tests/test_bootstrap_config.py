"""Day-1 config admission: tests committed RED before implementation."""
import copy
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

try:
    from cortex_core.runtime.bootstrap_config import decode_config, read_config_file
    from cortex_core.runtime.credentials import resolve_credential
except ModuleNotFoundError:
    decode_config = read_config_file = resolve_credential = None


def sample(root):
    return dict(version=1, installation_id='10000000-0000-0000-0000-000000000001',
                tenant_id='10000000-0000-0000-0000-000000000002',
                project_id='10000000-0000-0000-0000-000000000003',
                project_names={'sample':'10000000-0000-0000-0000-000000000003'},
                ai_mode='off', vault_root=str(root),
                bind={'host':'0.0.0.0','port':8501},
                publish={'host':'127.0.0.1','port':8501},
                client={'credential_ref':'secrets/cortex/client'},
                database={'host':'core.internal','port':5432,'name':'cortex',
                          'tls':{'mode':'verify-full','root_cert_ref':'secrets/cortex/ca'}},
                roles={name:dict(login='cortex_'+name,secret_ref='secrets/cortex/'+name)
                       for name in ['bootstrap','migrator','api_request','probe','installer_enrollment']},
                limits={'request_seconds':30,'shutdown_seconds':20})


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.value=sample(self.root)
        self.path=self.root/'config.json'
        self.write(self.value)

    def write(self,value):
        self.path.write_text(json.dumps(value));self.path.chmod(0o600)

    def test_floor_complete(self):
        self.assertIsNotNone(read_config_file)
        config=read_config_file(self.path,mode='serve')
        self.assertEqual(config.installation_id,self.value['installation_id'])
        self.assertEqual(config.ai_mode,'off')
        self.assertEqual(set(config.roles),{'api_request','probe'})

    def test_config_duplicate_key(self):
        self.assertIsNotNone(decode_config)
        raw=json.dumps(self.value).encode().replace(b'"version": 1',b'"version": 1, "version": 1')
        with self.assertRaisesRegex(ValueError,'raw_shape'):
            decode_config(raw,mode='serve')

    def test_closed_shapes_and_explicit_ai(self):
        self.assertIsNotNone(decode_config)
        variants=[]
        for key in ['ai_mode','version']:
            value=copy.deepcopy(self.value);del value[key];variants.append(value)
        for field,val in [('ai_mode','hosted'),('version',True),('version',2),('provider','example')]:
            value=copy.deepcopy(self.value);value[field]=val;variants.append(value)
        value=copy.deepcopy(self.value);value['bind']['extra']=1;variants.append(value)
        value=copy.deepcopy(self.value);value['roles']['observer']=value['roles']['probe'];variants.append(value)
        for value in variants:
            with self.subTest(value=value),self.assertRaises(ValueError):
                decode_config(json.dumps(value).encode(),mode='serve')

    def test_installed_binding(self):
        self.assertIsNotNone(decode_config)
        for mutate in [lambda v:v.update(project_id='not-a-uuid'),
                       lambda v:v['project_names'].update(sample=v['tenant_id']),
                       lambda v:v['roles']['probe'].update(login=v['roles']['api_request']['login']),
                       lambda v:v['roles']['probe'].update(secret_ref=v['roles']['api_request']['secret_ref'])]:
            value=copy.deepcopy(self.value);mutate(value)
            with self.subTest(value=value),self.assertRaises(ValueError):
                decode_config(json.dumps(value).encode(),mode='serve')

    def test_public_listener_and_wrong_transport_refuse(self):
        self.assertIsNotNone(decode_config)
        for mutate in [lambda v:v['publish'].update(host='0.0.0.0'),
                       lambda v:v['bind'].update(port=True),
                       lambda v:v['database']['tls'].update(mode='disable'),
                       lambda v:v['database'].update(host='https://user:secret@db'),
                       lambda v:v['limits'].update(request_seconds=121)]:
            value=copy.deepcopy(self.value);mutate(value)
            with self.subTest(value=value),self.assertRaises(ValueError):
                decode_config(json.dumps(value).encode(),mode='serve')

    def test_config_read_is_private_bounded_and_symlink_free(self):
        self.assertIsNotNone(read_config_file)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):read_config_file(self.path,mode='serve')
        self.path.chmod(0o600)
        alias=self.root/'alias';alias.symlink_to(self.path)
        with self.assertRaises(ValueError):read_config_file(alias,mode='serve')
        self.path.write_bytes(b'x'*65537)
        with self.assertRaises(ValueError):read_config_file(self.path,mode='serve')

    def test_stdlib_only_host_decoder(self):
        self.assertIsNotNone(decode_config)
        src=Path(__file__).resolve().parents[3]
        code='import sys;sys.path.insert(0,sys.argv[1]);from cortex_core.runtime.bootstrap_config import read_config_file;assert read_config_file(sys.argv[2],mode="client").ai_mode=="off"'
        result=subprocess.run([sys.executable,'-S','-c',code,str(src),str(self.path)],capture_output=True,text=True,env={**os.environ,'PYTHONPATH':''})
        self.assertEqual(result.returncode,0,result.stderr)

    def test_mode_is_required_and_known(self):
        self.assertIsNotNone(decode_config)
        with self.assertRaises(ValueError):decode_config(json.dumps(self.value).encode(),mode='owner')

    def test_os_root_and_public_database_refuse(self):
        self.assertIsNotNone(decode_config)
        for mutate in [lambda v:v.update(vault_root='/'),
                       lambda v:v['database'].update(host='8.8.8.8'),
                       lambda v:v['database'].update(host='169.254.169.254')]:
            value=copy.deepcopy(self.value);mutate(value)
            with self.subTest(value=value),self.assertRaises(ValueError):
                decode_config(json.dumps(value).encode(),mode='serve')

    def test_numeric_database_aliases_refuse(self):
        for host in ['134744072','0xa9fea9fe','0251.0376.0251.0376',
                     '127.1','0X7F000001','2130706433']:
            value=copy.deepcopy(self.value);value['database']['host']=host
            with self.subTest(host=host),self.assertRaisesRegex(ValueError,'config_database'):
                decode_config(json.dumps(value).encode(),mode='serve')

    def test_double_leading_slash_vault_refuses(self):
        for root in ['//','//vault','//vault/private']:
            value=copy.deepcopy(self.value);value['vault_root']=root
            with self.subTest(root=root),self.assertRaisesRegex(ValueError,'config_vault'):
                decode_config(json.dumps(value).encode(),mode='serve')

    def test_private_ip_literals_and_dns_are_admitted(self):
        for host in ['::1','fc00::1','127.0.0.1','10.0.0.2','core.internal']:
            value=copy.deepcopy(self.value);value['database']['host']=host
            try:
                config=decode_config(json.dumps(value).encode(),mode='serve')
            except ValueError as error:
                self.fail('private host refused: '+host+': '+str(error))
            self.assertEqual(config.database['host'],host)
            self.assertEqual(config.database['tls']['mode'],'verify-full')


class CausalAdmissionTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('foundation_mutations',
            Path(__file__).with_name('run_foundation_mutations.py'))
        self.runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.runner)

    def result(self,*,phase='healthy',failures=None,errors=None,skipped=0,executed=True,
               source='source',test='test',tests_run=1):
        value=dict(tests_run=tests_run,failures=failures or [],errors=errors or [],skipped=skipped,
                   source_sha256=source,test_sha256=test,
                   executed_code_fingerprints={'body:1':'code'} if executed else {})
        return subprocess.CompletedProcess([],1 if phase=='mutant' else 0,
            'CORTEX_TEST_RESULT='+json.dumps(value)+'\n','')

    def test_causal_admission_requires_healthy_restored_execution_and_hashes(self):
        admit=getattr(self.runner,'admit_phase',None)
        self.assertIsNotNone(admit,'causal phases must have strict admission')
        for phase in ['healthy','restored']:
            self.assertTrue(admit(self.result(),{'case.body'},phase,'source','test'))
            for kwargs in [dict(skipped=1),dict(executed=False),dict(source='other'),
                           dict(test='other'),dict(tests_run=2)]:
                with self.subTest(phase=phase,kwargs=kwargs):
                    self.assertFalse(admit(self.result(**kwargs),{'case.body'},phase,'source','test'))

    def test_causal_admission_refuses_undeclared_failures_and_errors(self):
        admit=getattr(self.runner,'admit_phase',None)
        self.assertIsNotNone(admit,'causal phases must have strict admission')
        failure=dict(id='case.body',phase='test',is_assertion=True,traceback='AssertionError')
        result=self.result(phase='mutant',failures=[failure])
        self.assertTrue(admit(result,{'case.body'},'mutant','source','test'))
        for failures,errors in [([failure,{**failure,'id':'other.body'}],[]),
                                ([{**failure,'phase':'fixture'}],[]),
                                ([failure],[dict(id='case.body',traceback='RuntimeError')]),
                                ([],[])]:
            with self.subTest(failures=failures,errors=errors):
                self.assertFalse(admit(self.result(phase='mutant',failures=failures,errors=errors),
                                       {'case.body'},'mutant','source','test'))


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.value=sample(self.root)
        self.secrets=self.root/'secrets/cortex';self.secrets.mkdir(parents=True)
        for role in [*self.value['roles'],'client','ca']:
            path=self.secrets/role;path.write_bytes(b'opaque-private-material');path.chmod(0o600)

    def test_allowed_purpose_returns_exact_private_bytes(self):
        self.assertIsNotNone(resolve_credential)
        config=decode_config(json.dumps(self.value).encode(),mode='serve')
        self.assertEqual(resolve_credential(config,'api_request'),b'opaque-private-material')

    def test_purpose_confusion_before_any_file_read(self):
        self.assertIsNotNone(resolve_credential)
        config=decode_config(json.dumps(self.value).encode(),mode='serve')
        for purpose in ['bootstrap','migrator','installer_enrollment','client']:
            with self.subTest(purpose=purpose),self.assertRaisesRegex(ValueError,'purpose_not_admitted'):
                resolve_credential(config,purpose)

    def test_secret_symlink_and_hardlink_refuse(self):
        self.assertIsNotNone(resolve_credential)
        config=decode_config(json.dumps(self.value).encode(),mode='serve')
        path=self.secrets/'api_request';path.unlink();path.symlink_to(self.secrets/'probe')
        with self.assertRaises(ValueError):resolve_credential(config,'api_request')
        path.unlink();os.link(self.secrets/'probe',path)
        with self.assertRaises(ValueError):resolve_credential(config,'api_request')

    def test_parent_symlink_refuses(self):
        self.assertIsNotNone(resolve_credential)
        (self.root/'secrets').rename(self.root/'moved-secrets')
        (self.root/'secrets').symlink_to(self.root/'moved-secrets',target_is_directory=True)
        with self.assertRaises(ValueError):
            config=decode_config(json.dumps(self.value).encode(),mode='serve');resolve_credential(config,'api_request')

    def test_unsafe_or_missing_secret_has_closed_error(self):
        self.assertIsNotNone(resolve_credential)
        config=decode_config(json.dumps(self.value).encode(),mode='serve')
        path=self.secrets/'api_request'
        for data,mode in [(b'private-SENTINEL',0o644),(b'',0o600),(b'x'*4097,0o600)]:
            path.write_bytes(data);path.chmod(mode)
            with self.assertRaises(ValueError) as caught:resolve_credential(config,'api_request')
            self.assertNotIn('SENTINEL',str(caught.exception));self.assertNotIn(str(path),str(caught.exception))
        path.unlink()
        with self.assertRaises(ValueError):resolve_credential(config,'api_request')

    def test_client_has_no_database_purpose(self):
        self.assertIsNotNone(resolve_credential)
        config=decode_config(json.dumps(self.value).encode(),mode='client')
        self.assertEqual(dict(config.roles),{})
        self.assertEqual(resolve_credential(config,'client'),b'opaque-private-material')
        with self.assertRaises(ValueError):resolve_credential(config,'api_request')

    def test_config_immutability_keeps_purpose_policy(self):
        self.assertIsNotNone(decode_config)
        config=decode_config(json.dumps(self.value).encode(),mode='serve')
        with self.assertRaises((TypeError,AttributeError)):
            config.roles['bootstrap']=config.roles['api_request']

    def test_reconstructed_view_cannot_widen_mode_purposes(self):
        self.assertIsNotNone(resolve_credential)
        config=decode_config(json.dumps(self.value).encode(),mode='serve')
        config=replace(config,roles={'migrator':config.roles['api_request']})
        with self.assertRaisesRegex(ValueError,'purpose_not_admitted'):
            resolve_credential(config,'migrator')


if __name__=='__main__':unittest.main()
