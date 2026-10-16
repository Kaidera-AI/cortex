"""RC2 oracle frozen before source. All positives use a non-authoritative double.

No database, listener, provider or product process is created. The output is a
declaration plan, never a runtime execution grant or real signed-lock admission.
"""
import ast
import copy
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cortex_core.release.codec import sha256

NEXT = Path(__file__).resolve().parents[2]
SRC = NEXT / 'src/cortex_core'
DOCS = NEXT / 'docs/runtime/rc0'
FIXTURE = json.loads((DOCS / 'fixtures/paired-complete.json').read_bytes())
NEGATIVES = json.loads((DOCS / 'fixtures/negatives.json').read_bytes())
GAP_UNIT = 'R01 unit 3: signed Core/API aggregate and member admission'
AGGREGATE = 'test-only-aggregate'
MEMBER = 'runtime.lock.json'


def encode(value):
    return (json.dumps(value, indent=2, ensure_ascii=True) + '\n').encode()


def product_import_gate(source):
    # Product has no test import dependency, including literal dynamic imports.
    tree = ast.parse(source)
    return not any(isinstance(node, ast.Constant) and isinstance(node.value, str)
                   and 'non_authoritative_signed_member_double' in node.value
                   or isinstance(node, ast.Import) and any('non_authoritative_signed_member_double' in x.name for x in node.names)
                   or isinstance(node, ast.ImportFrom) and 'non_authoritative_signed_member_double' in (node.module or '')
                   for node in ast.walk(tree))


