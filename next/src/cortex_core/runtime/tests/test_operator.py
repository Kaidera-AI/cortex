"""Actual encrypted client projection; no fixture grants installed credit."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cortex_core.runtime.bootstrap_config import decode_config
from cortex_core.runtime.custody import open_custody
from cortex_core.runtime.tests.test_bootstrap_config import sample
from cortex_core.runtime.tests.test_custody import intent,receipt,SECRET,KEY
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


if __name__=='__main__':unittest.main()
