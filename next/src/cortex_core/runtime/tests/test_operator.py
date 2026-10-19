"""Actual encrypted client projection; no fixture grants installed credit."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cortex_core.runtime.bootstrap_config import decode_config
from cortex_core.runtime.custody import open_custody
from cortex_core.runtime.tests.test_bootstrap_config import sample
from cortex_core.runtime.tests.test_custody import intent,receipt,SECRET,KEY
from cortex_core.runtime.tests.test_resources import DriverConnection
try:
    from cortex_core.runtime import operator
except ImportError:
    operator=None


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
        (self.root/'config').mkdir(mode=0o700);(self.root/'secrets').mkdir(mode=0o700)
        (self.root/'secrets/cortex').mkdir(mode=0o700)
        self.value=sample(self.root);self.path=self.root/'config/cortex.json';self.save()

    def save(self):self.path.write_text(json.dumps(self.value));self.path.chmod(0o600)

    def stage(self,*,committed=True):
        config=decode_config(json.dumps(self.value).encode(),mode='seed');r,w=os.pipe()
        try:
            os.write(w,KEY);os.close(w)
            with open_custody(config,unlock_fd=r,key_id='test-key') as v:
                with v.seed_record() as record:
                    value=intent(self.value);record.create(intent=value,credential=SECRET)
                    if committed:record.commit(receipt=receipt(value))
        finally:os.close(r)

    def invoke(self,extra=(),*,key=KEY,project=None,output_fd=None):
        self.assertIsNotNone(operator,'actual operator module is missing')
        r,w=os.pipe();kr,kw=os.pipe();os.write(kw,key);os.close(kw)
        out,err=io.StringIO(),io.StringIO()
        args=['config','client','--config',str(self.path),'--unlock-fd',str(kr),
              '--key-id','test-key','--projection-fd',str(w if output_fd is None else output_fd)]
        if project is not None:args+=['--project',project]
        args+=list(extra)
        try:
            with redirect_stdout(out),redirect_stderr(err):code=operator.main(args)
            if output_fd is not None:
                os.close(w);w=None
            os.set_blocking(r,False);raw=b'';eof=False
            while True:
                try:chunk=os.read(r,4096)
                except BlockingIOError:break
                if not chunk:eof=True;break
                raw+=chunk
            return code,raw,eof,out.getvalue(),err.getvalue()
        finally:
            os.close(kr);os.close(r)
            if w is not None:
                try:os.close(w)
                except OSError:pass

    def refusal(self,result,reason):
        code,raw,eof,out,err=result
        self.assertNotEqual(code,0);self.assertEqual(raw,b'');self.assertEqual(out,'')
        self.assertEqual(len(err.splitlines()),1);packet=json.loads(err)
        self.assertFalse(packet['ok']);self.assertEqual(packet['error']['reason'],reason)
        self.assertNotIn(SECRET.decode(),err)

    def test_committed_seven_fields_exact_eof_and_no_public_secret_output(self):
        self.assertIsNotNone(operator);self.stage();code,raw,eof,out,err=self.invoke()
        self.assertEqual(code,0,err);self.assertTrue(eof);self.assertEqual(out+err,'')
        self.assertEqual(raw.split(b'\x00'),[b'cortex.client.v1',b'http://127.0.0.1:8501',
            self.value['installation_id'].encode(),self.value['project_id'].encode(),b'sample',SECRET,b'10',b''])

    def test_explicit_project_selection_comes_only_from_config_map(self):
        self.assertIsNotNone(operator);other='20000000-0000-0000-0000-000000000003'
        self.value['project_names']['other']=other;self.save();self.stage()
        code,raw,eof,out,err=self.invoke(project='other')
        self.assertEqual(code,0,err);self.assertTrue(eof);fields=raw.split(b'\x00')
        self.assertEqual(fields[3:5],[other.encode(),b'other'])
        self.refusal(self.invoke(project='missing'),'config_projects')

    def test_ipv6_loopback_url_and_configured_shorter_total_budget(self):
        self.assertIsNotNone(operator);self.value['publish']['host']='::1';self.value['limits']['request_seconds']=3
        self.save();self.stage();code,raw,eof,out,err=self.invoke()
        self.assertEqual(code,0,err);self.assertEqual(raw.split(b'\x00')[1],b'http://[::1]:8501')
        self.assertEqual(raw.split(b'\x00')[-2],b'3')

    def test_missing_or_uncommitted_state_has_no_projection_prefix(self):
        self.assertIsNotNone(operator);self.refusal(self.invoke(),'custody_missing')
        self.stage(committed=False);self.refusal(self.invoke(),'custody_uncommitted')

    def test_wrong_key_and_tamper_fail_without_secret_or_ciphertext_details(self):
        self.assertIsNotNone(operator);self.stage();self.refusal(self.invoke(key=b'x'*32),'custody_invalid')
        path=self.root/'secrets/cortex/client';path.write_bytes(b'not-a-ciphertext')
        self.refusal(self.invoke(),'custody_invalid')

    def test_output_must_be_writable_owned_anonymous_pipe(self):
        self.assertIsNotNone(operator);self.stage()
        path=self.root/'public-output';fd=os.open(path,os.O_WRONLY|os.O_CREAT,0o600)
        try:self.refusal(self.invoke(output_fd=fd),'operator_projection')
        finally:os.close(fd)
        self.assertEqual(path.read_bytes(),b'')
        for fd in (0,1,2,-1):self.refusal(self.invoke(output_fd=fd),'operator_projection')
        r,w=os.pipe()
        try:self.refusal(self.invoke(output_fd=r),'operator_projection')
        finally:os.close(r);os.close(w)

    def test_named_fifo_is_not_a_private_output_pipe(self):
        self.assertIsNotNone(operator);self.stage();fifo=self.root/'fifo';os.mkfifo(fifo,0o600)
        r=os.open(fifo,os.O_RDONLY|os.O_NONBLOCK);w=os.open(fifo,os.O_WRONLY|os.O_NONBLOCK)
        try:self.refusal(self.invoke(output_fd=w),'operator_projection')
        finally:os.close(r);os.close(w)

    def test_unsafe_or_missing_config_refuses_before_unlock_or_projection_bytes(self):
        self.assertIsNotNone(operator);self.path.chmod(0o644)
        with patch.object(operator,'open_custody',side_effect=AssertionError('unlock reached')):
            self.refusal(self.invoke(),'private_file_required')
        self.path.unlink()
        with patch.object(operator,'open_custody',side_effect=AssertionError('unlock reached')):
            self.refusal(self.invoke(),'private_file_required')

    def test_unknown_project_refuses_before_unlock(self):
        self.assertIsNotNone(operator)
        with patch.object(operator,'open_custody',side_effect=AssertionError('unlock reached')):
            self.refusal(self.invoke(project='unknown'),'config_projects')

    def test_duplicate_or_unknown_cli_arguments_refuse_without_echo(self):
        self.assertIsNotNone(operator)
        for extra in (['--config',str(self.path)],['--key-id','duplicate'],['--unknown','public-test-secret']):
            result=self.invoke(extra);self.refusal(result,'operator_arguments');self.assertNotIn('public-test-secret',result[-1])

    def test_backend_exception_becomes_one_sanitized_failure(self):
        self.assertIsNotNone(operator)
        with patch.object(operator,'open_custody',side_effect=RuntimeError('public-test-secret raw error')):
            result=self.invoke();self.refusal(result,'operator_unavailable');self.assertNotIn('public-test-secret',result[-1])

    def test_projection_never_uses_environment_or_opens_network(self):
        self.assertIsNotNone(operator);self.stage()
        with patch.dict(os.environ,{'CORTEX_API_URL':'http://public.invalid:1','CORTEX_PROJECT':'unadmitted',
                                    'CORTEX_API_KEY':'public-test-secret'}),patch('socket.socket',side_effect=AssertionError('network reached')):
            code,raw,eof,out,err=self.invoke()
        self.assertEqual(code,0,err);self.assertTrue(eof);self.assertNotIn(b'public.invalid',raw)


class MigrationOperatorTests(unittest.TestCase):
    setUp=OperatorTests.setUp
    save=OperatorTests.save

    def invoke_migration(self):
        kr,kw=os.pipe();os.write(kw,KEY);os.close(kw)
        out,err=io.StringIO(),io.StringIO()
        try:
            with redirect_stdout(out),redirect_stderr(err):
                code=operator.main(['migrate','--config',str(self.path),'--unlock-fd',str(kr),'--key-id','test-key'])
            return code,out.getvalue(),err.getvalue()
        finally:os.close(kr)

    def stage_role(self):
        config=decode_config(json.dumps(self.value).encode(),mode='migrate');r,w=os.pipe()
        try:
            os.write(w,KEY);os.close(w)
            with open_custody(config,unlock_fd=r,key_id='test-key') as v:v.import_credential('migrator',b'public-test-password')
        finally:os.close(r)
        p=self.root/'secrets/cortex/ca';p.write_bytes(b'public-test-certificate');p.chmod(0o600)

    def test_actual_dispatch_uses_one_config_one_finite_login_and_existing_engine(self):
        self.assertIsNotNone(operator);self.stage_role();events=[];calls=[]
        def driver(**kwargs):calls.append(kwargs);return DriverConnection(kwargs['user'],events)
        with patch('cortex_core.runtime.resources._driver_connect',side_effect=driver),patch(
                'cortex_core.schema.migrate.apply_forward_migrations',return_value=['001','002']) as engine:
            code,out,err=self.invoke_migration()
        self.assertEqual(code,0,err);self.assertEqual(err,'');self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['user'],'cortex_migrator');self.assertEqual(engine.call_count,1)
        self.assertEqual(events,['close:cortex_migrator'])
        self.assertEqual(json.loads(out),{'schema':'cortex.operator.v1','ok':True,'operation':'migrate',
            'installation_id':self.value['installation_id'],'applied':['001','002']})
        self.assertNotIn('public-test-password',out)

    def test_engine_error_returns_one_typed_line_no_success_or_driver_text(self):
        self.assertIsNotNone(operator);self.stage_role();events=[]
        with patch('cortex_core.runtime.resources._driver_connect',return_value=DriverConnection('migrator',events)),patch(
                'cortex_core.schema.migrate.apply_forward_migrations',side_effect=RuntimeError('public-test-secret')):
            code,out,err=self.invoke_migration()
        self.assertEqual(code,2);self.assertEqual(out,'');self.assertEqual(len(err.splitlines()),1)
        self.assertEqual(json.loads(err)['error']['reason'],'schema_migration')
        self.assertNotIn('public-test-secret',err);self.assertEqual(events,['close:migrator'])

    def test_config_refusal_precedes_migration_unlock_and_sql(self):
        self.assertIsNotNone(operator);self.path.chmod(0o644)
        with patch.object(operator,'open_custody',side_effect=AssertionError('unlock reached')):
            code,out,err=self.invoke_migration()
        self.assertEqual(code,2);self.assertEqual(out,'')
        self.assertEqual(json.loads(err)['error']['reason'],'private_file_required')


class SeedOperatorTests(unittest.TestCase):
    setUp=OperatorTests.setUp
    save=OperatorTests.save

    def invoke_seed(self,key='test-operation'):
        kr,kw=os.pipe();os.write(kw,KEY);os.close(kw)
        out,err=io.StringIO(),io.StringIO()
        try:
            with redirect_stdout(out),redirect_stderr(err):
                code=operator.main(['seed','--config',str(self.path),'--unlock-fd',str(kr),
                    '--key-id','test-key','--operation-key',key])
            return code,out.getvalue(),err.getvalue()
        finally:os.close(kr)

    def stage_roles(self):
        config=decode_config(json.dumps(self.value).encode(),mode='seed');r,w=os.pipe()
        try:
            os.write(w,KEY);os.close(w)
            with open_custody(config,unlock_fd=r,key_id='test-key') as vault:
                for purpose in config.roles:vault.import_credential(purpose,b'public-test-password')
        finally:os.close(r)
        p=self.root/'secrets/cortex/ca';p.write_bytes(b'public-test-certificate');p.chmod(0o600)

    def seed(self,config,key,deadline,*,commit=True,extra=False):
        from cortex_core.runtime.resources import current_seed_resources
        owned=current_seed_resources(config,deadline);self.assertIs(owned._config,config)
        self.assertEqual(config.mode,'seed');self.assertGreater(deadline,time.monotonic())
        self.assertLessEqual(deadline-time.monotonic(),config.limits['request_seconds'])
        self.assertEqual(set(owned.connections),{'bootstrap','installer_enrollment','api_request'})
        value=intent(self.value);value['operation_key']=key;ack=receipt(value)
        with owned.custody.seed_record() as record:
            record.create(intent=value,credential=SECRET)
            if commit:record.commit(receipt=ack)
        return {**ack,'secret':SECRET.decode()} if extra else ack

    def test_fixed_seed_call_same_config_context_then_closed_committed_public_receipt(self):
        self.assertIsNotNone(operator);self.stage_roles();events=[];calls=[]
        def driver(**kwargs):calls.append(kwargs);return DriverConnection(kwargs['user'],events)
        with patch('cortex_core.runtime.resources._driver_connect',side_effect=driver),patch.dict('sys.modules',
                {'cortex_core.schema.seed':SimpleNamespace(seed_runtime_bindings=self.seed)}):
            code,out,err=self.invoke_seed()
        self.assertEqual(code,0,err);self.assertEqual(err,'');packet=json.loads(out)
        self.assertEqual(packet,{'schema':'cortex.operator.v1','ok':True,'operation':'seed',
            'installation_id':self.value['installation_id'],'receipt':receipt(intent(self.value))})
        self.assertNotIn(SECRET.decode(),out)
        self.assertEqual([c['user'] for c in calls],['cortex_bootstrap','cortex_installer_enrollment','cortex_api_request'])
        self.assertEqual(events,['close:cortex_api_request','close:cortex_installer_enrollment','close:cortex_bootstrap'])

    def test_returned_receipt_without_durable_commit_is_never_success(self):
        self.assertIsNotNone(operator);self.stage_roles();events=[]
        with patch('cortex_core.runtime.resources._driver_connect',side_effect=lambda **kw:DriverConnection(kw['user'],events)),patch.dict('sys.modules',
                {'cortex_core.schema.seed':SimpleNamespace(seed_runtime_bindings=lambda *args:self.seed(*args,commit=False))}):
            code,out,err=self.invoke_seed()
        self.assertEqual(code,2);self.assertEqual(out,'');self.assertEqual(len(err.splitlines()),1)
        self.assertEqual(json.loads(err)['error']['reason'],'seed_receipt')

    def test_extra_result_field_never_exposes_secret_even_after_commit(self):
        self.assertIsNotNone(operator);self.stage_roles();events=[]
        with patch('cortex_core.runtime.resources._driver_connect',side_effect=lambda **kw:DriverConnection(kw['user'],events)),patch.dict('sys.modules',
                {'cortex_core.schema.seed':SimpleNamespace(seed_runtime_bindings=lambda *args:self.seed(*args,extra=True))}):
            code,out,err=self.invoke_seed()
        self.assertEqual(code,2);self.assertEqual(out,'');self.assertNotIn(SECRET.decode(),err)
        self.assertEqual(json.loads(err)['error']['reason'],'seed_receipt')

    def test_cleanup_failure_after_commit_never_publishes_success(self):
        self.assertIsNotNone(operator);self.stage_roles();events=[]
        with patch('cortex_core.runtime.resources._driver_connect',side_effect=lambda **kw:DriverConnection(kw['user'],events,True)),patch.dict('sys.modules',
                {'cortex_core.schema.seed':SimpleNamespace(seed_runtime_bindings=self.seed)}):
            code,out,err=self.invoke_seed()
        self.assertEqual(code,2);self.assertEqual(out,'');self.assertEqual(len(events),3)
        self.assertEqual(json.loads(err)['error']['reason'],'resources_cleanup')

    def test_invalid_operation_key_refuses_before_unlock_or_sql(self):
        self.assertIsNotNone(operator)
        for key in ('x'*257,'line\nkey','\x00key'):
            with patch.object(operator,'open_custody',side_effect=AssertionError('unlock reached')):
                code,out,err=self.invoke_seed(key)
            self.assertEqual(code,2);self.assertEqual(out,'')
            self.assertEqual(json.loads(err)['error']['reason'],'seed_operation_key')


if __name__=='__main__':unittest.main()
