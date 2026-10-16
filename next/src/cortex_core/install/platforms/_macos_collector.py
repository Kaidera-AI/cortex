"""X2 macOS owner collector behind ONE native seam (accepted X2 note c7fa754c).

`_native_snapshot()` is the single zero-argument native seam. It stays UNBOUND
(required_binding_gap, before any read) until the signed native helper exists and
strict M2/M3 hold on the test MacBook. Bound, it returns one closed snapshot:
the admitted owner state (B1 originals, profile ref, the admitted launch descriptor
and entitlement set) plus TWO bracketing samples of raw facts (launchd job, owner
process, XPC peer with its code-requirement result and audit-token lifetime, owner
code/entitlements, measured caller/backend denials, custody chains).

This module only maps those facts into the existing observation record that
macos.py validates, refusing what that record cannot express: a failed or
substituted peer requirement, any entitlement outside the admitted set, an
ambiguous or absent launchd job, a missing peer lifetime (no PID-only fallback),
a process replaced between samples, a launch descriptor that drifted, a world that
moved between samples. It performs no platform probe or effect and grants nothing:
the result is DATA for macos._installed_owner_inputs.
"""
import hashlib
from dataclasses import replace
import json

from .. import owner

__all__ = ()
SNAPSHOT_SCHEMA = 'cortex.macos-native-snapshot.v1'
_SNAPSHOT = frozenset(('schema', 'admitted', 'samples'))
_ADMITTED = frozenset(('owner_inputs', 'profile_ref', 'observation_path', 'launch', 'entitlements'))
_LAUNCH = frozenset(('label', 'mach_service', 'domain', 'uid', 'argv', 'environment', 'plist_sha256'))
_SAMPLE = frozenset(('platform', 'arch', 'host_identity_sha256', 'owner_epoch', 'session_id', 'launchd', 'owner',
                     'xpc', 'code', 'denials', 'custody'))
_JOB = frozenset(('label', 'domain', 'uid', 'pid', 'mach_services', 'argv', 'environment', 'plist_sha256'))
_PROCESS = frozenset(('pid', 'uids', 'groups', 'lifetime'))
_XPC = frozenset(('requirement_rc', 'requirement', 'sender_valid', 'peer'))
_CODE = frozenset(('owner_closure', 'owner_requirement_satisfied', 'complete', 'isolated_startup',
                   'site_hooks_disabled', 'loader_inputs_fixed', 'candidate_imports', 'dependency_repair', 'entitlements'))
_DENIALS = frozenset(('caller_launch', 'caller_debug', 'caller_elevate', 'backend_bypass'))


def _native_snapshot():
    # The one native seam: the signed helper (XPC peer requirement, audit-token
    # lifetime, launchd job, code signing, custody, denial probes). UNBOUND.
    raise owner.OwnerRefusal('required_binding_gap')


def _closed(value, keys):
    return type(value) is dict and set(value) == keys


def _encode(value):
    # The Gate B original encoding: sorted keys, compact, UTF-8, one trailing newline.
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()


def _str(value):
    return type(value) is str


def _int(value):
    return type(value) is int  # exact: a bool or an integral float is NOT an int (Vera X2M-001)


def _strs(value):
    return type(value) is list and all(type(v) is str for v in value)


def _ints(value):
    return type(value) is list and all(type(v) is int for v in value)


def _process(value):
    return (_closed(value, _PROCESS) and _int(value['pid']) and _ints(value['uids']) and _ints(value['groups'])
            and _str(value['lifetime']))


def _launch(value, keys):
    return (_closed(value, keys) and all(_str(value[k]) for k in ('label', 'domain', 'plist_sha256')) and
            _int(value['uid']) and _strs(value['argv']) and _strs(value['environment']) and
            (_int(value['pid']) and _strs(value['mach_services']) if 'pid' in keys else _str(value['mach_service'])))


def _sample(value):
    return (_closed(value, _SAMPLE)
        and all(_str(value[k]) for k in ('platform', 'arch', 'host_identity_sha256', 'owner_epoch', 'session_id'))
        and type(value['launchd']) is list and all(_launch(job, _JOB) for job in value['launchd'])
        and _process(value['owner']) and _closed(value['xpc'], _XPC) and _int(value['xpc']['requirement_rc'])
        and _closed(value['xpc']['requirement'], {'requirement_id', 'revision'})
        and _str(value['xpc']['requirement']['requirement_id']) and _int(value['xpc']['requirement']['revision'])
        and type(value['xpc']['sender_valid']) is bool
        and (value['xpc']['peer'] is None or _process(value['xpc']['peer']))
        and _closed(value['code'], _CODE) and type(value['code']['owner_closure']) is dict
        and all(type(value['code'][k]) is bool for k in _CODE - {'owner_closure', 'entitlements'})
        and type(value['code']['entitlements']) is dict
        and _closed(value['denials'], _DENIALS) and all(_str(v) for v in value['denials'].values())
        and type(value['custody']) is dict)


