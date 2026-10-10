"""C02 OpenKai new-consumer fixtures; source-bound, not legacy acceptance."""
import copy
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT/'contracts/openkai-consumer-v1.json'
FIXTURES = ROOT/'contracts/fixtures/openkai'
EXPECTED_OBSERVED = {
    ('GET', '/beat/embeddings/backlog'), ('GET', '/degradation'),
    ('GET', '/health'), ('GET', '/projects/{project_key}'),
    ('GET', '/workers/health'), ('POST', '/artifacts'),
    ('POST', '/memory'), ('POST', '/search'), ('POST', '/sessions/ingest'),
}
EXPECTED_SDK_ONLY = {
    ('DELETE', '/skills/{slug}'), ('GET', '/events'),
    ('GET', '/sessions/ingested-ids'), ('GET', '/skills'),
    ('POST', '/skills'), ('POST', '/skills/{slug}/bind'),
}


class OpenKaiConsumer(unittest.TestCase):
    def packet(self):
        if not CONTRACT.is_file():
            self.fail('OpenKai C02 packet is absent')
        return json.loads(CONTRACT.read_text())

    def cases(self, name):
        path = FIXTURES/(name+'.json')
        if not path.is_file():
            self.fail(f'OpenKai {name} cases are absent')
        return json.loads(path.read_text())['cases']

    def validator(self):
        try:
            from cortex_core.openkai_contract import validate_packet, validate_case, ContractRefusal
        except ImportError:
            self.fail('OpenKai C02 contract runner is absent')
        return validate_packet, validate_case, ContractRefusal

    def test_observed_inventory_and_release_pin(self):
        packet = self.packet()
        validate_packet, _, refusal = self.validator()
        openapi = json.loads((ROOT/'contracts/openapi.json').read_text())
        self.assertEqual(validate_packet(packet, openapi), (9, 6))
        self.assertEqual({(r['method'], r['path']) for r in packet['observed']},
                         EXPECTED_OBSERVED)
        self.assertEqual({(r['method'], r['path']) for r in packet['sdk_only']},
                         EXPECTED_SDK_ONLY)
        self.assertEqual(packet['source']['commit'],
                         '83b9409989ca9ca7f6a08473523d6a171c63d44b')
        self.assertEqual(packet['upgrade_origin']['first_signed_candidate'],
                         'v0.1.003')
        self.assertEqual(packet['upgrade_origin']['v0.1.002'], 'not_signed_origin')
        for key, bad in (('commit', '0'*40), ('evidence_sha256', '0'*64)):
            altered = copy.deepcopy(packet)
            altered['source'][key] = bad
            with self.subTest(key=key), self.assertRaises(refusal):
                validate_packet(altered, openapi)
        altered = copy.deepcopy(packet)
        first = next(iter(altered['source']['files']))
        altered['source']['files'][first] = '0'*64
        with self.assertRaises(refusal):
            validate_packet(altered, openapi)

    def test_inventory_cannot_lose_or_promote_an_operation(self):
        packet = self.packet()
        validate_packet, _, refusal = self.validator()
        openapi = json.loads((ROOT/'contracts/openapi.json').read_text())
        missing = copy.deepcopy(packet)
        missing['observed'].pop()
        with self.assertRaises(refusal):
            validate_packet(missing, openapi)
        promoted = copy.deepcopy(packet)
        promoted['observed'].append(promoted['sdk_only'].pop())
        with self.assertRaises(refusal):
            validate_packet(promoted, openapi)
        wrong = copy.deepcopy(packet)
        wrong['observed'][0]['operation_id'] = 'C01-R999'
        with self.assertRaises(refusal):
            validate_packet(wrong, openapi)

    def test_each_observed_route_has_a_proposed_exchange_and_typed_error(self):
        _, validate_case, refusal = self.validator()
        cases = self.cases('route-exchanges')
        self.assertEqual({(c['method'], c['path']) for c in cases},
                         EXPECTED_OBSERVED)
        self.assertEqual(len(cases), 9)
        for case in cases:
            with self.subTest(route=case['id']):
                self.assertEqual(validate_case(case), case['id'])
        missing_error = copy.deepcopy(cases[0])
        del missing_error['error']
        with self.assertRaises(refusal):
            validate_case(missing_error)
        empty_code = copy.deepcopy(cases[0])
        empty_code['error']['body']['error']['code'] = ''
        with self.assertRaises(refusal):
            validate_case(empty_code)
        sdk_promoted = copy.deepcopy(cases[0])
        sdk_promoted['path'] = '/skills'
        with self.assertRaises(refusal):
            validate_case(sdk_promoted)

    def test_committed_write_read_retry_conflict_and_unknown_timeout(self):
        _, validate_case, refusal = self.validator()
        cases = self.cases('write-visibility')
        self.assertEqual({x['id'] for x in cases},
                         {'ack-read', 'idempotency', 'timeout-unknown'})
        for case in cases:
            with self.subTest(case=case['id']):
                self.assertEqual(validate_case(case), case['id'])
        bad = copy.deepcopy(next(x for x in cases if x['id'] == 'ack-read'))
        bad['read']['revision'] += 1
        with self.assertRaises(refusal):
            validate_case(bad)
        bad = copy.deepcopy(next(x for x in cases if x['id'] == 'ack-read'))
        bad['ack']['receipt']['durability'] = 'queued'
        with self.assertRaises(refusal):
            validate_case(bad)
        bad = copy.deepcopy(next(x for x in cases if x['id'] == 'idempotency'))
        bad['same_retry']['receipt']['record_id'] = '00000000-0000-4000-8000-000000000099'
        with self.assertRaises(refusal):
            validate_case(bad)
        bad = copy.deepcopy(next(x for x in cases if x['id'] == 'timeout-unknown'))
        bad['outcome'] = 'not_saved'
        with self.assertRaises(refusal):
            validate_case(bad)

    def test_search_states_never_turn_lag_into_zero_hits(self):
        _, validate_case, refusal = self.validator()
        cases = self.cases('search-states')
        self.assertEqual({x['state'] for x in cases},
                         {'ready_empty', 'pending', 'partial', 'unavailable'})
        for case in cases:
            self.assertEqual(validate_case(case), case['id'])
        pending = copy.deepcopy(next(x for x in cases if x['state'] == 'pending'))
        pending['results'] = []
        with self.assertRaises(refusal):
            validate_case(pending)
        partial = copy.deepcopy(next(x for x in cases if x['state'] == 'partial'))
        partial['complete'] = True
        with self.assertRaises(refusal):
            validate_case(partial)

    def test_openkai_alone_scope_off_mirror_and_stale_recall(self):
        _, validate_case, refusal = self.validator()
        cases = self.cases('alone-safety')
        self.assertEqual({x['id'] for x in cases},
                         {'wrong-project', 'revoked-writer', 'off',
                          'session-mirror', 'stale-recall'})
        for case in cases:
            self.assertEqual(validate_case(case), case['id'])
        off = copy.deepcopy(next(x for x in cases if x['id'] == 'off'))
        off['calls'] = ['POST /search']
        with self.assertRaises(refusal):
            validate_case(off)
        mirror = copy.deepcopy(next(x for x in cases if x['id'] == 'session-mirror'))
        mirror['request']['source_path'] = '/other/session'
        with self.assertRaises(refusal):
            validate_case(mirror)
        stale = copy.deepcopy(next(x for x in cases if x['id'] == 'stale-recall'))
        stale['decision'] = 'displayed'
        with self.assertRaises(refusal):
            validate_case(stale)


if __name__ == '__main__':
    unittest.main()
