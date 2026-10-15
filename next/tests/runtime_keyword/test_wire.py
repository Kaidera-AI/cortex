"""Closed keyword transport variant; ordinary dictionaries cannot set status."""
import copy
import unittest
from cortex_core.api.c11a import GatewayError, KeywordResponse

ID='00000000-0000-0000-0000-000000000001'


class KeywordWire(unittest.TestCase):
    def packet(self):
        return dict(query='match',results=[],degraded=['hosted_disabled'],reranked=False,
                    hall='project',room=None,graph=False,complete=False,state='partial',
                    unsupported=[dict(id=ID,reason='unknown_kind')],
                    freshness=dict(source='canonical',state='current',complete=False,scanned_records=1),
                    error=dict(code='capability_unavailable',capability='search',state='partial',reason='unsupported_text',retryable=False))

    def test_partial_is503_with_strict_closed_error_and_actual_canonical_freshness(self):
        base=self.packet();self.assertEqual(KeywordResponse(base).transport_status(),503)
        for path,value in ((('status',),200),(('error','retryable'),0),(('error','retryable'),True),
                (('freshness','complete'),True),(('freshness','model_identity'),'fake'),
                (('freshness','scanned_records'),True),(('unsupported',),[dict(id=ID,reason='secret-payload')])):
            with self.subTest(path=path,value=value):
                bad=copy.deepcopy(base);item=bad
                for name in path[:-1]:item=item[name]
                item[path[-1]]=value
                with self.assertRaises(GatewayError):KeywordResponse(bad).transport_status()

    def test_complete_hit_has_public_id_revision_score_and_no_semantic_fields(self):
        value=self.packet();value.pop('error');value.update(complete=True,state='ready',unsupported=[],
            results=[dict(id=ID,kind='memory',revision=1,score=1.0)])
        value['freshness']['complete']=True
        self.assertEqual(KeywordResponse(value).transport_status(),200)
        for changes in (dict(id='fake-id'),dict(revision=True),dict(score=float('nan')),dict(distance=.1)):
            with self.subTest(changes=changes):
                bad=copy.deepcopy(value);bad['results'][0].update(changes)
                with self.assertRaises(GatewayError):KeywordResponse(bad).transport_status()
