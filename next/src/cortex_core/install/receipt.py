"""Canonical source receipt projection and original-request byte checks.

Neither this projection nor a parsed PASS grants native/resource authority.
"""
from cortex_core.release.codec import canonical_bytes, sha256
from cortex_core.release.contracts import thaw

from .contracts import InstallRefusal, measure_original_ref, parse_install_message


def verify_product_receipt(original_bytes, original_request, *, request_ref, original_evidence=None, live_readers=None):
    """P1 verifies shape and original request binding, not installed/live facts."""
    request = parse_install_message(original_request, expected_kind='request')
    if original_evidence is not None:
        if type(original_evidence) is not dict or set(original_evidence) != {'expected_child_ref'}:
            raise InstallRefusal('bad_shape')
        measure_original_ref(original_evidence['expected_child_ref'], original_bytes)
    receipt = parse_install_message(original_bytes, expected_kind='receipt')
    q, r = thaw(request.document), thaw(receipt.document)
    measure_original_ref(request_ref, original_request)
    if r['request_ref'] != request_ref:
        raise InstallRefusal('wrong_release')
    for key in ('identity', 'caller', 'target', 'release', 'profile', 'roots'):
        if r[key] != q[key]:
            raise InstallRefusal('wrong_release')
    invocation = r['invocation_result']
    for key in ('entrypoint_ref', 'argv', 'cwd_root', 'environment_policy_ref'):
        if invocation[key] != q['invocation'][key]:
            raise InstallRefusal('wrong_release')
    if q['operation'] in ('plan', 'verify'):
        if r['journal_ref'] is not None or invocation['outcome'] != 'not_executed' or invocation['not_executed_reason'] != 'read_only':
            raise InstallRefusal('bad_shape')
        for field in ('previous_receipt_ref', 'journal_ref'):
            if q[field] is not None and q[field] not in r['evidence_refs']:
                raise InstallRefusal('wrong_release')
        if (r['disposition'] == 'created_by_this_transaction'
                or any(resource['ownership'] == 'created_by_this_transaction' for resource in r['resources'])
                or r['phase'] not in ('planned', 'verified', 'refusing')):
            raise InstallRefusal('bad_shape')
    return receipt


def _persist_child_receipt(original_request, original_child):
    """Fixed owner source seam; durable ordinary evidence before parent ACK.

    This does not activate KOS, mint readiness or renew an old consumed grant.
    The independent current owner binds the whole expected child original.
    """
    from pathlib import Path
    from . import contracts, lifecycle, owner, recovery
    first = owner.propose_effect(original_request)
    bindings = lifecycle._installed_lifecycle()
    owner._require(type(bindings) is lifecycle._Bindings
        and type(bindings.history) is lifecycle._History, 'backend_unavailable')
    history = bindings.history
    with history.operation():
        inputs, current = lifecycle._current_owner(original_request, first.deadline)
        owner._same_context(first, current)
        if (parse_install_message is not contracts.parse_install_message
                or Path(parse_install_message.__code__.co_filename).resolve()
                != Path(__file__).resolve().with_name('contracts.py')):
            raise InstallRefusal('unsafe_custody')
        context = thaw(current.context)
        expected = context['effect']['expected_before_ref']
        try:
            measure_original_ref(expected, inputs.originals[expected['path']])
        except KeyError:
            raise owner.OwnerRefusal('owner_unavailable') from None
        child = verify_product_receipt(original_child, original_request,
            request_ref=context['request_ref'], original_evidence={'expected_child_ref': expected})
        q = thaw(parse_install_message(original_request, expected_kind='request').document)
        r = thaw(child.document)
        if q['caller']['route'] != 'kos_prerequisite' or q['caller']['parent_transaction_id'] is None:
            raise InstallRefusal('bad_shape')
        original = recovery._settled_applied(history, r['journal_ref'])
        owner._require(original['raw_request'] == original_request and r['result'] == 'PASS'
            and current.observed_state_revision == history.current_revision(), 'resource_changed')
        # Existing file+directory fsync must finish before the parent gets an ACK.
        child_ref = history.retain(original_child)
        revision_after_persistence = history.current_revision()
        _, final = lifecycle._current_owner(original_request, current.deadline)
        owner._same_context(current, final)
        owner._require(final.observed_state_revision == revision_after_persistence, 'generation_changed')
        from cortex_core.release.contracts import freeze
        return freeze(child_ref)


def readonly_receipt(parsed_request, *, request_ref, attempts_ref, evidence_refs, disposition='absent', product_state=None):
    q = thaw(parsed_request.document)
    measure_original_ref(request_ref, parsed_request.raw)
    invocation = q['invocation']
    receipt = {'schema': q['schema'], 'format_version': 1, 'kind': 'receipt',
               'identity': q['identity'], 'caller': q['caller'], 'target': q['target'],
               'request_ref': request_ref, 'release': q['release'], 'product_state': product_state,
               'result': 'PASS', 'reason_code': 'completed', 'phase': 'planned' if q['operation'] == 'plan' else 'verified',
               'disposition': disposition,
               'invocation_result': {'outcome': 'not_executed', 'entrypoint_ref': invocation['entrypoint_ref'],
                   'executable_witness_ref': None, 'argv': invocation['argv'], 'cwd_root': invocation['cwd_root'],
                   'environment_policy_ref': invocation['environment_policy_ref'], 'started_at': None, 'ended_at': None,
                   'exit_code': None, 'signal': None, 'not_executed_reason': 'read_only', 'output_refs': [],
                   'forbidden_attempts_ref': attempts_ref},
               'profile': q['profile'], 'roots': q['roots'], 'resources': [],
               'readiness': {'state': 'not_installed' if product_state is None else 'not_checked',
                             'descriptor_ref': None, 'connection_custody_ref': None, 'checks_ref': None},
               'journal_ref': None, 'evidence_refs': evidence_refs,
               'cleanup': {'state': 'not_required', 'actions': [], 'remaining_owned_ids': [], 'evidence_ref': None},
               'retained_items': []}
    # The actual common verifier decides the projection; no parallel writer ABI.
    verify_product_receipt(canonical_bytes(receipt), parsed_request.raw, request_ref=request_ref)
    return receipt
