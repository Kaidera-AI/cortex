"""Typed recipe SOURCE envelopes. No executable recipe or native authority.

BOUND means measured descriptive source bytes, never independent release/image
acceptance. The only result is immutable nonexecuting fixture-source metadata.
"""
from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import PurePosixPath
import re

from ..codec import canonical_bytes, decode_json, relative_path, sha256
from ..contracts import freeze, thaw
from ..inputs import FileObservation
from . import inputs

RUNTIME_FIELDS = frozenset((
    'core_sql_schema', 'migration_invocation', 'shipped_layout', 'service_factory',
    'core_probe', 'credentialed_ports', 'pg_ports', 'provider_seam', 'enrollment',
    'health_status_metrics', 'supervisor', 'backup_restore', 'runtime_lock',
    'product_launcher', 'consumer_descriptor', 'module_version', 'entrypoint',
    'argv', 'config', 'context_inventory', 'module_descriptor',
))
PLATFORMS = frozenset(('linux/arm64', 'linux/amd64'))
TARGETS = frozenset((module, platform) for module in ('core', 'api') for platform in PLATFORMS)
MATERIALS = frozenset(('base', 'toolchain', 'image_members'))
ROOTS = frozenset(('source', 'store', 'runroot', 'cache', 'output'))
PHASES = frozenset(('input', 'parser', 'build', 'export', 'audit', 'cleanup'))
MAX_SOURCE_BYTES = 65536


class SourceRefusal(ValueError):
    def __init__(self, code, fields=()):
        self.code, self.fields = code, tuple(sorted(fields))
        super().__init__(code)


def _require(value, code, fields=()):
    if not value:
        raise SourceRefusal(code, fields)


@dataclass(frozen=True)
class UnboundFact:
    owner: str


@dataclass(frozen=True)
class BoundFact:
    owner: str
    value: object
    evidence: object


@dataclass(frozen=True)
class RecipeEnvelope:
    raw: bytes
    sha256: str
    module: str
    source: object
    targets: object


@dataclass(frozen=True)
class SourceInput:
    raw: bytes
    reader: object
    policy: object


@dataclass(frozen=True)
class SourceRequest:
    module: str
    platform: str
    input_sha256: str
    entrypoint: str
    argv: tuple
    roots: object
    launchable: bool = False
    native_qualified: bool = False


@dataclass(frozen=True)
class SourcePlan:
    requests: tuple
    limits: object
    schema: str = 'cortex.recipe-source-plan.v1'
    launchable: bool = False
    native_qualified: bool = False


def _descriptive(value, depth=0):
    _require(depth <= 32, 'r02_fact_shape')
    _require(value is not None and type(value) in (str, int, bool, list, dict), 'r02_fact_shape')
    if type(value) is str:
        _require(bool(value) and value.upper() not in ('UNBOUND', 'GAP', 'TODO', 'TBD'), 'r02_fact_unbound_value')
    if type(value) in (list, dict):
        _require(bool(value), 'r02_fact_unbound_value')
        if type(value) is dict:
            _require(not {'accepted', 'native_qualified', 'launchable', 'authority', 'qualified'} & set(value),
                     'r02_fact_authority')
        for item in value.values() if type(value) is dict else value:
            _descriptive(item, depth + 1)


def _fact(record):
    _require(type(record) is dict and set(record) == {'state', 'owner', 'value', 'evidence'}
             and record['owner'] == 'cox@helix', 'r02_fact_shape')
    if record['state'] == 'UNBOUND':
        _require(record['value'] is None and record['evidence'] is None, 'r02_fact_unbound_value')
        return UnboundFact(record['owner'])
    _require(record['state'] == 'BOUND', 'r02_fact_state')
    _require(type(record['value']) in (str, list, dict), 'r02_fact_shape')
    _descriptive(record['value'])
    evidence = record['evidence']
    _require(type(evidence) is dict and set(evidence) == {'path', 'kind', 'bytes', 'sha256', 'mode'},
             'r02_fact_evidence')
    _require(evidence['kind'] == 'regular' and type(evidence['mode']) is int and evidence['mode'] == 0o644
             and type(evidence['bytes']) is int and 0 < evidence['bytes'] <= MAX_SOURCE_BYTES
             and type(evidence['sha256']) is str and re.fullmatch('[0-9a-f]{64}', evidence['sha256']) is not None,
             'r02_fact_evidence')
    try:
        relative_path(evidence['path'])
    except (ValueError, TypeError):
        raise SourceRefusal('r02_fact_evidence') from None
    return BoundFact(record['owner'], freeze(record['value']), freeze(evidence))


