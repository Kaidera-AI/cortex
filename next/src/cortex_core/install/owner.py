"""One in-owner Path A gate: immutable pre-consume DATA, never an effect grant.

The fixed protected observer is deliberately UNBOUND before candidate access.
Its eventual adapter must independently admit peer/code/image/current originals
under C0/R0/B0, including the selected CPython3.12.15/cryptography50 closure.
Caller bytes never choose that adapter, a root, clock, phase or provider. R01's
existing fixed RC2 backend remains the sole release admission implementation.
P2a owns actual reserve/intent/consume/result and all durable history/preimages.
"""
from dataclasses import dataclass, field
import re

from cortex_core.release.codec import decode_json
from cortex_core.release.contracts import freeze, thaw
from cortex_core.runtime import signed_member
from cortex_core.runtime.config import RUNTIME_LOCK_MEMBER

from .contracts import InstallRefusal, measure_original_ref, parse_install_message

__all__ = ['OwnerRefusal', 'OwnerProposal', 'ProposedDeadline', 'propose_effect']
MISSING_UNIT = 'B1 protected owner peer/code/image/current observation composition'
_UTC_MAX = 253402300799
_INT_MAX = 2**63-1
_CONTEXT = frozenset(('identity request_ref effect peer_ref target owner_epoch session_id nonce '
    'policy_ref consent_ref current_state_ref clock_ref policy_generation freshness_generation '
    'state_revision host_lease private_binding').split())
_CONSENT = frozenset(('issuer_ref subject_ref scope_ref not_before expires_at generation revoked gate_b_acceptance_ref').split())
_SCOPE = frozenset(('identity target release_binding_ref effect resource_ids backend_binding_ref operation purpose cleanup_restrictions_ref').split())
_CLOCK = frozenset(('owner_epoch now_utc uncertainty_seconds monotonic_ns clock_receipt_ref').split())
_R01_REASONS = frozenset(('required_binding_gap member_authority_unavailable raw_limit aggregate_unsigned '
    'aggregate_signature_invalid aggregate_root_mismatch aggregate_algorithm_unsupported aggregate_shape '
    'aggregate_identity_mismatch aggregate_scope_mismatch aggregate_replay aggregate_pair_mismatch '
    'member_not_in_aggregate member_identity_mismatch member_digest_mismatch member_path_alias '
    'member_custody_changed unsealed_member').split())
_UNAVAILABLE = frozenset(('required_binding_gap owner_unavailable time_unavailable atomicity_unavailable backend_unavailable r01_required_binding_gap').split())
_DRIFT = frozenset(('policy_changed generation_changed fence_changed custody_changed resource_changed journal_invalid').split())


class OwnerRefusal(InstallRefusal):
    """Closed C0 detail and existing P1 outward result/reason; no raw text."""

    def __init__(self, detail, *, r01_reason=None):
        self.detail = detail
        self.r01_reason = r01_reason
        unit = None
        if detail in _UNAVAILABLE:
            self.result, reason = 'BLOCKED', 'dependency_not_run'
            if detail == 'required_binding_gap':
                unit = MISSING_UNIT
            elif detail == 'r01_required_binding_gap':
                unit = signed_member.MISSING_UNIT
        elif detail in _DRIFT:
            self.result, reason = 'HOLD', 'state_drift'
        elif detail == 'attempt_ambiguous':
            self.result, reason = 'HOLD', 'ambiguous_create'
        else:
            self.result = 'FAIL'
            reason = {'peer_denied': 'unsafe_custody', 'invalid_request': 'bad_shape',
                      'r01_refusal': 'wrong_release'}.get(detail, 'external_conflict')
        super().__init__(reason, unit=unit)


def _require(condition, detail):
    if not condition:
        raise OwnerRefusal(detail)


def _integer(value, maximum, detail, minimum=0):
    _require(type(value) is int and minimum <= value <= maximum, detail)
    return value


def _atom(value, detail='owner_unavailable'):
    _require(type(value) is str and 0 < len(value.encode('utf-8')) <= 128, detail)


@dataclass(frozen=True)
class _Inputs:
    """Private original-observation carrier, not provenance or a grant type."""
    context_raw: bytes
    originals: dict
    current_clock_raw: bytes
    aggregate_id: str


@dataclass(frozen=True)
class ProposedDeadline:
    owner_epoch: str
    proposed_at_utc: int
    proposed_at_monotonic_ns: int
    expires_at: int
    expires_monotonic_ns: int


