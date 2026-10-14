"""Strict, effect-free config/lock admission against the original-member port.

An AdmissionPlan describes locked argv only. It never grants execution authority.
The production authority is UNBOUND; test doubles cannot change that status.
"""
from dataclasses import dataclass
import ipaddress
import posixpath

from cortex_core.release.codec import sha256
from .contracts import MAX_CONFIG_BYTES, MAX_LOCK_BYTES, RC0_SHA256, decode, load_contracts
from .signed_member import OriginalMember, Refusal, read_member
from .selection import AiSelection
from cortex_core.embeddings.pg_search import EmbeddingIdentity

RUNTIME_LOCK_MEMBER = 'runtime.lock.json'


@dataclass(frozen=True)
class AdmissionPlan:
    target: str
    mode: str
    argv: tuple[str, ...]
    lock_sha256: str
    secret_handles: tuple[str, ...]
    authority_status: str = 'UNBOUND'
    effect_authority: bool = False
    native_qualified: bool = False
    ai: AiSelection | None = None


def _bindings(value):
    if isinstance(value, dict):
        if value.get('state') in ('GAP', 'BOUND'):
            yield value
        for child in value.values():
            yield from _bindings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _bindings(child)


def _private_address(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise Refusal('private_address_required') from None
    if not any(address.version == network.version and address in network
               for network in map(ipaddress.ip_network, ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7'))):
        raise Refusal('private_address_required')


def _paths(value):
    if isinstance(value, dict):
        if 'path' in value:
            yield value['path']
        for child in value.values():
            yield from _paths(child)
    elif isinstance(value, list):
        for child in value:
            yield from _paths(child)


def _identity(lock, config):
    # Identity conflicts have their own refusal even when the conflicting label
    # also has an invalid spelling. Inspect only typed fields before schema use.
    if not isinstance(lock, dict) or not isinstance(config, dict):
        return
    labels = config.get('metrics_labels')
    release = lock.get('release')
    value = release.get('value') if isinstance(release, dict) else None
    if not isinstance(labels, dict) or not isinstance(value, dict):
        return
    if (isinstance(labels.get('deployment_id'), str) and isinstance(config.get('installation_id'), str)
            and labels['deployment_id'] != config['installation_id']
            or isinstance(labels.get('release'), str) and isinstance(value.get('release_id'), str)
            and labels['release'] != value['release_id']):
        raise Refusal('identity_mismatch')


def _image_lock_cycles(lock):
    # RC0-LOCK-001: check BOTH images on BOTH targets, including lexical aliases.
    # This is a path refusal, not evidence of image contents, symlinks or hardlinks.
    # Actual aggregate/member containment/custody is R01 unit 3's responsibility.
    if not isinstance(lock, dict):
        return
    def normalized(value):
        return posixpath.normpath('/' + value.lstrip('/')) if isinstance(value, str) and value.startswith('/') else None
    layout = lock.get('layout')
    lock_path = normalized(layout.get('runtime_lock')) if isinstance(layout, dict) else None
    targets = lock.get('targets')
    if lock_path is None or not isinstance(targets, dict):
        return
    for target in targets.values():
        for component in ('core', 'api'):
            current = target
            for key in (component, 'image', 'value', 'path'):
                current = current.get(key) if isinstance(current, dict) else None
            if normalized(current) == lock_path:
                raise Refusal('image_lock_cycle')


def admit_config(member: OriginalMember, config_raw: bytes, *, aggregate_id: str,
                 observed_target: str, mode: str, operations_raw: bytes | None = None) -> AdmissionPlan:
    # Provenance before parsing candidate bytes or reading package files.
    lock_raw = read_member(member, aggregate_id=aggregate_id, member_name=RUNTIME_LOCK_MEMBER)
    lock = decode(lock_raw, limit=MAX_LOCK_BYTES)
    config = decode(config_raw, limit=MAX_CONFIG_BYTES)
    contracts = load_contracts(operations_raw=operations_raw)
    _image_lock_cycles(lock)
    _identity(lock, config)
    if (next(contracts.lock_validator.iter_errors(lock), None) is not None
            or next(contracts.config_validator.iter_errors(config), None) is not None):
        raise Refusal('bad_shape')
    if any(item['state'] == 'GAP' for item in _bindings([lock, config])):
        raise Refusal('required_binding_gap')
    if (lock['operations_sha256'] != contracts.operations_sha256
            or config['operations_sha256'] != contracts.operations_sha256
            or lock['contract_members']['operations']['value']['sha256'] != contracts.operations_sha256):
        raise Refusal('operations_mismatch')
    if config['runtime_lock_sha256'] != sha256(lock_raw):
        raise Refusal('lock_mismatch')
    for key, filename in (('lock_schema','runtime-lock-v1.schema.json'),
                          ('config_schema','runtime-config-v1.schema.json'),
                          ('operations','runtime-operations-v1.json')):
        descriptor=lock['contract_members'][key]['value']
        if (descriptor['sha256']!=RC0_SHA256[filename]
                or descriptor['bytes']!=contracts.member_sizes[filename]):
            raise Refusal('contract_mismatch')
    if observed_target not in lock['targets'] or config['target'] != observed_target:
        raise Refusal('observed_target_mismatch')
    if config['declaration_scope'] != lock['declaration_scope']:
        raise Refusal('scope_mismatch')
    if len({t['core']['pg_major']['value'] for t in lock['targets'].values()}) != 1:
        raise Refusal('paired_version_mismatch')
    operations = contracts.operations
    if mode not in operations['operation_vectors']:
        raise Refusal('unsupported_mode')
    operation = operations['operation_vectors'][mode]
    if operation['mode'] is not None and config['mode'] != operation['mode']:
        raise Refusal('mode_mismatch')
    names = [role['binding']['value']['sql_name'] for role in config['roles'].values()]
    if len(names) != len(set(names)):
        raise Refusal('role_collision')
    if any(role['binding']['value']['installation_id'] != config['installation_id'] for role in config['roles'].values()):
        raise Refusal('role_installation_mismatch')
    if config['resolver']['owner_binding_receipt'] != lock['owner_ports']['secret_resolver']['original_receipt']:
        raise Refusal('resolver_owner_mismatch')
    if set(config['project_routes']) != set(config['project_repositories']):
        raise Refusal('project_routing_mismatch')
    _private_address(config['database']['host']['value'])
    _private_address(config['listeners']['api']['internal_host']['value'])
    for path in _paths([lock, config]):
        if any(part in ('.', '..') for part in path.split('/')):
            raise Refusal('unsafe_member_path')

    def atom(value):
        if isinstance(value, str):
            return value
        current = {'config': config, 'target': lock['targets'][observed_target]}
        for token in value['field'].split('.'):
            current = current[token]
        if type(current) is not str:
            raise Refusal('argv_mismatch')
        return current
    ai_mode=config.get('ai_mode','hosted')
    identity=None
    if ai_mode=='hosted':
        value=config['embedding']['value']
        identity=EmbeddingIdentity(value['provider'],value['model'],value['model_version'],
                                   value['dimensions'],value['preprocessing'])
    ai=AiSelection(ai_mode,sha256(config_raw),identity)
    handles=tuple(operations['secret_handles_by_mode'].get(mode,()))
    if mode=='api' and ai_mode=='hosted':
        handles+=('provider',)
    return AdmissionPlan(observed_target, mode, tuple(map(atom, operation['argv'])),
                         sha256(lock_raw),handles,ai=ai)
