"""Frozen RC0 declaration tests; no listener, DB, subprocess or real resolver."""
import copy
import json
import pathlib
import sys
import unittest

from oracle import Oracle, Refusal, decode, encode, sha

HERE = pathlib.Path(__file__).resolve().parent
CONTRACTS = HERE.parents[2] / 'contracts/runtime'
FIXTURE = json.loads((HERE / 'fixtures/paired-complete.json').read_text())
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
        oracle = self.oracle()
        rows = GAPS['native_materials']
        self.assertEqual(len(rows), 17)
        self.assertEqual(len({r['id'] for r in rows}), 17)
        self.assertTrue(all(r['state'] == 'GAP' and r['native_material_sha256'] is None for r in rows))
        owner_lock = GAPS['nonlaunchable_lock_declaration']
        self.assertEqual(list(oracle.lock_validator.iter_errors(owner_lock)), [])
        for target, cfg in FIXTURE['configs'].items():
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