@dataclass(frozen=True)
class OwnerProposal:
    request_sha256: str
    context: object
    deadline: ProposedDeadline
    observed_state_revision: int
    stage: str = field(default='PRECONSUME_DATA', init=False)
    effect_authority: bool = field(default=False, init=False)
    native_qualified: bool = field(default=False, init=False)
    reservation_created: bool = field(default=False, init=False)
    consumed: bool = field(default=False, init=False)


def _installed_owner_inputs():
    # ONE fixed private composition; no environment/argv/path/setter/test mode.
    # Missing any actual peer/code/image/owner/current binding refuses here.
    raise OwnerRefusal('required_binding_gap')


def _decode(raw, detail):
    try:
        value = decode_json(raw, 262144)
        _require(type(value) is dict, detail)
        return value
    except ValueError:
        raise OwnerRefusal(detail) from None


def _original(inputs, reference, detail='owner_unavailable'):
    try:
        _require(type(reference) is dict and set(reference) == {'path', 'bytes', 'sha256'}, detail)
        raw = inputs.originals[reference['path']]
        _require(type(raw) is bytes and len(raw) <= 262144, detail)
        measure_original_ref(reference, raw)
        return _decode(raw, detail)
    except (InstallRefusal, KeyError, TypeError, ValueError):
        raise OwnerRefusal(detail) from None


def _clock(value, epoch):
    _require(type(value) is dict and set(value) == _CLOCK, 'time_unavailable')
    _require(value['owner_epoch'] == epoch, 'time_unavailable')
    for key, limit in (('now_utc', _UTC_MAX), ('uncertainty_seconds', _UTC_MAX), ('monotonic_ns', _INT_MAX)):
        _integer(value[key], limit, 'time_unavailable')


def _window(context, start, now, receipt, consent, policy, peer, private):
    """Pure independently observed UTC/monotonic minimum; creates no reservation."""
    epoch = context['owner_epoch']
    _clock(start, epoch)
    _clock(now, epoch)
    _require(receipt.get('owner_epoch') == epoch and receipt.get('quality') == 'bounded', 'time_unavailable')
    _require(now['now_utc'] >= start['now_utc'] and now['monotonic_ns'] >= start['monotonic_ns'], 'time_unavailable')
    utc_bounds = [consent['expires_at'], policy['validity']['expires_at'], peer['expires_at'], context['host_lease']['expires_at']]
    names = {'consent', 'policy', 'session', 'host'}
    if private is not None:
        utc_bounds.append(private['expires_at'])
        names.add('private')
    mono = receipt.get('monotonic_deadlines')
    _require(type(mono) is dict and set(mono) == names, 'time_unavailable')
    for bound in utc_bounds:
        _integer(bound, _UTC_MAX, 'time_unavailable')
    for bound in mono.values():
        _integer(bound, _INT_MAX, 'time_unavailable')
    expires = min([start['now_utc'] + 30] + utc_bounds)
    expires_mono = min([start['monotonic_ns'] + 30_000_000_000] + list(mono.values()))
    _integer(expires, _UTC_MAX, 'time_unavailable')
    _integer(expires_mono, _INT_MAX, 'time_unavailable')
    not_before = (consent['not_before'], policy['validity']['not_before'], peer['not_before'])
    for bound in not_before:
        _integer(bound, _UTC_MAX, 'time_unavailable')
    lower = max(not_before)
    for clock in (start, now):
        _require(clock['now_utc'] - clock['uncertainty_seconds'] >= lower, 'time_unavailable')
        _require(clock['now_utc'] + clock['uncertainty_seconds'] < expires, 'reservation_expired')
        _require(clock['monotonic_ns'] < expires_mono, 'reservation_expired')
    return ProposedDeadline(epoch, start['now_utc'], start['monotonic_ns'], expires, expires_mono)