def parse_source_envelope(raw):
    try:
        doc = decode_json(raw, MAX_SOURCE_BYTES)
    except (ValueError, TypeError, RecursionError):
        raise SourceRefusal('r02_envelope_json') from None
    _require(type(doc) is dict and set(doc) == {'schema', 'module', 'source', 'targets'}
             and doc['schema'] == 'cortex.recipe-source-envelope.v1'
             and doc['module'] in ('core', 'api'), 'r02_envelope_shape')
    source = doc['source']
    _require(type(source) is dict and set(source) == {'repository', 'revision', 'tree'}
             and source['repository'] == 'Kaidera-AI/cortex'
             and all(type(source[k]) is str and re.fullmatch('[0-9a-f]{40}', source[k]) is not None
                     for k in ('revision', 'tree')), 'r02_source_identity')
    _require(type(doc['targets']) is dict and set(doc['targets']) == PLATFORMS, 'r02_target_matrix')
    targets = {}
    for platform, target in doc['targets'].items():
        _require(type(target) is dict and set(target) == {'slots', 'runtime_facts', 'materials'}
                 and target['slots'] == ['A', 'B'], 'r02_target_matrix')
        _require(type(target['runtime_facts']) is dict and set(target['runtime_facts']) == RUNTIME_FIELDS
                 and type(target['materials']) is dict and set(target['materials']) == MATERIALS,
                 'r02_fact_set')
        targets[platform] = {'slots': ('A', 'B'),
            'runtime_facts': {k: _fact(v) for k, v in target['runtime_facts'].items()},
            'materials': {k: _fact(v) for k, v in target['materials'].items()}}
    return RecipeEnvelope(raw, sha256(raw), doc['module'], freeze(source), freeze(targets))


def _check_matrix(envelopes):
    _require(type(envelopes) is dict and set(envelopes) == {'core', 'api'}, 'r02_target_matrix')
    _require(all(type(x) is RecipeEnvelope and x.module == name and set(x.targets) == PLATFORMS
                 for name, x in envelopes.items()), 'r02_target_matrix')
    _require(envelopes['core'].source == envelopes['api'].source, 'r02_source_identity')


def _runtime_bound(facts):
    missing = [name for name, fact in facts.items() if type(fact) is not BoundFact]
    _require(not missing, 'r02_runtime_unbound', missing)


def _materials(materials):
    missing = [name for name, fact in materials.items() if type(fact) is not BoundFact]
    _require(not missing, 'r02_material_unbound', missing)


def _source_scope(policy):
    _require(type(policy) is dict and set(policy) == {'input_sha256', 'policy_sha256', 'qualification_scope'}
             and policy['qualification_scope'] == 'fixture'
             and all(type(policy[k]) is str and re.fullmatch('[0-9a-f]{64}', policy[k]) is not None
                     for k in ('input_sha256', 'policy_sha256')), 'r02_source_scope')


def _request_types(requests):
    _require(type(requests) is dict and set(requests) == TARGETS, 'r02_input_matrix')
    _require(all(type(x) is SourceInput and type(x.raw) is bytes and callable(x.reader)
                 for x in requests.values()), 'r02_input_source_type')


