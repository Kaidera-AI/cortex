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


def _shape(snapshot):
    admitted = snapshot.get('admitted') if type(snapshot) is dict else None
    samples = snapshot.get('samples') if type(snapshot) is dict else None
    return (_closed(snapshot, _SNAPSHOT) and snapshot['schema'] == SNAPSHOT_SCHEMA
        and _closed(admitted, _ADMITTED) and type(admitted['owner_inputs']) is owner._Inputs
        and type(admitted['observation_path']) is str and _closed(admitted['launch'], _LAUNCH)
        and type(admitted['entitlements']) is dict
        and type(samples) is list and len(samples) == 2
        and all(_closed(s, _SAMPLE) and type(s['launchd']) is list and all(_closed(j, _JOB) for j in s['launchd'])
                and _closed(s['owner'], _PROCESS) and _closed(s['xpc'], _XPC)
                and (s['xpc']['peer'] is None or _closed(s['xpc']['peer'], _PROCESS))
                and _closed(s['code'], _CODE) and type(s['code']['entitlements']) is dict
                and _closed(s['denials'], _DENIALS) for s in samples))


def collect():
    """Re-observe through the native seam and return the macOS carrier DATA."""
    from .macos import _MacOSInputs

    snapshot = _native_snapshot()  # mandatory binding before any read
    owner._require(_shape(snapshot), 'owner_unavailable')
    admitted, (first, second) = snapshot['admitted'], snapshot['samples']
    inputs = admitted['owner_inputs']
    # A process replaced between the bracketing samples is a different peer/owner;
    # any other movement, custody included, is drift (HOLD) - decided here, because
    # moved bytes would otherwise surface first as the session's pinned-ref mismatch.
    owner._require(first['owner'] == second['owner'] and first['xpc']['peer'] == second['xpc']['peer'], 'peer_denied')
    owner._require(all(first[k] == second[k] for k in _SAMPLE), 'custody_changed')
    owner._require(first['xpc']['peer'] is not None, 'owner_unavailable')  # no PID-only fallback
    profile = owner._original(inputs, admitted['profile_ref'])
    owner._require(type(profile) is dict and type(profile.get('host_identity_ref')) is dict, 'owner_unavailable')
    owner._require(first['host_identity_sha256'] == profile['host_identity_ref'].get('sha256'), 'peer_denied')
    launch = admitted['launch']
    jobs = [job for job in first['launchd'] if job['label'] == launch['label']]
    owner._require(len(jobs) != 0, 'owner_unavailable')
    owner._require(len(jobs) == 1, 'peer_denied')  # ambiguous launch identity
    job = jobs[0]
    owner._require(job['plist_sha256'] == launch['plist_sha256'], 'custody_changed')  # descriptor drift
    owner._require(job['mach_services'] == [launch['mach_service']], 'peer_denied')  # the one fixed endpoint
    xpc = first['xpc']
    owner._require(xpc['requirement_rc'] == 0 and xpc['requirement'] == owner._original(inputs, profile.get('peer_code_ref')),
                   'peer_denied')  # requirement set (rc checked before resume) and the accepted one
    owner._require(first['code']['entitlements'] == admitted['entitlements'] == {}, 'peer_denied')  # empty set only
    denied = {name: first['denials'][name] == 'denied' for name in _DENIALS}
    observation = {
        'platform': first['platform'], 'arch': first['arch'], 'host_identity_ref': profile['host_identity_ref'],
        'owner_epoch': first['owner_epoch'], 'session_id': first['session_id'],
        'launch': {'launch_id': job['label'], 'endpoint_id': job['mach_services'][0], 'system_scope': job['domain'] == 'system',
                   'owner_uid': job['uid'], 'fixed_executable_ref': profile.get('code_ref'),
                   'caller_controls_launch': not denied['caller_launch'], 'caller_can_debug_owner': not denied['caller_debug'],
                   'caller_can_elevate_to_owner': not denied['caller_elevate'],
                   'caller_can_bypass_owner': not denied['backend_bypass'],
                   'startup_inputs_fixed': job['argv'] == launch['argv'] and job['environment'] == launch['environment'] == []},
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
    return _MacOSInputs(owner._Inputs(inputs.context_raw, originals, inputs.current_clock_raw, inputs.aggregate_id),
                        admitted['profile_ref'], reference)
