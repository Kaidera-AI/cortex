"""Current P1 fixture closure and strict historical/mixed E2 tuple refusals.

Uses the existing nonauthoritative member double and real admission. Neither a
fixture revision nor a passing test grants install, effect or native authority.
"""
import copy
import json
from pathlib import Path
import unittest

from test_contracts import NEXT, SUBJECTS, encode
from cortex_core.install.contracts import measure_original_ref, validate_journal
from cortex_core.install.receipt import verify_product_receipt
from cortex_core.release.codec import sha256
from cortex_core.runtime import config, contracts, signed_member
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble


FIXTURES = Path(__file__).parent / 'fixtures'
REVISION = 'pgc20-runtime-tuple-v1'
HISTORICAL_SHA256 = '25962b3efc0f0d43cfdb706707af46e0464dfe59ffe00ac2ceba8cf16e00ca58'
HISTORICAL = json.loads((FIXTURES / 'p1-subjects-pre-e2.json').read_bytes())
HISTORICAL_E2 = json.loads((FIXTURES / 'p1-subjects.json').read_bytes())
ORIGINAL_FIXTURE_SHA256 = {'gate-b-linux.json': 'b4b7d8531398bdbd5df9ef45f92288fed1e7891fb11c21a97d2a613c0664377b', 'gate-b-macos.json': '1bb831297364e92f7b2cfae69dfc0d0c9f4319c10f27033ef06fadec4ba84924', 'gate-b-subjects.json': '5046c85bda82973bbf0e9b970ebdbff636e2dfdf07186acbea8eb0f88c655063', 'owner-proposal-subjects.json': 'b2f4aee17b91eff5c57b16a4096484944403f33e782b31e8b59fba5b216b73ed', 'p1-subjects-pre-e2.json': '25962b3efc0f0d43cfdb706707af46e0464dfe59ffe00ac2ceba8cf16e00ca58', 'p1-subjects.json': '698303043bdaf3550e543b096dcd5fa75fb36e32c9eca5b742ebd3075e263883', 'p2a-subjects.json': 'ec42de009bbc4b2829a7999117fe4985b5a9fc6343876695331310c8ce2166e6', 'p2b-subjects.json': '5c185e5880370514c1ca6676673812dc466cf2e6cbf85ca2e4f78c8387351f42', 'subjects.json': 'ea26da33aee24c4c1d048194b49736f315c8cebf888064ed88345525e1d1aa9d'}
MEMBERS = (
    ('lock_schema', 'runtime-lock-v1.schema.json'),
    ('config_schema', 'runtime-config-v1.schema.json'),
    ('operations', 'runtime-operations-v1.json'),
)


def references(value):
    if isinstance(value, dict):
        if {'path', 'bytes', 'sha256'} <= value.keys():
            yield value
        for child in value.values():
            yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


def wire(value):
    return (json.dumps(value, indent=2, ensure_ascii=True) + '\n').encode()