def _shape(snapshot):
    """The closed snapshot schema with EXACT leaf types, checked before any mapping."""
    admitted = snapshot.get('admitted') if type(snapshot) is dict else None
    samples = snapshot.get('samples') if type(snapshot) is dict else None
    return (_closed(snapshot, _SNAPSHOT) and snapshot['schema'] == SNAPSHOT_SCHEMA
        and _closed(admitted, _ADMITTED) and type(admitted['owner_inputs']) is owner._Inputs
        and _str(admitted['observation_path']) and _launch(admitted['launch'], _LAUNCH)
        and type(admitted['entitlements']) is dict
        and type(samples) is list and len(samples) == 2 and all(_sample(s) for s in samples))


def collect():
    """Re-observe through the native seam and return the macOS carrier DATA."""
    from .macos import _MacOSInputs, _same  # exact DATA equality: JSON true is not 1

    snapshot = _native_snapshot()  # mandatory binding before any read
    owner._require(_shape(snapshot), 'owner_unavailable')
    admitted, (first, second) = snapshot['admitted'], snapshot['samples']
    inputs = admitted['owner_inputs']
    # A process replaced between the bracketing samples is a different peer/owner;
    # any other movement, custody included, is drift (HOLD) - decided here, because
    # moved bytes would otherwise surface first as the session's pinned-ref mismatch.
    owner._require(_same(first['owner'], second['owner']) and _same(first['xpc']['peer'], second['xpc']['peer']),
                   'peer_denied')
    owner._require(all(_same(first[k], second[k]) for k in _SAMPLE), 'custody_changed')
    owner._require(first['xpc']['peer'] is not None, 'owner_unavailable')  # no PID-only fallback
    profile = owner._original(inputs, admitted['profile_ref'])
    owner._require(type(profile) is dict and type(profile.get('host_identity_ref')) is dict, 'owner_unavailable')
    owner._require(_same(first['host_identity_sha256'], profile['host_identity_ref'].get('sha256')), 'peer_denied')
    launch = admitted['launch']
    jobs = [job for job in first['launchd'] if _same(job['label'], launch['label'])]
    owner._require(len(jobs) != 0, 'owner_unavailable')
    owner._require(len(jobs) == 1, 'peer_denied')  # ambiguous launch identity
    job = jobs[0]
    owner._require(_same(job['plist_sha256'], launch['plist_sha256']), 'custody_changed')  # descriptor drift
    owner._require(_same(job['mach_services'], [launch['mach_service']]), 'peer_denied')  # the one fixed endpoint
    xpc = first['xpc']
    owner._require(_same(xpc['requirement_rc'], 0) and _same(xpc['requirement'], owner._original(inputs, profile.get('peer_code_ref'))),
                   'peer_denied')  # requirement set (rc checked before resume) and the accepted one
    owner._require(_same(first['code']['entitlements'], {}) and _same(admitted['entitlements'], {}), 'peer_denied')  # empty set only
    denied = {name: first['denials'][name] == 'denied' for name in _DENIALS}
    observation = {
        'platform': first['platform'], 'arch': first['arch'], 'host_identity_ref': profile['host_identity_ref'],
        'owner_epoch': first['owner_epoch'], 'session_id': first['session_id'],
        'launch': {'launch_id': job['label'], 'endpoint_id': job['mach_services'][0], 'system_scope': job['domain'] == 'system',
                   'owner_uid': job['uid'], 'fixed_executable_ref': profile.get('code_ref'),
                   'caller_controls_launch': not denied['caller_launch'], 'caller_can_debug_owner': not denied['caller_debug'],
                   'caller_can_elevate_to_owner': not denied['caller_elevate'],
                   'caller_can_bypass_owner': not denied['backend_bypass'],
                   'startup_inputs_fixed': _same(job['argv'], launch['argv']) and _same(job['environment'], [])
                                           and _same(launch['environment'], [])},
        'owner_process': dict(first['owner'], provenance='protected_launch' if first['owner']['pid'] == job['pid']
                              else 'unbound_process'),
        'peer_process': dict(xpc['peer'], provenance='kernel_peer_lifetime' if xpc['sender_valid'] is True
                             else 'unbound_process'),
        'code': dict({k: v for k, v in first['code'].items() if k != 'entitlements'},
                     peer_requirement_ref=profile.get('peer_code_ref'), peer_requirement_satisfied=xpc['sender_valid'] is True),
        'custody_before': first['custody'], 'custody_after': second['custody']}
    raw = _encode(observation)
    path = admitted['observation_path']
    owner._require(path not in inputs.originals, 'owner_unavailable')  # never overwrite an admitted original
    reference = {'path': path, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    originals = dict(inputs.originals)
    originals[path] = raw
    return _MacOSInputs(replace(inputs, originals=originals),
                        admitted['profile_ref'], reference)