def _recipe(verified, facts):
    recipe = verified.document['recipe']
    name = facts['entrypoint'].value
    _require(type(name) is str and name == recipe['entrypoint']
             and name in {x['path'] for x in recipe['inventory']}, 'r02_recipe_binding')
    _require(facts['module_descriptor'].value == verified.document['descriptor']
             and facts['context_inventory'].value == verified.document['source']['inventory'],
             'r02_recipe_binding')


def _argv(verified, facts):
    argv, recipe = facts['argv'].value, verified.document['recipe']
    _require(type(argv) is tuple and argv == recipe['argv_template'] and bool(argv), 'r02_recipe_argv')
    _require(argv[0] == recipe['entrypoint'] or
             (len(argv) >= 2 and argv[0] == 'python3' and argv[1] == recipe['entrypoint']
              and recipe['entrypoint'].endswith('.py')), 'r02_recipe_argv')


def _target(verified, key, facts, materials):
    module, platform = key
    toolchain, image = materials['toolchain'].value, materials['image_members'].value
    _require(all(isinstance(x.value, Mapping) for x in materials.values()), 'r02_target_binding')
    abi, members = toolchain.get('abi'), image.get('members')
    _require(type(abi) is str and 0 < len(abi) <= 256 and type(members) is tuple
             and 0 < len(members) <= 256 and all(type(x) is str for x in members)
             and len(set(members)) == len(members), 'r02_target_binding')
    try:
        for member in members:
            relative_path(member)
    except (ValueError, TypeError):
        raise SourceRefusal('r02_target_binding') from None
    _require(verified.target['module_id'] == module and verified.target['oci_platform'] == platform
             and facts['module_version'].value == verified.target['module_version'], 'r02_target_binding')
    _require(materials['base'].value.get('platform') == platform
             and toolchain.get('architecture') == platform.split('/')[1]
             and toolchain.get('engine_id') == verified.requirements['engine']['engine_id']
             and toolchain.get('sha256') == verified.requirements['toolchain']['sha256']
             and image.get('platform') == platform and image.get('abi') == abi,
             'r02_target_binding')


def _custody(verified, reader):
    for row in verified.inventory:
        try:
            actual = reader(row['path'])
        except (OSError, KeyError, ValueError):
            raise SourceRefusal('r02_input_custody') from None
        _require(type(actual) is FileObservation and actual.kind == row['kind']
                 and actual.mode == row['mode'] and actual.link_target == row.get('link_target')
                 and type(actual.data) is bytes and len(actual.data) == row['bytes']
                 and sha256(actual.data) == row['sha256'], 'r02_input_custody')


def _fact_custody(facts, reader):
    for fact in facts.values():
        ref = fact.evidence
        try:
            actual = reader(ref['path'])
        except (OSError, KeyError, ValueError):
            raise SourceRefusal('r02_fact_custody') from None
        _require(type(actual) is FileObservation and actual.kind == ref['kind']
                 and actual.mode == ref['mode'] and actual.link_target is None
                 and type(actual.data) is bytes and len(actual.data) == ref['bytes']
                 and sha256(actual.data) == ref['sha256']
                 and actual.data == canonical_bytes(thaw(fact.value)), 'r02_fact_custody')


def _roots(roots):
    _require(type(roots) is dict and set(roots) == TARGETS, 'r02_source_roots')
    paths = []
    for slots in roots.values():
        _require(type(slots) is dict and set(slots) == {'A', 'B'}, 'r02_source_roots')
        for values in slots.values():
            _require(type(values) is dict and set(values) == ROOTS, 'r02_source_roots')
            for value in values.values():
                _require(type(value) is str and value.startswith('/') and '\x00' not in value
                         and '..' not in value.split('/') and '.' not in value.split('/')
                         and '//' not in value, 'r02_source_roots')
                paths.append(PurePosixPath(value))
    _require(len(set(paths)) == len(paths) and
             not any(a.is_relative_to(b) for a in paths for b in paths if a != b), 'r02_source_roots')


