"""Effect-free option A config; the existing decoder, one closed authority file."""
from dataclasses import dataclass
import ipaddress
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from uuid import UUID

from .contracts import decode, MAX_CONFIG_BYTES
from .credentials import read_private_file
from .signed_member import Refusal

MODE_PURPOSES = MappingProxyType({
    'serve': ('api_request', 'probe'), 'migrate': ('migrator',),
    'seed': ('bootstrap', 'installer_enrollment', 'api_request'),
    'status': (), 'client': (),
})
ROLE_NAMES = frozenset({'bootstrap', 'migrator', 'api_request', 'probe', 'installer_enrollment'})
NAME = re.compile(r'[a-zA-Z_][a-zA-Z_0-9-]{0,62}\Z')
REF = re.compile(r'secrets/cortex/[a-zA-Z0-9_-]{1,96}\Z')


@dataclass(frozen=True)
class Role:
    login: str
    secret_ref: str


@dataclass(frozen=True)
class BootstrapConfig:
    installation_id: str
    tenant_id: str
    project_id: str
    project_names: object
    ai_mode: str
    vault_root: str
    bind: object
    publish: object
    database: object
    roles: object
    limits: object
    mode: str
    client_ref: str | None = None


def _shape(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise Refusal('config_shape')


def _integer(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise Refusal('config_integer')


def _identifier(value):
    if type(value) is not str or NAME.fullmatch(value) is None:
        raise Refusal('config_identifier')


def _reference(value):
    if type(value) is not str or REF.fullmatch(value) is None:
        raise Refusal('config_reference')


def _uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value or UUID(value).int == 0:
            raise ValueError
    except (ValueError, AttributeError):
        raise Refusal('config_identity') from None


def _listener(value, *, loopback):
    _shape(value, {'host', 'port'})
    _integer(value['port'], 1, 65535)
    try:
        host = ipaddress.ip_address(value['host'])
    except (ValueError, TypeError):
        raise Refusal('config_binding') from None
    if (type(value['host']) is not str or host.is_multicast
            or (loopback and not host.is_loopback)
            or (not loopback and not (host.is_unspecified or host.is_private or host.is_loopback))):
        raise Refusal('config_binding')
    return MappingProxyType(dict(value))


def validate_bootstrap(value, *, mode):
    if type(mode) is not str or mode not in MODE_PURPOSES:
        raise Refusal('config_mode')
    _shape(value, {'version', 'installation_id', 'tenant_id', 'project_id', 'project_names',
                   'ai_mode', 'vault_root', 'bind', 'publish', 'client', 'database', 'roles', 'limits'})
    if type(value['version']) is not int or value['version'] != 1:
        raise Refusal('config_version')
    if value['ai_mode'] != 'off':
        raise Refusal('unshipped_provider_refusal')
    ids = [value[name] for name in ('installation_id', 'tenant_id', 'project_id')]
    for identity in ids:
        _uuid(identity)
    if len(set(ids)) != 3:
        raise Refusal('config_identity')
    names = value['project_names']
    if type(names) is not dict or not 1 <= len(names) <= 256:
        raise Refusal('config_projects')
    for name, identity in names.items():
        _identifier(name); _uuid(identity)
    if len(set(names.values())) != len(names) or value['project_id'] not in names.values():
        raise Refusal('config_projects')
    root = value['vault_root']
    if (type(root) is not str or not 1 <= len(root.encode('utf-8')) <= 4096
            or root == '/' or not root.startswith('/') or root.startswith('//')
            or str(PurePosixPath(root)) != root
            or '..' in PurePosixPath(root).parts):
        raise Refusal('config_vault')
    bind = _listener(value['bind'], loopback=False)
    publish = _listener(value['publish'], loopback=True)
    client = value['client']; _shape(client, {'credential_ref'}); _reference(client['credential_ref'])
    database = value['database']; _shape(database, {'host', 'port', 'name', 'tls'})
    host = database['host']
    if type(host) is not str or not 1 <= len(host) <= 253:
        raise Refusal('config_database')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Legacy inet_aton spellings must not acquire DNS-name admission after
        # the canonical IP parser refuses them. No DNS or connection probe.
        labels = host.split('.')
        if (all(re.fullmatch(r'(?:0[xX][0-9a-fA-F]+|[0-9]+)', label) for label in labels)
                or any(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', label) is None
                       for label in labels)):
            raise Refusal('config_database') from None
    else:
        networks = map(ipaddress.ip_network, ('127.0.0.0/8', '::1/128', '10.0.0.0/8',
                       '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7'))
        if not any(address.version == net.version and address in net for net in networks):
            raise Refusal('config_database')
    _integer(database['port'], 1, 65535); _identifier(database['name'])
    tls = database['tls']; _shape(tls, {'mode', 'root_cert_ref'}); _reference(tls['root_cert_ref'])
    if tls['mode'] != 'verify-full':
        raise Refusal('config_transport_unratified')
    roles = value['roles']; _shape(roles, ROLE_NAMES)
    for role in roles.values():
        _shape(role, {'login', 'secret_ref'}); _identifier(role['login']); _reference(role['secret_ref'])
    if (len({r['login'] for r in roles.values()}) != len(roles)
            or len({r['secret_ref'] for r in roles.values()} | {client['credential_ref'], tls['root_cert_ref']}) != len(roles) + 2):
        raise Refusal('config_role_collision')
    limits = value['limits']; _shape(limits, {'request_seconds', 'shutdown_seconds'})
    for seconds in limits.values():
        _integer(seconds, 1, 120)
    admitted = MappingProxyType({name: Role(**roles[name]) for name in MODE_PURPOSES[mode]})
    return BootstrapConfig(*ids, MappingProxyType(dict(names)), 'off', root, bind, publish,
                           MappingProxyType({**database, 'tls': MappingProxyType(dict(tls))}),
                           admitted, MappingProxyType(dict(limits)), mode,
                           client['credential_ref'] if mode == 'client' else None)


def decode_config(raw, *, mode):
    return validate_bootstrap(decode(raw, limit=MAX_CONFIG_BYTES), mode=mode)


def read_config_file(path, *, mode):
    # Unknown mode refuses even before file access; no implicit purpose widening.
    if type(mode) is not str or mode not in MODE_PURPOSES:
        raise Refusal('config_mode')
    return decode_config(read_private_file(path, limit=MAX_CONFIG_BYTES), mode=mode)
