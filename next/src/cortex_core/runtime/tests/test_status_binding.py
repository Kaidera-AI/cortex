"""Configured status uses stock HTTP transport; no custody or SQL authority."""
import io
import json
import time
import unittest
from unittest.mock import patch

import httpx
from cortex_core.runtime.bootstrap_config import decode_config
from cortex_core.runtime.tests import test_resources as fixtures
try:
    from cortex_core.runtime import status
except ImportError:
    status=None


class StatusBindingTests(unittest.TestCase):
    setUp=fixtures.ResourceTests.setUp

    def require_source(self):
        self.assertTrue(status is not None and hasattr(status,'read_status'),
            'actual configured Option-A status reader is missing')
        self.config=decode_config(json.dumps(self.value).encode(),mode='status')

    def health(self,readiness='ready'):
        return dict(component='cortex',status='unavailable' if readiness=='unavailable' else 'ok',
            readiness=readiness,installation_id=self.config.installation_id)

    def invoke(self,handler):
        calls=[];client=httpx.Client
        def create(**kw):calls.append(kw);return client(transport=httpx.MockTransport(handler),**kw)
        out=io.StringIO()
        with patch.object(status.httpx,'Client',side_effect=create),patch('sys.stdout',out):
            result=status.read_status(self.config)
        return result,json.loads(out.getvalue()),calls

    def test_one_fixed_configured_loopback_get_has_no_auth_proxy_or_env_defaults(self):
        self.require_source();requests=[]
        def handler(request):requests.append(request);return httpx.Response(200,json=self.health())
        code,packet,calls=self.invoke(handler)
        self.assertEqual(code,0);self.assertEqual(packet,dict(schema='cortex.status.v1',health=self.health(),client_error=None))
        self.assertEqual(len(requests),1);self.assertEqual(requests[0].method,'GET')
        self.assertEqual(str(requests[0].url),'http://127.0.0.1:8501/health')
        self.assertNotIn('authorization',requests[0].headers)
        self.assertFalse(calls[0]['trust_env']);self.assertFalse(calls[0]['follow_redirects'])

    def test_real_enrollment_and_unavailable_states_are_not_fabricated_ready(self):
        self.require_source()
        for readiness,expected,http_status in [('enrollment_required',1,200),('unavailable',2,503)]:
            code,packet,calls=self.invoke(lambda request:httpx.Response(http_status,json=self.health(readiness)))
            self.assertEqual(code,expected);self.assertEqual(packet['health']['readiness'],readiness)
            self.assertIsNone(packet['client_error'])

    def test_wrong_install_duplicate_extra_and_inconsistent_state_refuse(self):
        self.require_source()
        bad=[{**self.health(),'installation_id':'10000000-0000-0000-0000-000000000099'},
            {**self.health(),'extra':True},{**self.health(),'status':'unavailable'},
            {**self.health(),'readiness':'invented'}]
        for body in bad:
            code,packet,calls=self.invoke(lambda request:httpx.Response(200,json=body))
            self.assertEqual((code,packet['health'],packet['client_error']),(2,None,'invalid_response'))
        raw=json.dumps(self.health())[:-1]+',"status":"ok"}'
        code,packet,calls=self.invoke(lambda request:httpx.Response(200,content=raw,headers={'content-type':'application/json'}))
        self.assertEqual((code,packet['health'],packet['client_error']),(2,None,'invalid_response'))

    def test_redirect_compression_and_oversized_body_are_not_accepted_or_retried(self):
        self.require_source()
        for response in [httpx.Response(302,headers={'location':'http://192.0.2.1/private'}),
                         httpx.Response(200,headers={'content-type':'application/json','content-encoding':'br'},stream=httpx.ByteStream(b'{}')),
                         httpx.Response(200,headers={'content-type':'application/json'},content=b' '*20000)]:
            seen=[]
            def handler(request):seen.append(request);return response
            code,packet,calls=self.invoke(handler)
            self.assertEqual(code,2);self.assertIsNone(packet['health']);self.assertEqual(len(seen),1)

    def test_original_deadline_bounds_synchronous_stream_and_hides_transport_details(self):
        self.require_source()
        class Slow(httpx.SyncByteStream):
            def __iter__(self):time.sleep(1);yield b'{}'
        with patch.object(status,'BUDGET_SECONDS',0.1):
            started=time.monotonic()
            code,packet,calls=self.invoke(lambda request:httpx.Response(200,stream=Slow(),headers={'content-type':'application/json'}))
            self.assertEqual((code,packet['client_error']),(2,'timeout'))
            self.assertLess(time.monotonic()-started,0.7)
        def broken(request):raise RuntimeError('private-password')
        code,packet,calls=self.invoke(broken)
        self.assertEqual((code,packet['client_error']),(2,'connection_unavailable'))
        self.assertNotIn('private-password',json.dumps(packet))

    def test_operator_status_uses_same_config_without_unlock_or_custody(self):
        self.require_source()
        from cortex_core.runtime import operator
        path=self.root/'config.json';path.write_text(json.dumps(self.value));path.chmod(0o600)
        calls=[]
        def read(config):calls.append(config);return 1
        with patch.object(status,'read_status',side_effect=read), \
             patch.object(operator,'open_custody',side_effect=AssertionError('status unlocked custody')):
            self.assertEqual(operator.dispatch_operator_command(['status','--config',str(path)]),1)
        self.assertEqual(len(calls),1);self.assertEqual(calls[0].mode,'status')

    def test_operator_status_main_exposes_real_diagnostic_exit_without_custody(self):
        self.require_source()
        from cortex_core.runtime import operator
        path=self.root/'config.json';path.write_text(json.dumps(self.value));path.chmod(0o600)
        out,err=io.StringIO(),io.StringIO();seen=[]
        def read(config):seen.append(config.mode);return 1
        with patch.object(status,'read_status',side_effect=read),patch('sys.stdout',out),patch('sys.stderr',err), \
             patch.object(operator,'open_custody',side_effect=AssertionError('status unlocked custody')):
            code=operator.main(['status','--config',str(path)])
        self.assertEqual(code,1,err.getvalue());self.assertEqual(seen,['status'])
        self.assertEqual((out.getvalue(),err.getvalue()),('',''))


if __name__=='__main__':unittest.main()