def _limits(limits):
    numeric = {'cpu_millicores', 'memory_bytes', 'disk_bytes', 'input_bytes', 'output_bytes',
               'decode_depth', 'process_limit', 'absolute_timeout_sec'}
    _require(type(limits) is dict and set(limits) == numeric | {'phase_timeouts_sec', 'environment_names'},
             'r02_source_limits')
    _require(all(type(limits[k]) is int and 0 < limits[k] <= 2**63 - 1 for k in numeric)
             and limits['decode_depth'] <= 32 and limits['input_bytes'] <= MAX_SOURCE_BYTES * 16,
             'r02_source_limits')
    phases = limits['phase_timeouts_sec']
    _require(type(phases) is dict and set(phases) == PHASES
             and all(type(v) is int and 0 < v <= limits['absolute_timeout_sec'] for v in phases.values()),
             'r02_source_limits')
    names = limits['environment_names']
    _require(type(names) is list and len(names) <= 256
             and all(type(v) is str and re.fullmatch('[A-Z][A-Z0-9_]{0,127}', v) is not None
                     and re.search('TOKEN|SECRET|PASSWORD|PRIVATE|DSN|CREDENTIAL|API_KEY', v) is None
                     for v in names) and len(set(names)) == len(names), 'r02_source_limits')


def plan_source_recipes(raw_envelopes, requests, fact_reader, roots, limits):
    """Source readers only. Nothing is executed, staged, emitted or authorized."""
    _require(type(raw_envelopes) is dict and set(raw_envelopes) == {'core', 'api'},
             'r02_target_matrix')
    envelopes = {k: parse_source_envelope(raw) for k, raw in raw_envelopes.items()}
    _check_matrix(envelopes)
    # Whole-matrix UNBOUND check precedes any source/owner input reader.
    for env in envelopes.values():
        for target in env.targets.values():
            _runtime_bound(target['runtime_facts'])
            _materials(target['materials'])
    _request_types(requests)
    for request in requests.values():
        _source_scope(request.policy)
    _roots(roots)
    _limits(limits)
    # Hold descriptive arguments before any callback. Source callbacks cannot
    # replace a policy/root/budget after its check; this still grants no effects.
    requests = {key: SourceInput(x.raw, x.reader, dict(x.policy)) for key, x in requests.items()}
    roots = freeze(roots)
    limits = freeze(limits)
    _require(callable(fact_reader), 'r02_fact_custody')
    result = []
    versions = {}
    for key in sorted(TARGETS):
        env = envelopes[key[0]]
        facts = env.targets[key[1]]['runtime_facts']
        materials = env.targets[key[1]]['materials']
        _fact_custody(facts, fact_reader)
        _fact_custody(materials, fact_reader)
        request = requests[key]
        try:
            verified = inputs.admit(request.raw, request.reader, request.policy)
        except (ValueError, OSError, KeyError, TypeError):
            raise SourceRefusal('r02_r01_refused') from None
        _require(not verified.native_qualified and
                 {k: verified.document['source'][k] for k in env.source} == dict(env.source),
                 'r02_source_identity')
        _recipe(verified, facts)
        _argv(verified, facts)
        _target(verified, key, facts, materials)
        _custody(verified, request.reader)
        version = facts['module_version'].value
        _require(versions.setdefault(key[0], version) == version, 'r02_target_binding')
        result.append(SourceRequest(key[0], key[1], verified.input_sha256,
            facts['entrypoint'].value, facts['argv'].value, freeze(roots[key])))
    # Re-observe all facts/inputs after all four selections; no stale early return.
    for key in sorted(TARGETS):
        target = envelopes[key[0]].targets[key[1]]
        _fact_custody(target['runtime_facts'], fact_reader)
        _fact_custody(target['materials'], fact_reader)
        try:
            verified = inputs.admit(requests[key].raw, requests[key].reader, requests[key].policy)
        except (ValueError, OSError, KeyError, TypeError):
            raise SourceRefusal('r02_r01_refused') from None
        _custody(verified, requests[key].reader)
    return SourcePlan(tuple(result), freeze(limits))