class Admission(unittest.TestCase):
    def runtime(self):
        self.assertTrue((SRC / 'runtime/signed_member.py').is_file(), 'RC2 port source absent')
        return (importlib.import_module('cortex_core.runtime.signed_member'),
                importlib.import_module('cortex_core.runtime.config'),
                importlib.import_module('cortex_core.runtime.__main__'))

    def materials(self, target='linux/arm64', mode='api'):
        lock = copy.deepcopy(FIXTURE['lock'])
        cfg = copy.deepcopy(FIXTURE['mode_configs'][target]['api' if mode == 'status' else mode])
        cfg['runtime_lock_sha256'] = sha256(encode(lock))
        return lock, cfg

    def select(self, lock, cfg, *, target='linux/arm64', mode='api', operations_raw=None, raw=None):
        port, config, _ = self.runtime()
        from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble
        double = NonAuthoritativeSignedMemberDouble(encode(lock) if raw is None else raw)
        with double.installed(port):
            member = port.request_member(AGGREGATE, MEMBER)
        kwargs = {} if operations_raw is None else {'operations_raw': operations_raw}
        result = config.admit_config(member, encode(cfg), aggregate_id=AGGREGATE,
                                     observed_target=target, mode=mode, **kwargs)
        self.assertEqual(double.calls, [(AGGREGATE, MEMBER)])
        self.assertEqual(double.reads, [262144])
        return result

    def refuse(self, reason, fn):
        port, _, _ = self.runtime()
        with self.assertRaises(port.Refusal) as caught:
            fn()
        self.assertEqual(caught.exception.reason, reason)

    def test_production_default_valid_lock_refuses_before_any_io(self):
        port, _, entry = self.runtime()
        lock, cfg = self.materials()
        def no_effect(*args, **kwargs):
            self.fail('production default reached an input/effect')
        with patch('builtins.open', no_effect), patch.object(Path, 'read_bytes', no_effect), \
             patch('socket.socket', no_effect), patch('subprocess.Popen', no_effect):
            with self.assertRaises(port.Refusal) as caught:
                entry.admit_entrypoint(['api', '--config', '/run/cortex/instance.json', '--runtime-lock',
                                       '/run/cortex/runtime.lock.json'], aggregate_id=AGGREGATE,
                                      observed_target='linux/arm64', config_reader=no_effect, environ={})
        self.assertEqual(caught.exception.reason, 'required_binding_gap')
        self.assertEqual(caught.exception.unit, GAP_UNIT)
        self.assertGreater(len(encode(lock)) + len(encode(cfg)), 0)

    def test_module_main_is_unbound_before_reading_argv_or_environment(self):
        port, _, entry = self.runtime()
        class NoRead:
            def __iter__(self):
                raise AssertionError('argv read before port')
        with patch.object(sys, 'argv', NoRead()), patch('builtins.open', side_effect=AssertionError('file read')):
            self.refuse('required_binding_gap', lambda: entry.main())

    def test_hash_matching_plain_member_cannot_be_promoted(self):
        port, config, _ = self.runtime()
        lock, cfg = self.materials()
        raw = encode(lock)
        for candidate in (raw, lock, Path('/run/cortex/runtime.lock.json'),
                          SimpleNamespace(aggregate_id=AGGREGATE, signing_root_id='test-only-root',
                                          member_name=MEMBER, member_sha256=sha256(raw), read_original=lambda: raw)):
            with self.subTest(candidate=type(candidate).__name__):
                self.refuse('unsealed_member', lambda: config.admit_config(candidate, encode(cfg),
                    aggregate_id=AGGREGATE, observed_target='linux/arm64', mode='api'))

    def test_bound_and_policy_dict_do_not_bind_port(self):
        port, _, _ = self.runtime()
        for candidate in ({'state':'BOUND','value':FIXTURE['lock']}, {'accepted_policy':FIXTURE['lock']}, FIXTURE):
            with self.subTest(candidate=list(candidate)[:1]):
                self.refuse('required_binding_gap', lambda: port.request_member(candidate, MEMBER))
        self.assertFalse(hasattr(port, 'bind'))

    def test_seal_constructor_and_object_new_cannot_mint(self):
        port, config, _ = self.runtime()
        with self.assertRaises(TypeError):
            port.OriginalMember()
        forged = object.__new__(port.OriginalMember)
        lock, cfg = self.materials()
        self.refuse('unsealed_member', lambda: config.admit_config(forged, encode(cfg),
                    aggregate_id=AGGREGATE, observed_target='linux/arm64', mode='api'))

    def test_seal_identity_reader_and_immutable_snapshot(self):
        port, _, _ = self.runtime()
        from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble
        raw = encode(self.materials()[0])
        double = NonAuthoritativeSignedMemberDouble(raw)
        with double.installed(port):
            member = port.request_member(AGGREGATE, MEMBER)
        self.assertEqual(member.aggregate_id, AGGREGATE)
        self.assertEqual(member.signing_root_id, 'test-only-root')
        self.assertEqual(member.member_name, MEMBER)
        self.assertEqual(member.member_sha256, sha256(raw))
        double.raw = b'changed after admission'
        self.assertEqual(port.read_member(member, aggregate_id=AGGREGATE, member_name=MEMBER), raw)
        self.assertEqual(double.reads, [262144])
        with self.assertRaises(AttributeError):
            member.member_sha256 = '0'*64
        self.refuse('member_identity_mismatch', lambda: port.read_member(member, aggregate_id='different', member_name=MEMBER))
        self.refuse('member_identity_mismatch', lambda: port.read_member(member, aggregate_id=AGGREGATE, member_name='different'))

    def test_authority_wrong_identity_or_changed_member_refuses(self):
        port, _, _ = self.runtime()
        from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble
        for kwargs, reason in (({'aggregate_id':'different'},'member_identity_mismatch'),
                               ({'member_name':'other'},'member_identity_mismatch'),
                               ({'declared_sha':'0'*64},'member_digest_mismatch'),
                               ({'signing_root_id':''},'member_identity_mismatch')):
            with self.subTest(kwargs=kwargs):
                double = NonAuthoritativeSignedMemberDouble(encode(self.materials()[0]), **kwargs)
                with double.installed(port):
                    self.refuse(reason, lambda: port.request_member(AGGREGATE, MEMBER))

    def test_product_import_double_gate_detects_its_positive_controls(self):
        self.runtime()
        for source in ('import non_authoritative_signed_member_double',
                       'from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble',
                       "__import__('non_authoritative_signed_member_double')"):
            self.assertFalse(product_import_gate(source))
        self.assertTrue(product_import_gate('import json'))
        for file in SRC.rglob('*.py'):
            self.assertTrue(product_import_gate(file.read_text()), str(file))

    def test_product_code_import_double_fails_at_runtime(self):
        self.runtime()
        name = 'non_authoritative_signed_member_double'
        saved = sys.modules.pop(name, None)
        try:
            with self.assertRaises(ImportError):
                exec(compile('import '+name, str(SRC/'runtime/product_import_probe.py'), 'exec'), {})
        finally:
            sys.modules.pop(name, None)
            if saved is not None:
                sys.modules[name] = saved

    def test_original_noncanonical_bytes_are_hashed_without_reencoding(self):
        lock, cfg = self.materials()
        raw = json.dumps(lock, separators=(',',':')).encode() + b'  \n'
        cfg['runtime_lock_sha256'] = sha256(raw)
        result = self.select(lock, cfg, raw=raw)
        self.assertEqual(result.lock_sha256, sha256(raw))
        cfg['runtime_lock_sha256'] = sha256(encode(lock))
        self.refuse('lock_mismatch', lambda: self.select(lock, cfg, raw=raw))

    def test_lock_path_aliases_core_and_api_on_both_targets(self):
        for target in FIXTURE['configs']:
            for component in ('core','api'):
                for alias in ('/run/cortex/runtime.lock.json','//run/cortex/runtime.lock.json',
                              '/run/cortex/./runtime.lock.json','/run/cortex/x/../runtime.lock.json',
                              '/run//cortex/runtime.lock.json'):
                    with self.subTest(target=target, component=component, alias=alias):
                        lock, cfg = self.materials()
                        lock['targets'][target][component]['image']['value']['path'] = alias
                        cfg['runtime_lock_sha256'] = sha256(encode(lock))
                        self.refuse('image_lock_cycle', lambda: self.select(lock, cfg))

    def test_unknown_mode(self):
        lock, cfg = self.materials()
        self.refuse('unsupported_mode', lambda: self.select(lock, cfg, mode='serve'))

    def test_paired_version_mismatch(self):
        lock, cfg = self.materials()
        lock['targets']['linux/amd64']['core']['pg_major']['value'] = 18
        cfg['runtime_lock_sha256'] = sha256(encode(lock))
        self.refuse('paired_version_mismatch', lambda: self.select(lock, cfg))

    def test_mode_mismatch(self):
        lock, cfg = self.materials()
        self.refuse('mode_mismatch', lambda: self.select(lock, cfg, mode='migrate'))

    def test_owner_resolver_and_identity_cross_bindings(self):
        for path, reason in ((('resolver','owner_binding_receipt'),'resolver_owner_mismatch'),
                             (('metrics_labels','deployment_id'),'identity_mismatch'),
                             (('metrics_labels','release'),'identity_mismatch')):
            lock, cfg = self.materials()
            cfg[path[0]][path[1]] = '00000000-0000-0000-0000-000000000000' if path[1] != 'release' else 'wrong'
            with self.subTest(path=path):
                self.refuse(reason, lambda: self.select(lock, cfg))

    def test_project_routing_mismatch(self):
        lock, cfg = self.materials()
        cfg['project_repositories'] = {'different':'fixture/repository'}
        self.refuse('project_routing_mismatch', lambda: self.select(lock, cfg))

    def test_unsafe_member_path(self):
        lock, cfg = self.materials()
        lock['targets']['linux/arm64']['closure']['value']['inventory']['path'] = '/fixture/../inventory.json'
        cfg['runtime_lock_sha256'] = sha256(encode(lock))
        self.refuse('unsafe_member_path', lambda: self.select(lock, cfg))

    def test_contract_files_are_pinned_not_candidate_authority(self):
        port, config, _ = self.runtime()
        lock, cfg = self.materials()
        raw = (NEXT/'contracts/runtime/runtime-operations-v1.json').read_bytes() + b' '
        self.refuse('operations_mismatch', lambda: self.select(lock, cfg, operations_raw=raw))
        contracts = importlib.import_module('cortex_core.runtime.contracts')
        original = Path.read_bytes
        def altered(path):
            raw = original(path)
            return raw + b' ' if path.name == 'runtime-config-v1.schema.json' else raw
        with patch.object(Path, 'read_bytes', altered):
            self.refuse('contract_mismatch', contracts.load_contracts)

    def test_entrypoint_exact_vectors_and_config_read_once(self):
        port, _, entry = self.runtime()
        from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble
        for mode in ('api','supervisor','inspect'):
            lock, cfg = self.materials(mode=mode)
            argv = FIXTURE['expected_vectors']['linux/arm64'][mode][5:]
            reads = []
            def reader(path, limit):
                reads.append((path, limit)); return encode(cfg)
            with NonAuthoritativeSignedMemberDouble(encode(lock)).installed(port):
                result = entry.admit_entrypoint(argv, aggregate_id=AGGREGATE,
                    observed_target='linux/arm64', config_reader=reader, environ={})
            self.assertEqual(list(result.argv), FIXTURE['expected_vectors']['linux/arm64'][mode])
            self.assertEqual(reads, [(argv[2],65536)])
            self.assertFalse(result.effect_authority)

    def test_entrypoint_unknown_flag_duplicate_path_and_env_override(self):
        port, _, entry = self.runtime()
        from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble
        lock, cfg = self.materials()
        base = FIXTURE['expected_vectors']['linux/arm64']['api'][5:]
        for argv, env, reason in ((base+['--factory','evil'],{},'invalid_argv'),
                                  (base+['--config','/other'],{},'invalid_argv'),
                                  ([x.replace('instance.json','other.json') for x in base],{},'invalid_argv'),
                                  (base,{'CORTEX_CONFIG':'/other'},'environment_override'),
                                  (base,{'PGPASSWORD':'synthetic-not-secret'},'environment_override'),
                                  (base,{'PYTHONPATH':'/other'},'environment_override')):
            with self.subTest(argv=argv, env=list(env)):
                with NonAuthoritativeSignedMemberDouble(encode(lock)).installed(port):
                    self.refuse(reason, lambda: entry.admit_entrypoint(argv, aggregate_id=AGGREGATE,
                        observed_target='linux/arm64', config_reader=lambda *a: encode(cfg), environ=env))

    def test_migrate_adapter_remains_unbound_even_after_pipeline(self):
        port, _, entry = self.runtime()
        from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble
        lock, cfg = self.materials(mode='migrate')
        argv = FIXTURE['expected_vectors']['linux/arm64']['migrate'][5:]
        with NonAuthoritativeSignedMemberDouble(encode(lock)).installed(port):
            with self.assertRaises(port.Refusal) as caught:
                entry.admit_entrypoint(argv, aggregate_id=AGGREGATE, observed_target='linux/arm64',
                    config_reader=lambda *a: encode(cfg), environ={})
        self.assertEqual(caught.exception.reason, 'required_binding_gap')
        self.assertEqual(caught.exception.unit, 'RC2 finite migration owner adapter')

    def test_selected20_manifest_and_schema_pin_are_exact(self):
        contracts = importlib.import_module('cortex_core.runtime.contracts')
        lock, cfg = self.materials()
        current = lock['current_schema']
        manifest = json.loads((NEXT/'schema/manifest.json').read_bytes())
        self.assertEqual((current['migration_count'],current['business_table_count']), (20,44))
        self.assertEqual(current['migrations'], manifest['migrations'])
        self.assertEqual(current['manifest_sha256'], sha256((NEXT/'schema/manifest.json').read_bytes()))
        raw = (NEXT/'contracts/runtime/runtime-lock-v1.schema.json').read_bytes()
        self.assertEqual(contracts.RC0_SHA256['runtime-lock-v1.schema.json'], sha256(raw))
        pin = lock['contract_members']['lock_schema']['value']
        self.assertEqual((pin['sha256'],pin['bytes']), (sha256(raw),len(raw)))
        result = self.select(lock, cfg)
        self.assertFalse(result.effect_authority)
        self.assertFalse(result.native_qualified)

    def test_old19_package_and_mixed_schema_pins_refuse_current_package(self):
        historical = json.loads((DOCS/'fixtures/paired-complete-19.json').read_bytes())
        lock, cfg = self.materials()
        for field in ('migration_count', 'business_table_count', 'manifest_sha256', 'migrations'):
            mixed = copy.deepcopy(lock)
            mixed['current_schema'][field] = copy.deepcopy(historical['lock']['current_schema'][field])
            changed = copy.deepcopy(cfg)
            changed['runtime_lock_sha256'] = sha256(encode(mixed))
            with self.subTest(field=field):
                self.refuse('bad_shape', lambda: self.select(mixed, changed))
        self.refuse('bad_shape', lambda: self.select(historical['lock'], historical['configs']['linux/arm64']))
        for field in ('sha256', 'bytes'):
            mixed = copy.deepcopy(lock)
            mixed['contract_members']['lock_schema']['value'][field] = historical['lock']['contract_members']['lock_schema']['value'][field]
            changed = copy.deepcopy(cfg)
            changed['runtime_lock_sha256'] = sha256(encode(mixed))
            with self.subTest(pin=field):
                self.refuse('contract_mismatch', lambda: self.select(mixed, changed))

    def test_old_validator_bytes_cannot_be_loaded_as_current_package(self):
        contracts = importlib.import_module('cortex_core.runtime.contracts')
        historical = (DOCS/'fixtures/runtime-lock-schema-19.json').read_bytes()
        original = Path.read_bytes
        def substituted(path):
            return historical if path.name == 'runtime-lock-v1.schema.json' else original(path)
        with patch.object(Path, 'read_bytes', substituted):
            self.refuse('contract_mismatch', contracts.load_contracts)


