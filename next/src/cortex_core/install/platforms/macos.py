"""Fixed macOS observer contracts: source DATA, never native/effect authority.

The native protected-launch/kernel-peer/code/custody supplier is UNBOUND. This
module performs no platform probe, code-signing call, provisioning or effect.
Its private supplier must eventually be independently admitted inside V's fixed
protected closure. C cannot supply these records or select a provider/root.
Python records, provenance labels and hashes cannot establish that admission.

The existing B1 observer may delegate here in a later accepted composition.
It is not installed or rebound on import. P2a still consumes only B1's existing
propose_effect(original_request) -> OwnerProposal PRECONSUME_DATA interface.
The original peer record binds macos_profile_ref and macos_observation_ref using
the existing RecordRef grammar. B1's unchanged peer_ref comparison therefore
also binds these native DATA subjects across audit; no context field is added.
The reference graph is acyclic: these native subjects do not refer to the peer.
"""
from dataclasses import dataclass
from pathlib import PurePosixPath
import re

from .. import owner

__all__ = ()
_MAX = 2**63 - 1
_PROFILE = frozenset(('platform arch host_identity_ref owner_uid caller_uid launch_id '
    'endpoint_id code_ref peer_code_ref custody_ref').split())
_OBSERVATION = frozenset(('platform arch host_identity_ref owner_epoch session_id launch '
    'owner_process peer_process code custody_before custody_after').split())
_LAUNCH = frozenset(('launch_id endpoint_id system_scope owner_uid fixed_executable_ref '
    'caller_controls_launch caller_can_debug_owner caller_can_elevate_to_owner '
    'caller_can_bypass_owner startup_inputs_fixed').split())
_PROCESS = frozenset(('pid lifetime uids groups provenance').split())
_CODE = frozenset(('owner_closure peer_requirement_ref owner_requirement_satisfied '
    'peer_requirement_satisfied complete isolated_startup site_hooks_disabled '
    'loader_inputs_fixed candidate_imports dependency_repair').split())
_COMPONENTS = ('executable', 'imports', 'native', 'crypto', 'schemas')
_ROLES = frozenset(('launch endpoint_policy code imports native crypto schemas policy '
    'state journal lock originals backend_policy').split())
_NODE = frozenset(('path device inode uid gid mode kind links mount_id owners_enabled '
    'symlink acl_write_uids acl_write_gids caller_can_replace sha256').split())


@dataclass(frozen=True)
class _MacOSInputs:
    """Private original-record carrier, not a provenance seal or caller API."""
    owner_inputs: owner._Inputs
    profile_ref: dict
    observation_ref: dict


def _installed_macos_inputs():
    # One fixed private native composition. No environment, search or test mode.
    # X2: the private collector behind its one native seam, UNBOUND until the
    # signed helper exists (required_binding_gap before any read).
    from ._macos_collector import collect
    return collect()


def _closed(value, keys):
    return type(value) is dict and set(value) == keys


def _number(value, minimum=0):
    return type(value) is int and minimum <= value <= _MAX


def _atom(value):
    return type(value) is str and 0 < len(value.encode('utf-8')) <= 128