def _private(inputs, context):
    binding = context['private_binding']
    _require(type(binding) is dict, 'owner_unavailable')
    if binding == {'kind': 'none'}:
        return None
    _require(set(binding) == {'kind', 'binding_ref'} and binding['kind'] == 'private', 'owner_unavailable')
    value = _original(inputs, binding['binding_ref'], 'backend_unavailable')
    _require(value.get('phase') in ('runtime', 'bootstrap', 'migrator'), 'backend_unavailable')
    _require(value.get('kind') == value['phase'], 'backend_unavailable')
    _integer(value.get('generation'), _INT_MAX, 'backend_unavailable', 1)
    _require(value.get('installation_id') == context['identity']['installation_id']
             and value.get('operation_id') == context['identity']['operation_id']
             and value.get('generation') == context['policy_generation'], 'backend_unavailable')
    _integer(value.get('fence'), _INT_MAX, 'backend_unavailable', 1)
    _integer(value.get('session_user_oid'), _INT_MAX, 'backend_unavailable', 1)
    _atom(value.get('holder'), 'backend_unavailable')
    _atom(value.get('session_user_name'), 'backend_unavailable')
    if value['phase'] == 'runtime':
        _integer(value.get('map_revision'), _INT_MAX, 'backend_unavailable', 1)
    return value


def _drift(context, consent, policy, state):
    _require(context['policy_generation'] == consent['generation'] == policy['generation'] == state['generation'], 'generation_changed')
    _require(context['freshness_generation'] == state['freshness'] and context['state_revision'] == state['revision'], 'generation_changed')
    _require(context['host_lease']['fence'] == state['host_fence'], 'fence_changed')


def _evaluate(original_request, inputs, *, check_drift=True):
    """The common owner policy, read-only over current original observations."""
    _require(type(inputs) is _Inputs and type(inputs.originals) is dict and len(inputs.originals) <= 256, 'owner_unavailable')
    _require(all(type(v) is bytes for v in inputs.originals.values())
             and sum(len(v) for v in inputs.originals.values()) <= 8*1024*1024, 'owner_unavailable')
    context = _decode(inputs.context_raw, 'owner_unavailable')
    _require(set(context) == _CONTEXT, 'owner_unavailable')
    for key in ('owner_epoch', 'session_id', 'nonce'):
        _atom(context[key])
    for key in ('policy_generation', 'freshness_generation', 'state_revision'):
        _integer(context[key], _INT_MAX, 'owner_unavailable', 1)
    host = context['host_lease']
    _require(type(host) is dict and set(host) == {'holder', 'fence', 'expires_at'}, 'owner_unavailable')
    _atom(host['holder'])
    _integer(host['fence'], _INT_MAX, 'owner_unavailable', 1)
    peer = _original(inputs, context['peer_ref'])
    _require(peer.get('owner_epoch') == context['owner_epoch'] and peer.get('session_id') == context['session_id'], 'peer_denied')
    _atom(peer.get('lifetime'), 'peer_denied')
    try:
        request = parse_install_message(original_request, expected_kind='request')
    except InstallRefusal:
        raise OwnerRefusal('invalid_request') from None
    q = thaw(request.document)
    _require(type(peer.get('owner_uid')) is int and peer['owner_uid'] == q['caller']['owner_uid']
             and peer.get('host_identity_ref') == q['caller']['host_identity_ref'], 'peer_denied')
    consent = _original(inputs, context['consent_ref'])
    policy = _original(inputs, context['policy_ref'])
    state = _original(inputs, context['current_state_ref'])
    start = _original(inputs, context['clock_ref'], 'time_unavailable')
    now = _decode(inputs.current_clock_raw, 'time_unavailable')
    receipt = _original(inputs, start.get('clock_receipt_ref'), 'time_unavailable')
    _clock(now, context['owner_epoch'])
    _require(set(consent) == _CONSENT and type(consent['revoked']) is bool, 'owner_unavailable')
    _require(policy.get('reservation_max_seconds') == 30 and type(policy.get('validity')) is dict
             and set(policy['validity']) == {'not_before', 'expires_at'}, 'owner_unavailable')
    for field in ('generation', 'freshness', 'revision', 'host_fence'):
        _integer(state.get(field), _INT_MAX, 'owner_unavailable', 1)
    for value in (consent.get('generation'), policy.get('generation')):
        _integer(value, _INT_MAX, 'owner_unavailable', 1)
    scope = _original(inputs, consent['scope_ref'], 'consent_denied')
    _require(set(scope) == _SCOPE, 'consent_denied')
    _require(consent['subject_ref'] == context['peer_ref'], 'consent_denied')
    _require(q['identity'] == context['identity'] == scope['identity']
             and q['target'] == context['target'] == scope['target']
             and context['effect'] == scope['effect']
             and q['operation'] == scope['operation']
             and scope['operation'] in ('install', 'recover', 'rollback', 'uninstall', 'purge')
             and scope['purpose'] == policy.get('purpose'), 'scope_denied')
    try:
        measure_original_ref(context['request_ref'], original_request)
    except InstallRefusal:
        raise OwnerRefusal('scope_denied') from None
    _require(_original(inputs, scope['release_binding_ref'], 'scope_denied') == q['release'], 'scope_denied')
    resources = scope['resource_ids']
    _require(type(resources) is list and 0 < len(resources) <= 128 and all(type(v) is str for v in resources)
             and len(set(resources)) == len(resources), 'scope_denied')
    for resource in resources:
        _atom(resource, 'scope_denied')
    _original(inputs, scope['backend_binding_ref'], 'backend_unavailable')
    _require(not consent['revoked'], 'consent_revoked')
    _integer(consent['expires_at'], _UTC_MAX, 'time_unavailable')
    _integer(peer.get('expires_at'), _UTC_MAX, 'time_unavailable')
    _require(now['now_utc'] + now['uncertainty_seconds'] < consent['expires_at'], 'consent_expired')
    _require(now['now_utc'] + now['uncertainty_seconds'] < peer['expires_at'], 'session_stale')
    private = _private(inputs, context)
    deadline = _window(context, start, now, receipt, consent, policy, peer, private)
    if check_drift:
        _drift(context, consent, policy, state)
    return OwnerProposal(request.sha256, freeze(context), deadline, context['state_revision'])