def positive(target, mode):
    def test(self):
        lock, cfg = self.materials(target, mode)
        result = self.select(lock, cfg, target=target, mode=mode)
        self.assertEqual(list(result.argv), FIXTURE['expected_vectors'][target][mode])
        self.assertEqual(result.mode, mode)
        self.assertEqual(result.target, target)
        self.assertEqual(result.secret_handles, tuple(FIXTURE['mode_configs'][target]['api' if mode=='status' else mode]['resolver']['handles']) if mode!='status' else ())
        self.assertFalse(result.effect_authority)
        self.assertFalse(result.native_qualified)
        self.assertEqual(result.authority_status, 'UNBOUND')
    return test


def negative(case):
    def test(self):
        lock, cfg = self.materials(case['target'])
        ops = json.loads((NEXT/'contracts/runtime/runtime-operations-v1.json').read_bytes())
        doc = {'lock':lock,'config':cfg,'operations':ops}[case['document']]
        for token in case['path'][:-1]:
            doc = doc[token]
        if case['operation'] == 'delete':
            del doc[case['path'][-1]]
        else:
            doc[case['path'][-1]] = case['value']
        if case['document']=='lock':
            cfg['runtime_lock_sha256'] = sha256(encode(lock))
        self.refuse(case['expected_refusal'], lambda: self.select(lock, cfg, target=case['target'], operations_raw=encode(ops)))
    return test