def _same(left, right):
    """Exact DATA comparison: JSON true is not the integer 1."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(_same(left[k], right[k]) for k in left)
    if type(left) is list:
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _carrier_valid(value):
    if type(value) is not _MacOSInputs or type(value.owner_inputs) is not owner._Inputs:
        return False
    inputs = value.owner_inputs
    return (type(inputs.context_raw) is bytes and len(inputs.context_raw) <= 262144
        and type(inputs.current_clock_raw) is bytes and len(inputs.current_clock_raw) <= 262144
        and type(inputs.originals) is dict and len(inputs.originals) <= 256
        and all(type(k) is str and type(v) is bytes and len(v) <= 262144
                for k, v in inputs.originals.items())
        and sum(len(v) for v in inputs.originals.values()) <= 8 * 1024 * 1024)


def _profile_valid(profile):
    return (_closed(profile, _PROFILE)
        and profile['platform'] == 'darwin' and profile['arch'] == 'arm64'
        and _number(profile['owner_uid']) and _number(profile['caller_uid'])
        and profile['owner_uid'] != profile['caller_uid']
        and _atom(profile['launch_id']) and _atom(profile['endpoint_id']))


def _binding_valid(profile, observed, context, peer):
    if type(context) is not dict or type(peer) is not dict or type(context.get('target')) is not dict:
        return False
    target = context['target']
    return (observed['platform'] == profile['platform'] and observed['arch'] == profile['arch']
        and target.get('host_os') == 'darwin' and target.get('host_arch') == 'arm64'
        and target.get('backend_platform') == 'linux/arm64'
        and _same(profile['host_identity_ref'], observed['host_identity_ref'])
        and _same(profile['host_identity_ref'], peer.get('host_identity_ref'))
        and _atom(context.get('owner_epoch')) and _atom(context.get('session_id'))
        and context['owner_epoch'] == observed['owner_epoch'] == peer.get('owner_epoch')
        and context['session_id'] == observed['session_id'] == peer.get('session_id'))


def _launch_valid(profile, value):
    return (_closed(value, _LAUNCH)
        and value['launch_id'] == profile['launch_id']
        and value['endpoint_id'] == profile['endpoint_id']
        and _number(value['owner_uid']) and value['owner_uid'] == profile['owner_uid']
        and _same(value['fixed_executable_ref'], profile['code_ref'])
        and value['system_scope'] is True and value['startup_inputs_fixed'] is True
        and all(value[k] is False for k in ('caller_controls_launch', 'caller_can_debug_owner',
            'caller_can_elevate_to_owner', 'caller_can_bypass_owner')))


def _process_valid(value, uid, provenance):
    return (_closed(value, _PROCESS) and _number(value['pid'], 1) and _atom(value['lifetime'])
        and type(value['uids']) is list and len(value['uids']) == 3
        and all(_number(v) and v == uid for v in value['uids'])
        and type(value['groups']) is list and 0 < len(value['groups']) <= 64
        and all(_number(v) for v in value['groups'])
        and len(set(value['groups'])) == len(value['groups'])
        and value['provenance'] == provenance)


def _peer_valid(profile, observed, peer):
    actual_owner, actual_peer = observed['owner_process'], observed['peer_process']
    return (_process_valid(actual_owner, profile['owner_uid'], 'protected_launch')
        and _process_valid(actual_peer, profile['caller_uid'], 'kernel_peer_lifetime')
        and actual_owner['pid'] != actual_peer['pid']
        and actual_owner['lifetime'] != actual_peer['lifetime']
        and actual_peer['lifetime'] == peer.get('lifetime')
        and _number(peer.get('owner_uid')) and peer['owner_uid'] == profile['caller_uid'])


def _digest(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _closure_valid(value):
    if not _closed(value, {'interpreter', 'packages', 'components'}):
        return False
    return (_same(value['interpreter'], {'implementation': 'CPython', 'version': '3.12.15'})
        and _same(value['packages'], {'cryptography': '50.0.0', 'cffi': '2.1.1', 'pycparser': '3.0'})
        and type(value['components']) is list and len(value['components']) == len(_COMPONENTS)
        and all(_closed(v, {'role', 'sha256'}) and v['role'] == role and _digest(v['sha256'])
            for v, role in zip(value['components'], _COMPONENTS)))


def _code_valid(profile, actual, expected, peer_requirement):
    return (_closure_valid(expected)
        and _closed(peer_requirement, {'requirement_id', 'revision'})
        and _atom(peer_requirement['requirement_id']) and _number(peer_requirement['revision'], 1)
        and _closed(actual, _CODE) and _same(actual['owner_closure'], expected)
        and _same(actual['peer_requirement_ref'], profile['peer_code_ref'])
        and all(actual[k] is True for k in ('owner_requirement_satisfied',
            'peer_requirement_satisfied', 'complete', 'isolated_startup',
            'site_hooks_disabled', 'loader_inputs_fixed'))
        and actual['candidate_imports'] is False and actual['dependency_repair'] is False)


def _prefixes(path):
    # Pure lexical inspection only. No resolving, following or opening paths.
    if (type(path) is not str or not path.startswith('/') or path.startswith('//')
            or len(path.encode('utf-8')) > 4096 or '\x00' in path
            or any(v in ('.', '..') for v in path.split('/'))):
        return None
    value = PurePosixPath(path)
    if str(value) != path:
        return None
    parts = value.parts[1:]
    return ['/'] + ['/' + '/'.join(parts[:i]) for i in range(1, len(parts) + 1)]


def _node_safe(node, owner_uid, caller_uid):
    if not _closed(node, _NODE):
        return False
    return (_number(node['device']) and _number(node['inode'], 1)
        and _number(node['uid']) and node['uid'] in (0, owner_uid) and node['uid'] != caller_uid
        and _number(node['gid']) and type(node['mode']) is int and 0 <= node['mode'] <= 0o7777
        and node['mode'] & 0o6022 == 0
        and node['kind'] in ('file', 'directory') and _number(node['links'], 1)
        and (node['kind'] == 'directory' or node['links'] == 1)
        and _atom(node['mount_id']) and node['owners_enabled'] is True
        and node['symlink'] is False and node['caller_can_replace'] is False
        and type(node['acl_write_uids']) is list and len(node['acl_write_uids']) <= 64
        and all(_number(v) and v in (0, owner_uid) and v != caller_uid for v in node['acl_write_uids'])
        and len(set(node['acl_write_uids'])) == len(node['acl_write_uids'])
        and type(node['acl_write_gids']) is list and node['acl_write_gids'] == []
        and (node['sha256'] is None if node['kind'] == 'directory' else _digest(node['sha256'])))


def _custody_safe(value, profile):
    if not _closed(value, {'chains'}) or type(value['chains']) is not list or len(value['chains']) != len(_ROLES):
        return False
    roles = []
    for chain in value['chains']:
        if not _closed(chain, {'role', 'nodes'}) or type(chain['role']) is not str:
            return False
        roles.append(chain['role'])
        nodes = chain['nodes']
        if type(nodes) is not list or not 1 <= len(nodes) <= 128:
            return False
        if not all(_node_safe(n, profile['owner_uid'], profile['caller_uid']) for n in nodes):
            return False
        if [n['path'] for n in nodes] != _prefixes(nodes[-1]['path']):
            return False
        if any(n['kind'] != 'directory' for n in nodes[:-1]):
            return False
    return len(set(roles)) == len(roles) and set(roles) == _ROLES


def _custody_valid(profile, before, after, expected):
    # One conjunctive custody guard; no redundant independently credited check.
    return (_custody_safe(expected, profile) and _custody_safe(before, profile)
        and _custody_safe(after, profile) and _same(before, expected) and _same(after, before))


def _installed_owner_inputs():
    """Reobserve through the fixed supplier, then return only fresh B1 DATA.

    B1 re-enters this observer after R01 audit. P2a still owns durable current
    reserve/intent/CAS/consume/result, final enforcement and native admission.
    """
    try:
        packet = _installed_macos_inputs()  # mandatory binding before all reads
        owner._require(_carrier_valid(packet), 'owner_unavailable')  # BM carrier
        inputs = packet.owner_inputs
        profile = owner._original(inputs, packet.profile_ref)
        observed = owner._original(inputs, packet.observation_ref)
        owner._require(_profile_valid(profile), 'owner_unavailable')  # BM profile
        owner._require(_closed(observed, _OBSERVATION), 'owner_unavailable')  # BM shape
        context = owner._decode(inputs.context_raw, 'owner_unavailable')
        peer = owner._original(inputs, context.get('peer_ref'))
        owner._original(inputs, profile['host_identity_ref'])
        owner._require(_binding_valid(profile, observed, context, peer), 'peer_denied')  # BM binding
        owner._require(_same(peer.get('macos_profile_ref'), packet.profile_ref) and _same(peer.get('macos_observation_ref'), packet.observation_ref), 'peer_denied')  # BM native_refs
        owner._require(_launch_valid(profile, observed['launch']), 'peer_denied')  # BM launch
        owner._require(_peer_valid(profile, observed, peer), 'peer_denied')  # BM peer
        expected_code = owner._original(inputs, profile['code_ref'])
        peer_requirement = owner._original(inputs, profile['peer_code_ref'])
        owner._require(_code_valid(profile, observed['code'], expected_code, peer_requirement), 'peer_denied')  # BM code
        with owner._current_observation(inputs):
            expected_custody = owner._original(inputs, profile['custody_ref'])
            owner._require(_custody_valid(profile, observed['custody_before'], observed['custody_after'], expected_custody), 'custody_changed')  # BM custody
        return owner._Inputs(inputs.context_raw, dict(inputs.originals), inputs.current_clock_raw, inputs.aggregate_id)
    except owner.OwnerRefusal:
        raise
    except Exception:
        raise owner.OwnerRefusal('owner_unavailable') from None
