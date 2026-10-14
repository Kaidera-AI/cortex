"""Canonical source receipt projection and original-request byte checks.

Neither this projection nor a parsed PASS grants native/resource authority.
"""
from cortex_core.release.codec import canonical_bytes, sha256
from cortex_core.release.contracts import thaw

from .contracts import InstallRefusal, measure_original_ref, parse_install_message


def verify_product_receipt(original_bytes, original_request, *, request_ref, original_evidence=None, live_readers=None):
    """P1 verifies shape and original request binding, not installed/live facts."""
    request = parse_install_message(original_request, expected_kind='request')
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
