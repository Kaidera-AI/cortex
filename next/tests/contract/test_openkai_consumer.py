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
        self.assertEqual(packet.get('ruled_contract_deltas'), {
            'D1': {'method':'GET', 'path':'/records/{id}',
                   'implementation_state':'ruled_unimplemented',
                   'not_found':404, 'unauthorized':404, 'tombstone':410,
                   'body_fields':['revision','payload_sha256']},
            'D2': {'method':'POST', 'path':'/search', 'placement':'parameters',
                   'implementation_state':'ruled_unimplemented',
                   'min_revision':'non_negative_integer',
                   'wait_ms':{'default':0,'maximum':10000},
                   'deadline':{'status':503,'code':'capability_unavailable',
                               'state':'pending'}},
            'D3': {'status_scope':'read', 'status_binding':'bound_now',
                   'status_paths':['/beat/embeddings/backlog','/degradation',
                                   '/workers/health'],
                   'control_jobs_state':'ruled_unimplemented',
                   'future_control_scope':'owner_admin',
                   'gtm_response':{'status':503,'code':'capability_unavailable',
                                   'state':'unimplemented'}},
            'D4': {'implementation_state':'ruled_unimplemented',
                   'lag_partial':{'status':503,'code':'capability_unavailable',
                                  'results':'omitted'},
                   'ready_empty':{'status':200,'state':'ready_empty','results':[]}},
        })
        altered = copy.deepcopy(packet)
        altered['ruled_contract_deltas']['D2']['wait_ms']['maximum'] = 10001
        with self.assertRaises(refusal):
            validate_packet(altered, openapi)
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
        status_paths = {'/beat/embeddings/backlog', '/degradation', '/workers/health'}
        for case in cases:
            if case['path'] in status_paths:
                self.assertEqual((case.get('binding'), case.get('authorization')),
                                 ('ruled_status_read', 'read'))
        search = next(case for case in cases if case['path'] == '/search')
        self.assertEqual(search['request']['body'].get('min_revision'), 2)
        self.assertEqual(search['request']['body'].get('wait_ms'), 10000)
        self.assertEqual(search['error']['body']['error'].get('state'), 'pending')
        self.assertEqual(search['success']['body'].get('state'), 'ready_empty')
        unbounded_wait = copy.deepcopy(search)
        unbounded_wait['request']['body']['wait_ms'] = 10001
        with self.assertRaises(refusal):
            validate_case(unbounded_wait)
        false_pending = copy.deepcopy(search)
        false_pending['error']['body']['error']['state'] = 'ready_empty'
        with self.assertRaises(refusal):
            validate_case(false_pending)
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
        read = next(x for x in cases if x['id'] == 'ack-read')['read']
        self.assertEqual((read.get('binding'), read.get('method'), read.get('path')),
                         ('ruled_unimplemented', 'GET', '/records/{id}'))
        self.assertEqual(read.get('payload_sha256'), 'a'*64)
        self.assertEqual(read.get('not_found_status'), 404)
        self.assertEqual(read.get('unauthorized_status'), 404)
        self.assertEqual(read.get('tombstone_status'), 410)
        no_leak = copy.deepcopy(next(x for x in cases if x['id'] == 'ack-read'))
        no_leak['read']['unauthorized_status'] = 403
        with self.assertRaises(refusal):
            validate_case(no_leak)
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
        partial_wire = next(x for x in cases if x['state'] == 'partial')
        self.assertEqual((partial_wire['status'], partial_wire.get('wire_state'),
                          partial_wire['error']['code']),
                         (503, 'ruled_unimplemented', 'capability_unavailable'))
        self.assertNotIn('results', partial_wire)
        partial_success = copy.deepcopy(partial_wire)
        partial_success['status'] = 206
        with self.assertRaises(refusal):
            validate_case(partial_success)
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
                          'session-mirror', 'stale-recall',
                          'control-unbound', 'job-unbound'})
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
        control = copy.deepcopy(next(x for x in cases if x['id'] == 'control-unbound'))
        control['binding'] = 'ruled_bound'
        with self.assertRaises(refusal):
            validate_case(control)


if __name__ == '__main__':
    unittest.main()