def malformed(raw, reason):
    def test(self):
        self.runtime()
        contracts = importlib.import_module('cortex_core.runtime.contracts')
        self.refuse(reason, lambda: contracts.decode(raw, limit=65536))
    return test


for target, modes in FIXTURE['expected_vectors'].items():
    for mode in modes:
        setattr(Admission, 'test_vector_'+target.replace('/','_')+'_'+mode.replace('-','_'), positive(target, mode))
for case in NEGATIVES:
    setattr(Admission, 'test_rc0_'+case['target'].replace('/','_')+'_'+case['id'].replace('-','_'), negative(case))
for i, (raw, reason) in enumerate(((b'{"x":1,"x":2}','raw_shape'), (b'{"x":1.0}','raw_shape'),
        (b'{"x":1e0}','raw_shape'),(b'{"x":NaN}','raw_shape'),(b'{"x":Infinity}','raw_shape'),
        (b'\xef\xbb\xbf{}','raw_shape'),(b'{"x":"\xff"}','raw_shape'),(b'{"x":"\\ud800"}','raw_shape'),
        (b'['*34+b'0'+b']'*34,'raw_limit'),(b' '*65537,'raw_limit'),({},'raw_limit'))):
    setattr(Admission, 'test_raw_'+str(i), malformed(raw, reason))


if __name__ == '__main__':
    unittest.main(verbosity=2)
