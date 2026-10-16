"""Remaining original MAP(a,b) obligation: failed nested calls stay in lane evidence.
Only the existing R3/R4 owned fixtures and fake-engine seams are used.
"""
import base64,json,unittest
from pathlib import Path
from unittest.mock import patch
import test_r4_native_lane as r4
import test_r3_inspector as r3
WITNESSES={}

class Boundaries(unittest.TestCase):
    def exercise(self,fail=None,bad_reply=False):
        lane=r4.Lane(self)
        def reply(raw,request):
            if bad_reply:
                value=json.loads(raw);value['request_sha256']='0'*64
                return r3.codec.canonical_bytes(value)
            return raw
        fake=r3.FakeEngine(lane.fx,fail=fail,reply=reply)
        actual=[];call=r4.inspector._Children.call
        def observe(children,*args):
            try:return call(children,*args)
            finally:
                if not actual or actual[-1] is not children:actual.append(children)
        def engine(argv,environment,session,timeout,limit):
            # The real _Children collector receives actual fixture streams and copies
            # them before session cleanup. No real engine command can run here.
            code,out=fake(argv,environment,session,timeout,limit)
            files={}
            for key,raw in [('stdout',out),('stderr',b''),('journal',b'{"fixture":true}\n')]:
                p=Path(session)/('fixture-'+str(len(fake.calls))+'.'+key);p.write_bytes(raw);files[key]=p
            return code,out,files
        def inspect(*args,inspection,**kwargs):
            inspection.check(r3.rows(('a',b'x = 1\n')))
            return (),[]
        with patch.object(r4.product('artifacts'),'inspect_oci',inspect), \
             patch.object(r4.inspector,'_engine',engine), \
             patch.object(r4.inspector._Children,'call',observe):
            record=lane.run(r4.Engine(lane))
        self.assertTrue(actual,'the real nested collector must have been reached')
        nested=actual[0].records
        self.assertEqual([c['argv'][8] for c in nested],fake.calls)
        raw_files={str(p.relative_to(lane.evidence)):base64.b64encode(p.read_bytes()).decode()
                   for p in lane.evidence.rglob('*') if p.is_file()}
        key='timeout' if fail else 'bad-reply' if bad_reply else 'positive'
        WITNESSES[key]={'fake_engine_calls':fake.calls,'actual_nested_records':nested,
                        'lane_audit_children':record['phases']['audit']['children'],
                        'lane_result':record['result'],'classes':record['classes'],
                        'retained_evidence_bytes_b64':raw_files}
        return record,nested

    def test_positive_nested_history_is_published(self):
        record,nested=self.exercise()
        self.assertEqual([c['argv'][8] for c in nested],['load','image','run','rm','ps','unshare'])
        self.assertEqual(len(record['phases']['audit']['children']),1+len(nested))
        self.assertTrue(all(c['exit']==0 for c in nested))

    def test_MAP_nested_timeout_and_cleanup_history_must_be_published(self):
        record,nested=self.exercise(fail='run')
        self.assertEqual([c['argv'][8] for c in nested],['load','image','run','rm','ps','unshare'])
        self.assertIsNone(nested[2]['exit'])
        self.assertEqual(nested[2]['error'],'TimeoutExpired')
        self.assertEqual(record['result'],'RED')
        self.assertEqual(len(record['phases']['audit']['children']),1+len(nested),
                         'MAP(a,b): a failed actual nested attempt and its cleanup must not disappear')

    def test_MAP_bad_reply_does_not_erase_completed_nested_history(self):
        record,nested=self.exercise(bad_reply=True)
        self.assertEqual([c['argv'][8] for c in nested],['load','image','run','rm','ps','unshare'])
        self.assertTrue(all(c['exit']==0 for c in nested))
        self.assertEqual(record['result'],'RED')
        self.assertEqual(len(record['phases']['audit']['children']),1+len(nested),
                         'MAP(b): reply validation failure must retain actual completed children')
