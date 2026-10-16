"""Frozen RC0 declaration tests; no listener, DB, subprocess or real resolver."""
import copy
import json
import pathlib
import sys
import unittest

from oracle import Oracle, Refusal, decode, encode, sha
from jsonschema import Draft202012Validator

HERE = pathlib.Path(__file__).resolve().parent
CONTRACTS = HERE.parents[2] / 'contracts/runtime'
FIXTURE = json.loads((HERE / 'fixtures/paired-complete.json').read_text())
HISTORICAL = json.loads((HERE / 'fixtures/paired-complete-19.json').read_bytes())
NEGATIVES = json.loads((HERE / 'fixtures/negatives.json').read_text())
GAPS = json.loads((HERE / 'fixtures/owner-gaps.json').read_text())


def deny_network(event, args):
    if event in ('socket.connect', 'socket.bind', 'socket.getaddrinfo'):
        raise RuntimeError('RC0 oracle has no network effect authority')
sys.addaudithook(deny_network)


class RuntimeContractOracle(unittest.TestCase):
    def oracle(self):
        for name in ('runtime-lock-v1.schema.json', 'runtime-config-v1.schema.json', 'runtime-operations-v1.json'):
            self.assertTrue((CONTRACTS / name).is_file(), 'missing RC0 contract: ' + name)
        return Oracle()

    def historical_oracle(self):
        # Data-only historical oracle, never a product fallback or selector.
        oracle = self.oracle()
        oracle.lock_schema = decode((HERE/'fixtures/runtime-lock-schema-19.json').read_bytes())
        oracle.lock_validator = Draft202012Validator(oracle.lock_schema)
        return oracle

    def test_complete_paired_round_trip_and_all_literal_vectors(self):
        oracle = self.oracle()
        count = 0
        for target, vectors in FIXTURE['expected_vectors'].items():
            for mode, expected in vectors.items():
                with self.subTest(target=target, mode=mode):
                    cfg = FIXTURE['mode_configs'][target]['api' if mode == 'status' else mode]
                    self.assertEqual(decode(encode(cfg)), cfg)
                    result = oracle.select(FIXTURE['lock'], cfg, oracle.operations, observed_target=target, mode=mode)
                    self.assertEqual(result['argv'], expected)
                    self.assertFalse(result['effect_authority'])
                    self.assertFalse(result['native_qualified'])
                    count += 1
        self.assertEqual(count, 14)

    def test_named_paired_negatives_refuse_before_any_effect_port(self):
        oracle = self.oracle()
        self.assertEqual(len(NEGATIVES), 60)
        for case in NEGATIVES:
            with self.subTest(id=case['id'], target=case['target']):
                lock = copy.deepcopy(FIXTURE['lock'])
                cfg = copy.deepcopy(FIXTURE['configs'][case['target']])
                ops = copy.deepcopy(oracle.operations)
                value = {'lock':lock,'config':cfg,'operations':ops}[case['document']]
                for token in case['path'][:-1]:
                    value = value[token]
                key = case['path'][-1]
                if case['operation'] == 'delete':
                    del value[key]
                else:
                    value[key] = case['value']
                effects = {'process':0,'listener':0,'database':0,'resolver':0,'installer':0,'native':0}
                with self.assertRaises(Refusal) as refused:
                    oracle.select(lock, cfg, ops, observed_target=case['target'], mode='api')
                    for port in effects:
                        effects[port] += 1
                self.assertEqual(refused.exception.reason, case['expected_refusal'])
                self.assertEqual(effects, dict.fromkeys(effects, 0))

    def test_owner_gaps_have_shape_but_no_selection_or_native_credit(self):
        oracle = self.historical_oracle()
        rows = GAPS['native_materials']
        self.assertEqual(len(rows), 17)
        self.assertEqual(len({r['id'] for r in rows}), 17)
        self.assertTrue(all(r['state'] == 'GAP' and r['native_material_sha256'] is None for r in rows))
        owner_lock = GAPS['nonlaunchable_lock_declaration']
        self.assertEqual(list(oracle.lock_validator.iter_errors(owner_lock)), [])
        for target, cfg in HISTORICAL['configs'].items():
            with self.subTest(target=target):
                with self.assertRaises(Refusal) as refused:
                    oracle.select(owner_lock, cfg, oracle.operations, observed_target=target, mode='api')
                self.assertEqual(refused.exception.reason, 'required_binding_gap')

    def test_raw_decoder_refuses_duplicate_float_nonfinite_utf8_and_surrogate(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":1.0}', b'{"x":1e0}', b'{"x":NaN}',
                    b'{"x":Infinity}', b'\xef\xbb\xbf{}', b'{"x":"\xff"}', b'{"x":"\\ud800"}'):
            with self.subTest(raw=repr(raw)):
                with self.assertRaises(Refusal):
                    decode(raw)

    def test_byte_depth_and_no_trailing_line_feed_in_identifier(self):
        with self.assertRaises(Refusal):
            decode(b'{} ', byte_limit=2)
        with self.assertRaises(Refusal):
            decode(b'['*34 + b'0' + b']'*34)
        oracle = self.oracle()
        cfg = copy.deepcopy(FIXTURE['configs']['linux/arm64'])
        cfg['installation_id'] += '\n'
        with self.assertRaises(Refusal) as refused:
            oracle.select(FIXTURE['lock'], cfg, oracle.operations, observed_target='linux/arm64', mode='api')
        self.assertEqual(refused.exception.reason, 'bad_shape')

    def test_roles_health_and_finite_argv_independent_frozen_expectations(self):
        oracle = self.oracle()
        ops = oracle.operations
        self.assertEqual(ops['health']['c11_fields'], ['component','status','readiness'])
        self.assertEqual(ops['health']['telemetry_fields'], ['component','status','core_available','conductor','reason'])
        self.assertEqual(ops['health']['telemetry_host'], '127.0.0.1')
        self.assertFalse(ops['health']['telemetry_published'])
        self.assertEqual(ops['roles']['verifier_owner']['credential_consumers'], [])
        self.assertFalse(ops['roles']['verifier_owner']['login'])
        self.assertEqual(ops['secret_handles_by_mode']['supervisor'], ['supervisor'])
        self.assertEqual(ops['secret_handles_by_mode']['migrate'], ['migrator'])
        self.assertTrue(ops['roles']['bootstrap']['superuser'])
        self.assertTrue(ops['roles']['migrator']['superuser'])
        for role in ('api_request','probe','supervisor','observer','installer_enrollment'):
            self.assertFalse(ops['roles'][role]['superuser'])
            self.assertFalse(ops['roles'][role]['bypassrls'])
        for target in FIXTURE['expected_vectors']:
            argv = FIXTURE['expected_vectors'][target]['core-init']
            self.assertIn('--auth-local=scram-sha-256', argv)
            self.assertIn('--auth-host=scram-sha-256', argv)
            self.assertIn('--pwfile=/run/cortex/bootstrap/initdb-password', argv)
            self.assertNotIn('/dev/stdin', argv)
        self.assertFalse(ops['anonymous_health_is_install_ready'])

    def test_wrong_process_mode_refuses_without_secret_or_effect_selection(self):
        oracle = self.oracle()
        for target, cfg in FIXTURE['configs'].items():
            with self.subTest(target=target):
                with self.assertRaises(Refusal) as refused:
                    oracle.select(FIXTURE['lock'], cfg, oracle.operations, observed_target=target, mode='migrate')
                self.assertEqual(refused.exception.reason, 'mode_mismatch')

    def test_mixed_contract_member_path_refuses_even_with_matching_lock_digest(self):
        oracle = self.oracle()
        for target in FIXTURE['configs']:
            with self.subTest(target=target):
                lock = copy.deepcopy(FIXTURE['lock'])
                cfg = copy.deepcopy(FIXTURE['configs'][target])
                lock['contract_members']['config_schema']['value']['path'] = '/opt/cortex/next/contracts/deployment/cortex-prerequisite-v3.schema.json'
                cfg['runtime_lock_sha256'] = sha(lock)
                with self.assertRaises(Refusal) as refused:
                    oracle.select(lock, cfg, oracle.operations, observed_target=target, mode='api')
                self.assertEqual(refused.exception.reason, 'bad_shape')

    def test_historical_originals_are_exact_and_current20_is_distinct(self):
        self.assertEqual(sha((HERE/'fixtures/paired-complete-19.json').read_bytes()),
                         'e1a8649904bc89ec410d395b339813fae7bb49739e059549534d90372f251dfe')
        self.assertEqual(sha((HERE/'fixtures/runtime-lock-schema-19.json').read_bytes()),
                         '82cc5af6ab758e8fc51ada65e3cb8ec574f6e7bf8960802849c58280b07463aa')
        old, new = HISTORICAL['lock']['current_schema'], FIXTURE['lock']['current_schema']
        self.assertEqual((old['migration_count'],old['business_table_count']), (19,42))
        self.assertEqual((new['migration_count'],new['business_table_count']), (20,44))
        self.assertEqual(new['migrations'][:19], old['migrations'])
        self.assertEqual(new['migrations'][19]['id'], 'runtime-grants-0001')
        self.assertNotEqual(new['manifest_sha256'], old['manifest_sha256'])
        manifest = json.loads((HERE.parents[2]/'schema/manifest.json').read_bytes())
        target_id = 'runtime-grants-0001'
        migration_ids = [entry['id'] for entry in manifest['migrations']]
        self.assertEqual(migration_ids.count(target_id), 1)
        # The sealed20 bytes end at this identity; later source migrations are distinct.
        manifest['migrations'] = manifest['migrations'][:migration_ids.index(target_id)+1]
        self.assertEqual(new['manifest_sha256'], sha(manifest))

    def test_historical_current_validator_matrix_refuses_both_crosses(self):
        old, new = self.historical_oracle(), self.oracle()
        for oracle, fixture in ((old,HISTORICAL), (new,FIXTURE)):
            with self.subTest(own=fixture['lock']['current_schema']['migration_count']):
                self.assertEqual(list(oracle.lock_validator.iter_errors(fixture['lock'])), [])
                result = oracle.select(fixture['lock'], fixture['configs']['linux/arm64'],
                                       oracle.operations, observed_target='linux/arm64', mode='api')
                self.assertFalse(result['effect_authority'])
                self.assertFalse(result['native_qualified'])
        for oracle, fixture in ((old,FIXTURE), (new,HISTORICAL)):
            with self.subTest(cross=fixture['lock']['current_schema']['migration_count']):
                with self.assertRaises(Refusal) as refused:
                    oracle.select(fixture['lock'], fixture['configs']['linux/arm64'],
                                  oracle.operations, observed_target='linux/arm64', mode='api')
                self.assertEqual(refused.exception.reason, 'bad_shape')

    def test_selected_new_identity_abi_and_all_internal_pins(self):
        self.assertEqual(FIXTURE['lock']['release']['value'], {
            'release_id':'v0.2.001', 'api_contract':'cortex.core-api.v1',
            'core_module_version':'0.2.001', 'api_module_version':'0.2.001',
            'schema_version':1, 'event_version':1})
        raw = (CONTRACTS/'runtime-lock-v1.schema.json').read_bytes()
        member = FIXTURE['lock']['contract_members']['lock_schema']['value']
        self.assertEqual((member['sha256'],member['bytes']), (sha(raw),len(raw)))
        self.assertEqual(FIXTURE['lock']['format_version'], 1)
        for modes in FIXTURE['mode_configs'].values():
            for cfg in modes.values():
                self.assertEqual(cfg['runtime_lock_sha256'], sha(FIXTURE['lock']))
                self.assertEqual(cfg['metrics_labels']['release'], 'v0.2.001')
        for cfg in FIXTURE['configs'].values():
            self.assertEqual(cfg['runtime_lock_sha256'], sha(FIXTURE['lock']))

    def test_current_binding_gaps_still_refuse_before_selection(self):
        oracle = self.oracle()
        lock = copy.deepcopy(GAPS['nonlaunchable_lock_declaration'])
        lock['current_schema'] = copy.deepcopy(FIXTURE['lock']['current_schema'])
        lock['contract_members']['lock_schema'] = copy.deepcopy(FIXTURE['lock']['contract_members']['lock_schema'])
        self.assertEqual(list(oracle.lock_validator.iter_errors(lock)), [])
        with self.assertRaises(Refusal) as refused:
            oracle.select(lock, FIXTURE['configs']['linux/arm64'], oracle.operations,
                          observed_target='linux/arm64', mode='api')
        self.assertEqual(refused.exception.reason, 'required_binding_gap')


if __name__ == '__main__':
    unittest.main(verbosity=2)
