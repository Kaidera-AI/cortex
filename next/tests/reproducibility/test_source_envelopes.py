"""Paired SOURCE rules; public fixtures never grant native execution."""
import copy
import importlib
import json
import unittest
from pathlib import Path

from cortex_core.release import codec
from cortex_core.release.contracts import freeze
from cortex_core.release.inputs import FileObservation
from cortex_core.release.build import runner, inputs
from test_shared_input_consumer import fixture


FIELDS = (
    'core_sql_schema', 'migration_invocation', 'shipped_layout', 'service_factory',
    'core_probe', 'credentialed_ports', 'pg_ports', 'provider_seam', 'enrollment',
    'health_status_metrics', 'supervisor', 'backup_restore', 'runtime_lock',
    'product_launcher', 'consumer_descriptor', 'module_version', 'entrypoint',
    'argv', 'config', 'context_inventory', 'module_descriptor',
)
TARGETS = (('core', 'linux/arm64'), ('core', 'linux/amd64'),
           ('api', 'linux/arm64'), ('api', 'linux/amd64'))


class SourceEnvelopes(unittest.TestCase):
    def api(self):
        self.assertTrue(callable(getattr(runner, 'plan_source_recipes', None)),
                        'BODY RED: nonexecuting paired SOURCE entry is absent')
        return importlib.import_module('cortex_core.release.build.envelopes')

    def case(self):
        m = self.api()
        evidence, requests, envelopes, reads, roots = {}, {}, {}, [], {}

        def observe(path):
            reads.append(('fact', path))
            return FileObservation(evidence[path], 'regular', 0o644)

        for module, platform in TARGETS:
            arch = platform.split('/')[1]
            value, data = fixture.build_input()
            value['target'].update(module_id=module, oci_platform=platform)
            value['builder_requirements']['architecture'] = arch
            for name in ('contracts/descriptor.json', 'contracts/api.json',
                         'contracts/events.json', 'contracts/event-v1.json'):
                record = json.loads(data[name])
                if 'module_id' in record:
                    record['module_id'] = module
                if 'producer_module_id' in record:
                    record['producer_module_id'] = module
                data[name] = fixture.canonical(record)
            records = {name: fixture.file(name, raw) for name, raw in data.items()}
            value['descriptor'] = records['contracts/descriptor.json']
            c = value['compatibility_inputs']
            c['api_descriptor'] = records['contracts/api.json']
            c['event_index'] = records['contracts/events.json']
            c['event_schemas'] = [records['contracts/event-v1.json']]
            raw = codec.canonical_bytes(value)
            policy = {'input_sha256': codec.sha256(raw), 'policy_sha256': 'e' * 64,
                      'qualification_scope': 'fixture'}

            def read(name, data=data):
                reads.append(('input', name))
                return FileObservation(data[name], 'regular', 0o644)

            requests[(module, platform)] = m.SourceInput(raw, read, policy)

            def bind(name, content):
                path = f'facts/{module}/{arch}/{name}.json'
                evidence[path] = codec.canonical_bytes(content)
                return {'state': 'BOUND', 'owner': 'cox@helix', 'value': content,
                        'evidence': fixture.file(path, evidence[path])}

            facts = {name: bind(name, {'source_fixture_role': name, 'module': module})
                     for name in FIELDS}
            for name, content in (
                ('module_version', value['target']['module_version']),
                ('entrypoint', value['recipe']['entrypoint']),
                ('argv', value['recipe']['argv_template']),
                ('context_inventory', value['source']['inventory']),
                ('module_descriptor', value['descriptor']),
            ):
                facts[name] = bind(name, content)
            materials = {
                'base': bind('base', {'platform': platform, 'source_digest': 'f' * 64}),
                'toolchain': bind('toolchain', {'architecture': arch,
                    'engine_id': 'fixture-engine', 'abi': 'source-fixture-' + arch,
                    'sha256': 'c' * 64}),
                'image_members': bind('image_members', {'platform': platform,
                    'abi': 'source-fixture-' + arch, 'members': ['source-fixture-inspector']}),
            }
            if module not in envelopes:
                envelopes[module] = {'schema': 'cortex.recipe-source-envelope.v1',
                    'module': module, 'source': copy.deepcopy(value['source']) | {},
                    'targets': {}}
                envelopes[module]['source'] = {key: value['source'][key]
                    for key in ('repository', 'revision', 'tree')}
            envelopes[module]['targets'][platform] = {
                'slots': ['A', 'B'], 'runtime_facts': facts, 'materials': materials}
            roots[(module, platform)] = {}
            for slot in ('A', 'B'):
                roots[(module, platform)][slot] = {
                    name: f'/source-fixture/{module}/{arch}/{slot}/{name}'
                    for name in ('source', 'store', 'runroot', 'cache', 'output')}
        limits = {'cpu_millicores': 1000, 'memory_bytes': 1048576,
                  'disk_bytes': 1048576, 'input_bytes': 1048576,
                  'output_bytes': 1048576, 'decode_depth': 32, 'process_limit': 1,
                  'absolute_timeout_sec': 60,
                  'phase_timeouts_sec': {name: 10 for name in
                      ('input', 'parser', 'build', 'export', 'audit', 'cleanup')},
                  'environment_names': ['PYTHONHASHSEED']}
        return m, envelopes, requests, observe, roots, limits, reads, evidence

    def plan(self, case):
        _, envs, requests, observe, roots, limits, _, _ = case
        return runner.plan_source_recipes(
            {k: codec.canonical_bytes(v) for k, v in envs.items()},
            requests, observe, roots, limits)

    def parsed(self, case, key=('api', 'linux/amd64')):
        m, envs, requests, _, _, _, _, _ = case
        envelope = m.parse_source_envelope(codec.canonical_bytes(envs[key[0]]))
        facts = envelope.targets[key[1]]['runtime_facts']
        materials = envelope.targets[key[1]]['materials']
        req = requests[key]
        verified = inputs.admit(req.raw, req.reader, req.policy)
        return envelope, facts, materials, verified

    def test_paired_obligation_matrix_and_source_only_positive(self):
        case = self.case()
        result = self.plan(case)
        self.assertEqual({(x.module, x.platform) for x in result.requests}, set(TARGETS))
        self.assertEqual(len(result.requests), 4)
        self.assertFalse(result.launchable)
        self.assertFalse(result.native_qualified)
        self.assertEqual(result.schema, 'cortex.recipe-source-plan.v1')
        with self.assertRaises((AttributeError, TypeError)):
            result.launchable = True

    def test_rule01_missing_amd64_obligation_refuses(self):
        case = self.case(); m = case[0]
        parsed = {name: m.parse_source_envelope(codec.canonical_bytes(raw))
                  for name, raw in case[1].items()}
        broken = dict(parsed); broken.pop('api')
        with self.assertRaises(m.SourceRefusal):
            m._check_matrix(broken)

    def test_rule02_owner_unbound_refuses_before_any_reader(self):
        case = self.case(); m = case[0]
        row = case[1]['core']['targets']['linux/arm64']['runtime_facts']['service_factory']
        row.update(state='UNBOUND', value=None, evidence=None)
        env = m.parse_source_envelope(codec.canonical_bytes(case[1]['core']))
        self.assertIsInstance(env.targets['linux/arm64']['runtime_facts']['service_factory'],
                              m.UnboundFact)
        with self.assertRaises(m.SourceRefusal):
            m._runtime_bound(env.targets['linux/arm64']['runtime_facts'])
        with self.assertRaises(m.SourceRefusal) as caught:
            self.plan(case)
        self.assertEqual(caught.exception.code, 'r02_runtime_unbound')
        self.assertIn('service_factory', caught.exception.fields)
        self.assertEqual(case[6], [])

    def test_all_shipped_runtime_facts_are_typed_unbound(self):
        m = self.api(); root = Path(__file__).resolve().parents[2]
        for module in ('core', 'api'):
            raw = (root / f'release/recipes/{module}/source-envelope.json').read_bytes()
            env = m.parse_source_envelope(raw)
            self.assertEqual(set(env.targets), {'linux/arm64', 'linux/amd64'})
            for target in env.targets.values():
                self.assertEqual(set(target['runtime_facts']), set(FIELDS))
                self.assertTrue(all(isinstance(x, m.UnboundFact)
                    for x in (*target['runtime_facts'].values(), *target['materials'].values())))
                with self.assertRaises(m.SourceRefusal):
                    m._runtime_bound(target['runtime_facts'])

    def test_unbound_to_bound_keeps_same_envelope_field_shape(self):
        case = self.case(); m = case[0]
        original = copy.deepcopy(case[1]['api'])
        unbound = copy.deepcopy(original)
        for target in unbound['targets'].values():
            for row in (*target['runtime_facts'].values(), *target['materials'].values()):
                row.update(state='UNBOUND', value=None, evidence=None)
        self.assertEqual(set(original), set(unbound))
        a = m.parse_source_envelope(codec.canonical_bytes(original))
        b = m.parse_source_envelope(codec.canonical_bytes(unbound))
        self.assertEqual(set(a.targets['linux/amd64']['runtime_facts']),
                         set(b.targets['linux/amd64']['runtime_facts']))

    def test_rule03_policy_outline_cannot_be_recipe(self):
        case = self.case(); m = case[0]
        _, facts, _, verified = self.parsed(case)
        facts = dict(facts); old = facts['entrypoint']
        facts['entrypoint'] = m.BoundFact(old.owner, 'policy.json', old.evidence)
        with self.assertRaises(m.SourceRefusal):
            m._recipe(verified, facts)

    def test_rule04_literal_argv_cannot_be_default_or_wrapper(self):
        case = self.case(); m = case[0]
        _, facts, _, verified = self.parsed(case)
        facts = dict(facts); old = facts['argv']
        facts['argv'] = m.BoundFact(old.owner, ('sh', '-c', 'Dockerfile'), old.evidence)
        with self.assertRaises(m.SourceRefusal):
            m._argv(verified, facts)

    def test_rule05_missing_material_is_not_a_donor_hash(self):
        case = self.case(); m = case[0]
        _, _, materials, _ = self.parsed(case)
        materials = dict(materials); materials['base'] = m.UnboundFact('cox@helix')
        with self.assertRaises(m.SourceRefusal):
            m._materials(materials)

    def test_rule06_fixture_and_caller_flags_never_admit_native(self):
        case = self.case(); m = case[0]
        with self.assertRaises(m.SourceRefusal):
            m._source_scope({'qualification_scope': 'native', 'accepted': True})
        req = case[2][('api', 'linux/amd64')]
        case[2][('api', 'linux/amd64')] = m.SourceInput(req.raw, req.reader,
            {**req.policy, 'qualification_scope': 'native'})
        with self.assertRaises(m.SourceRefusal):
            self.plan(case)
        self.assertFalse(hasattr(runner, 'run_native_pair'))

    def test_rule07_wrong_platform_engine_or_abi_refuses(self):
        case = self.case(); m = case[0]
        _, facts, materials, verified = self.parsed(case)
        bad = dict(materials); old = bad['toolchain']
        value = dict(old.value); value['architecture'] = 'arm64'
        bad['toolchain'] = m.BoundFact(old.owner, freeze(value), old.evidence)
        with self.assertRaises(m.SourceRefusal):
            m._target(verified, ('api', 'linux/amd64'), facts, bad)

    def test_rule08_custody_rereads_actual_source(self):
        case = self.case(); m = case[0]
        _, _, _, verified = self.parsed(case)
        req = case[2][('api', 'linux/amd64')]
        def changed(path):
            x = req.reader(path)
            return FileObservation(x.data + b'changed', x.kind, x.mode)
        with self.assertRaises(m.SourceRefusal):
            m._custody(verified, changed)

    def test_rule09_alias_nested_a_b_mutable_roots_refuse(self):
        case = self.case(); m = case[0]
        roots = copy.deepcopy(case[4])
        roots[('api', 'linux/amd64')]['B']['cache'] = roots[('api', 'linux/amd64')]['A']['cache']
        with self.assertRaises(m.SourceRefusal):
            m._roots(roots)

    def test_rule10_every_budget_is_finite_and_environment_closed(self):
        case = self.case(); m = case[0]
        bad = copy.deepcopy(case[5]); bad['output_bytes'] = 0
        with self.assertRaises(m.SourceRefusal):
            m._limits(bad)
        for key in ('memory_bytes', 'input_bytes', 'decode_depth', 'process_limit'):
            bad = copy.deepcopy(case[5]); bad[key] = True
            with self.subTest(key=key), self.assertRaises(m.SourceRefusal):
                m._limits(bad)

    def test_rule11_failure_stops_later_stages_and_cleanup_cannot_pass(self):
        self.api(); events = []
        def operate(name):
            events.append(name); return {'exit': 1}
        result = runner.lifecycle(('first', 'second'), operate,
            lambda: {'complete': True},
            lambda: {'complete': True, 'remaining_owned_resources': []}, lambda x: None)
        self.assertEqual(events, ['first'])
        self.assertEqual(result['result'], 'RED')
        self.assertEqual(result['reason'], 'ValueError')
        result = runner.lifecycle(('first',), lambda n: {'exit': 0},
            lambda: {'complete': True},
            lambda: {'complete': False, 'remaining_owned_resources': ['fixture']}, lambda x: None)
        self.assertEqual(result['result'], 'RED')

    def test_rule12_source_record_cannot_be_native_receipt_authority(self):
        case = self.case(); m = case[0]
        bad = dict(case[2]); bad[('api', 'linux/amd64')] = {'accepted': True}
        with self.assertRaises(m.SourceRefusal):
            m._request_types(bad)
        with self.assertRaises(m.SourceRefusal):
            runner.plan_source_recipes({}, {}, None, {}, {})

    def test_r01_wiring_is_actual_parser_not_a_private_copy(self):
        case = self.case(); m = case[0]
        req = case[2][('api', 'linux/amd64')]
        value = json.loads(req.raw); value['target']['module_version'] = 'changed'
        case[2][('api', 'linux/amd64')] = m.SourceInput(codec.canonical_bytes(value), req.reader, req.policy)
        with self.assertRaises(m.SourceRefusal) as caught:
            self.plan(case)
        self.assertEqual(caught.exception.code, 'r02_r01_refused')

    def test_changed_fact_proof_and_placeholder_promotion_refuse(self):
        case = self.case(); m = case[0]
        key = 'facts/api/amd64/config.json'
        case[7][key] = b'"changed"'
        with self.assertRaises(m.SourceRefusal) as caught:
            self.plan(case)
        self.assertEqual(caught.exception.code, 'r02_fact_custody')
        bad = copy.deepcopy(case[1]['api'])
        bad['targets']['linux/amd64']['runtime_facts']['entrypoint']['value'] = 'UNBOUND'
        with self.assertRaises(m.SourceRefusal):
            m.parse_source_envelope(codec.canonical_bytes(bad))

    def test_unknown_keys_duplicate_raw_json_and_mutable_lookalikes_refuse(self):
        case = self.case(); m = case[0]
        bad = copy.deepcopy(case[1]['api']); bad['launchable'] = True
        with self.assertRaises(m.SourceRefusal):
            m.parse_source_envelope(codec.canonical_bytes(bad))
        with self.assertRaises(m.SourceRefusal):
            m.parse_source_envelope(b'{"module":"api","module":"core"}')
        good = m.parse_source_envelope(codec.canonical_bytes(case[1]['api']))
        with self.assertRaises(TypeError):
            good.targets['linux/amd64']['runtime_facts']['argv'] = None


if __name__ == '__main__':
    unittest.main(verbosity=2)
