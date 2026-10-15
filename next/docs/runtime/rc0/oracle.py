"""Offline RC0 declaration oracle. No product exports or runtime authority.

Future RC2 imports the sole R01 codec/owner readers; this documentation specimen
does not replace them. It only checks authored schema/selection obligations.
"""
import hashlib
import ipaddress
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / 'contracts/runtime'
DOCS = Path(__file__).resolve().parent
MAX_LOCK_BYTES = 262144
MAX_CONFIG_BYTES = 65536


class Refusal(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def encode(value):
    return (json.dumps(value, indent=2, ensure_ascii=True) + '\n').encode()


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else encode(value)).hexdigest()


def decode(raw, *, byte_limit=MAX_LOCK_BYTES):
    if not isinstance(raw, bytes) or len(raw) > byte_limit:
        raise Refusal('raw_limit')
    if raw.startswith(b'\xef\xbb\xbf'):
        raise Refusal('raw_shape')

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Refusal('raw_shape')
            result[key] = value
        return result

    def number(_):
        raise Refusal('raw_shape')

    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                           parse_float=number, parse_constant=number)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise Refusal('raw_shape') from None

    def walk(item, depth):
        if depth > 32:
            raise Refusal('raw_limit')
        if isinstance(item, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in item):
            raise Refusal('raw_shape')
        if isinstance(item, dict):
            for key, child in item.items():
                walk(key, depth + 1)
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
    walk(value, 0)
    return value


def bindings(value):
    if isinstance(value, dict):
        if value.get('state') in ('GAP', 'BOUND'):
            yield value
        for child in value.values():
            yield from bindings(child)
    elif isinstance(value, list):
        for child in value:
            yield from bindings(child)


def private_address(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise Refusal('private_address_required') from None
    allowed = [ipaddress.ip_network(x) for x in
               ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7')]
    if not any(address.version == network.version and address in network for network in allowed):
        raise Refusal('private_address_required')


class Oracle:
    def __init__(self):
        self.lock_schema = decode((CONTRACTS / 'runtime-lock-v1.schema.json').read_bytes())
        self.config_schema = decode((CONTRACTS / 'runtime-config-v1.schema.json').read_bytes())
        self.operations = decode((CONTRACTS / 'runtime-operations-v1.json').read_bytes(), byte_limit=MAX_CONFIG_BYTES)
        self.expected = decode((DOCS / 'fixtures/operations.expected.json').read_bytes(), byte_limit=MAX_CONFIG_BYTES)
        Draft202012Validator.check_schema(self.lock_schema)
        Draft202012Validator.check_schema(self.config_schema)
        self.lock_validator = Draft202012Validator(self.lock_schema)
        self.config_validator = Draft202012Validator(self.config_schema)

    def shape(self, lock, config):
        if list(self.lock_validator.iter_errors(lock)) or list(self.config_validator.iter_errors(config)):
            raise Refusal('bad_shape')

    def select(self, lock, config, operations, *, observed_target, mode):
        # Raw reconstruction is for this inert oracle; RC2 validates original bytes
        # with existing R01 codecs, not this documentation encoder.
        lock = decode(encode(lock))
        config = decode(encode(config), byte_limit=MAX_CONFIG_BYTES)
        operations = decode(encode(operations), byte_limit=MAX_CONFIG_BYTES)
        self.shape(lock, config)
        if operations != self.expected or sha(operations) != lock['operations_sha256']:
            raise Refusal('operations_mismatch')
        if any(item['state'] == 'GAP' for item in bindings([lock, config])):
            raise Refusal('required_binding_gap')
        if any(t['api']['image']['value']['path'] == lock['layout']['runtime_lock'] for t in lock['targets'].values()):
            raise Refusal('image_lock_cycle')
        if config['runtime_lock_sha256'] != sha(lock):
            raise Refusal('lock_mismatch')
        if config['target'] != observed_target:
            raise Refusal('observed_target_mismatch')
        if config['declaration_scope'] != lock['declaration_scope']:
            raise Refusal('scope_mismatch')
        if config['operations_sha256'] != sha(operations):
            raise Refusal('operations_mismatch')
        if lock['contract_members']['operations']['value']['sha256'] != sha(operations):
            raise Refusal('operations_mismatch')
        if len({t['core']['pg_major']['value'] for t in lock['targets'].values()}) != 1:
            raise Refusal('paired_version_mismatch')
        if mode not in operations['operation_vectors']:
            raise Refusal('unsupported_mode')
        op = operations['operation_vectors'][mode]
        if op['mode'] is not None and config['mode'] != op['mode']:
            raise Refusal('mode_mismatch')
        names = [role['binding']['value']['sql_name'] for role in config['roles'].values()]
        if len(names) != len(set(names)):
            raise Refusal('role_collision')
        if any(role['binding']['value']['installation_id'] != config['installation_id'] for role in config['roles'].values()):
            raise Refusal('role_installation_mismatch')
        if config['resolver']['owner_binding_receipt'] != lock['owner_ports']['secret_resolver']['original_receipt']:
            raise Refusal('resolver_owner_mismatch')
        if config['metrics_labels']['deployment_id'] != config['installation_id'] or config['metrics_labels']['release'] != lock['release']['value']['release_id']:
            raise Refusal('identity_mismatch')
        if set(config['project_routes']) != set(config['project_repositories']):
            raise Refusal('project_routing_mismatch')
        private_address(config['database']['host']['value'])
        private_address(config['listeners']['api']['internal_host']['value'])
        for item in bindings([lock, config]):
            value = item.get('value')
            if isinstance(value, dict) and 'path' in value:
                if any(part in ('.', '..') for part in value['path'].split('/')):
                    raise Refusal('unsafe_member_path')

        def atom(value):
            if isinstance(value, str):
                return value
            current = {'config': config, 'target': lock['targets'][observed_target]}
            for token in value['field'].split('.'):
                current = current[token]
            if not isinstance(current, str):
                raise Refusal('argv_mismatch')
            return current
        return {'selection':'DECLARATION_COMPLETE','declaration_scope':lock['declaration_scope'],
                'target':observed_target,'mode':mode,'argv':[atom(x) for x in op['argv']],
                'effect_authority':False,'native_qualified':False}