class TupleRevisionOracle(unittest.TestCase):
    def admit(self, lock_raw, cfg, target):
        cfg = copy.deepcopy(cfg)
        cfg['runtime_lock_sha256'] = sha256(lock_raw)
        double = NonAuthoritativeSignedMemberDouble(lock_raw)
        with double.installed(signed_member):
            member = signed_member.request_member('test-only-aggregate', 'runtime.lock.json')
            return config.admit_config(member, wire(cfg), aggregate_id='test-only-aggregate',
                                       observed_target=target, mode='inspect')

    def refuse(self, lock_raw, cfg, target, reason):
        with self.assertRaises(signed_member.Refusal) as caught:
            self.admit(lock_raw, cfg, target)
        self.assertEqual(caught.exception.reason, reason)

    def test_current_lock_configs_and_three_descriptors_match_fixed_package_bytes(self):
        self.assertEqual(SUBJECTS.get('fixture_revision'), REVISION)
        lock_raw = SUBJECTS['members']['runtime.lock.json'].encode()
        lock = json.loads(lock_raw)
        package = contracts.load_contracts()
        self.assertEqual(lock['operations_sha256'], package.operations_sha256)
        for key, name in MEMBERS:
            raw = (NEXT / 'contracts/runtime' / name).read_bytes()
            self.assertEqual(sha256(raw), contracts.RC0_SHA256[name])
            descriptor = lock['contract_members'][key]['value']
            self.assertEqual((descriptor['sha256'], descriptor['bytes']), (sha256(raw), len(raw)))
        for target, raw in SUBJECTS['configs'].items():
            with self.subTest(target=target):
                cfg = json.loads(raw)
                self.assertEqual(cfg['operations_sha256'], package.operations_sha256)
                self.assertEqual(cfg['runtime_lock_sha256'], sha256(lock_raw))

    def test_every_member_and_original_request_receipt_journal_reference_is_closed(self):
        members = {name: raw.encode() for name, raw in SUBJECTS['members'].items()}
        original = encode(SUBJECTS['requests']['linux/arm64/manual'])
        lock_refs = request_refs = 0
        for ref in references(SUBJECTS):
            if ref['path'] in members:
                measure_original_ref(ref, members[ref['path']])
                lock_refs += ref['path'] == 'runtime.lock.json'
            elif ref['path'] == 'request.json':
                measure_original_ref(ref, original)
                request_refs += 1
        self.assertEqual(lock_refs, 9)
        self.assertEqual(request_refs, 3)
        verify_product_receipt(encode(SUBJECTS['receipt']), original,
                               request_ref=SUBJECTS['receipt']['request_ref'])
        self.assertEqual(len(validate_journal([encode(SUBJECTS['journal'])], original)), 1)

    def test_historical_fixture_bytes_remain_exact_and_nonauthoritative(self):
        self.assertEqual(sha256((FIXTURES / 'p1-subjects-pre-e2.json').read_bytes()), HISTORICAL_SHA256)
        self.assertEqual(SUBJECTS['scope'], HISTORICAL['scope'])
        for key in ('real_build_A', 'real_build_A_ref', 'real_build_A_output_key', 'real_build_A_output'):
            self.assertEqual(SUBJECTS[key], HISTORICAL[key])

    def test_fixture_index_binds_current_revision_and_original_history(self):
        index = json.loads((FIXTURES / 'subjects-pgc20-v1.json').read_bytes())
        self.assertEqual(index['P1'].get('revision'), REVISION)
        measure_original_ref({k: index['P1'][k] for k in ('path', 'bytes', 'sha256')},
                             (FIXTURES / index['P1']['path']).read_bytes())
        self.assertIn('historical_P1', index)
        measure_original_ref(index['historical_P1'], (FIXTURES / index['historical_P1']['path']).read_bytes())
        self.assertEqual(index['historical_P1']['sha256'], HISTORICAL_SHA256)

    def test_exact_current_tuple_admits_both_targets_without_effect_authority(self):
        lock_raw = SUBJECTS['members']['runtime.lock.json'].encode()
        for target, raw in SUBJECTS['configs'].items():
            with self.subTest(target=target):
                # Do not repair the supplied positive config's binding in the test.
                cfg = json.loads(raw)
                self.assertEqual(cfg['runtime_lock_sha256'], sha256(lock_raw))
                try:
                    plan = self.admit(lock_raw, cfg, target)
                except signed_member.Refusal as refusal:
                    self.fail('current tuple refused: ' + refusal.reason)
                self.assertEqual((plan.target, plan.mode), (target, 'inspect'))
                self.assertEqual(plan.lock_sha256, sha256(lock_raw))
                self.assertEqual(plan.authority_status, 'UNBOUND')
                self.assertFalse(plan.effect_authority)
                self.assertFalse(plan.native_qualified)

    def test_old_tuple_is_bad_shape_on_both_targets(self):
        lock_raw = HISTORICAL['members']['runtime.lock.json'].encode()
        for target, raw in HISTORICAL['configs'].items():
            with self.subTest(target=target):
                self.refuse(lock_raw, json.loads(raw), target, 'bad_shape')

    def test_current_operations_with_stale_descriptors_is_contract_mismatch(self):
        lock = json.loads(SUBJECTS['members']['runtime.lock.json'])
        old = json.loads(HISTORICAL['members']['runtime.lock.json'])
        lock['contract_members']['lock_schema'] = copy.deepcopy(old['contract_members']['lock_schema'])
        for target, raw in SUBJECTS['configs'].items():
            cfg = json.loads(raw)
            cfg['operations_sha256'] = lock['operations_sha256']
            with self.subTest(target=target):
                self.refuse(wire(lock), cfg, target, 'contract_mismatch')

    def test_current_lock_with_old_config_operations_is_operations_mismatch(self):
        lock_raw = SUBJECTS['members']['runtime.lock.json'].encode()
        old_operations = json.loads(HISTORICAL['members']['runtime.lock.json'])['operations_sha256']
        for target, raw in SUBJECTS['configs'].items():
            cfg = json.loads(raw)
            cfg['operations_sha256'] = old_operations
            with self.subTest(target=target):
                self.refuse(lock_raw, cfg, target, 'operations_mismatch')

    def test_wrong_schema_hash_or_any_member_size_is_contract_mismatch(self):
        for key, _ in MEMBERS:
            for field in ('sha256', 'bytes'):
                if key == 'operations' and field == 'sha256':
                    continue  # Operations SHA drift has its separate operations_mismatch contract.
                lock = json.loads(SUBJECTS['members']['runtime.lock.json'])
                descriptor = lock['contract_members'][key]['value']
                descriptor[field] = '0' * 64 if field == 'sha256' else descriptor[field] + 1
                for target, raw in SUBJECTS['configs'].items():
                    with self.subTest(member=key, field=field, target=target):
                        self.refuse(wire(lock), json.loads(raw), target, 'contract_mismatch')

    def test_e2_tuple_is_bad_shape_on_both_targets(self):
        lock_raw = HISTORICAL_E2['members']['runtime.lock.json'].encode()
        for target, raw in HISTORICAL_E2['configs'].items():
            with self.subTest(target=target):
                self.refuse(lock_raw, json.loads(raw), target, 'bad_shape')

    def test_all_prior_fixture_bytes_and_original_build_A_remain_exact(self):
        # Fixed original digests, independent of the newly selected index/data.
        originals = ORIGINAL_FIXTURE_SHA256
        for name, digest in originals.items():
            with self.subTest(fixture=name):
                self.assertEqual(sha256((FIXTURES / name).read_bytes()), digest)
        for key in ('real_build_A', 'real_build_A_ref', 'real_build_A_output_key', 'real_build_A_output'):
            self.assertEqual(SUBJECTS[key], HISTORICAL_E2[key])
        raw = SUBJECTS['real_build_A'].encode()
        self.assertEqual(len(raw), 3224)
        self.assertEqual(sha256(raw), '7c96304cfe47681904ebd84e4dbae7e2a0c28e4eea63f60f5e7a6e672f9111b4')

    def test_pgc20_fixture_index_and_embedded_owner_references_are_closed(self):
        index = json.loads((FIXTURES / 'subjects-pgc20-v1.json').read_bytes())
        self.assertNotIn('GateB_accepted_execution', index)
        for key in ('P1', 'historical_P1', 'historical_P1_e2', 'GateB_linux', 'GateB_macos', 'B1_proposal'):
            ref = index[key]
            measure_original_ref({k: ref[k] for k in ('path', 'bytes', 'sha256')},
                                 (FIXTURES / ref['path']).read_bytes())
        self.assertEqual(index['historical_P1_e2']['sha256'], ORIGINAL_FIXTURE_SHA256['p1-subjects.json'])
        self.assertEqual(index['P1']['revision'], REVISION)
        self.assertEqual(index['P1']['path'], 'p1-subjects-pgc20-v1.json')
        self.assertEqual(index['B1_held_F2'], json.loads((FIXTURES / 'subjects.json').read_bytes())['B1_held_F2'])

        def decoded_references(value):
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (ValueError, TypeError):
                    return
            if isinstance(value, dict):
                if {'path', 'bytes', 'sha256'} <= value.keys():
                    yield value
                for child in value.values():
                    yield from decoded_references(child)
            elif isinstance(value, list):
                for child in value:
                    yield from decoded_references(child)

        linux = json.loads((FIXTURES / index['GateB_linux']['path']).read_bytes())
        mac = json.loads((FIXTURES / index['GateB_macos']['path']).read_bytes())
        owner = json.loads((FIXTURES / index['B1_proposal']['path']).read_bytes())
        packets = [owner, mac['owner_inputs']]
        packets.extend(case['inputs'] for case in linux['cases'].values()
                       if case['expected']['result'] == 'DATA')
        self.assertEqual(linux['runtime_lock_raw'], SUBJECTS['members']['runtime.lock.json'])
        for number, packet in enumerate(packets):
            with self.subTest(packet=number):
                if 'runtime_lock_raw' in packet:
                    self.assertEqual(packet['runtime_lock_raw'], SUBJECTS['members']['runtime.lock.json'])
                originals = {name: raw.encode() for name, raw in packet['originals'].items()}
                originals.update({name: raw.encode() for name, raw in SUBJECTS['members'].items()})
                request_raw = packet.get('request_raw', linux['request_raw']).encode()
                self.assertEqual(request_raw, originals['b1-request.json'])
                context = json.loads(packet['context_raw'])
                measure_original_ref(context['request_ref'], request_raw)
                checked = 0
                for ref in decoded_references(packet):
                    if ref['path'] in originals:
                        measure_original_ref({k: ref[k] for k in ('path', 'bytes', 'sha256')}, originals[ref['path']])
                        checked += 1
                self.assertGreater(checked, 20)
        originals = mac['owner_inputs']['originals']
        for ref in (mac['profile_ref'], mac['observation_ref']):
            measure_original_ref(ref, originals[ref['path']].encode())


if __name__ == '__main__':
    unittest.main()