def _release_original(original_request, inputs):
    # Reuse the sole R01/RC2 backend and sealed original-byte reader. No verifier
    # provider is selected here; U3's fixed startup/cryptography closure owns it.
    q = thaw(parse_install_message(original_request, expected_kind='request').document)
    _require(type(inputs.aggregate_id) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', inputs.aggregate_id) is not None
             and inputs.aggregate_id == 'sha256:' + q['release']['joint_index_ref']['sha256'], 'scope_denied')
    member = signed_member.request_member(inputs.aggregate_id, RUNTIME_LOCK_MEMBER)
    raw = signed_member.read_member(member, aggregate_id=inputs.aggregate_id, member_name=RUNTIME_LOCK_MEMBER)
    rows = [r for r in q['release']['artifact_refs'] if r['path'] == RUNTIME_LOCK_MEMBER]
    _require(len(rows) == 1, 'scope_denied')
    try:
        measure_original_ref(rows[0], raw)
    except InstallRefusal:
        raise signed_member.Refusal('member_digest_mismatch') from None


def _same_context(first, fresh):
    a, b = first.context, fresh.context
    _require(a['owner_epoch'] == b['owner_epoch'] and a['session_id'] == b['session_id'], 'session_stale')
    _require(a['nonce'] == b['nonce'], 'nonce_replay')
    _require(a['policy_generation'] == b['policy_generation'] and a['freshness_generation'] == b['freshness_generation']
             and a['state_revision'] == b['state_revision'], 'generation_changed')
    _require(a['host_lease'] == b['host_lease'], 'fence_changed')
    _require(a['policy_ref'] == b['policy_ref'], 'policy_changed')
    _require({k: v for k, v in a.items() if k != 'clock_ref'} == {k: v for k, v in b.items() if k != 'clock_ref'}, 'resource_changed')


def propose_effect(original_request):
    """P2a entry: independently reobserve and return DATA, never reserve/consume.

    P2a must retain originals, atomically reserve, preserve the original deadline,
    repeat final current admission and durably consume/result at its primitive.
    Recalling this function cannot renew an actual historical reservation.
    """
    try:
        inputs = _installed_owner_inputs()  # mandatory fixed binding BEFORE candidate
        first = _evaluate(original_request, inputs, check_drift=False)
        _release_original(original_request, inputs)
        fresh = _evaluate(original_request, _installed_owner_inputs())
        _same_context(first, fresh)
        return fresh
    except OwnerRefusal:
        raise
    except signed_member.Refusal as error:
        reason = error.reason if type(error.reason) is str and error.reason in _R01_REASONS else 'member_authority_unavailable'
        detail = 'r01_required_binding_gap' if reason == 'required_binding_gap' else 'r01_refusal'
        raise OwnerRefusal(detail, r01_reason=reason) from None
    except Exception:
        raise OwnerRefusal('owner_unavailable') from None
