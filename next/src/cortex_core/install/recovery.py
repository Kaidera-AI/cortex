"""Read the original P1 lineage; only durable exact APPLIED is handled here.

No reconciliation provider, snapshot inference or old grant is introduced.
Consumed/unknown and intersecting attempts stay held for the original owner.
"""
from cortex_core.release.contracts import thaw
from . import owner
from .contracts import parse_install_message
from .receipt import verify_product_receipt


def _read_refs(history, value):
    if type(value) is dict:
        if set(value) == {'path', 'bytes', 'sha256'}:
            history.read(value)
        else:
            for child in value.values():
                _read_refs(history, child)
    elif type(value) is list:
        for child in value:
            _read_refs(history, child)


def _settled_applied(history, reference):
    from . import lifecycle as p2
    reference = thaw(reference)
    owner._require(reference is not None and any(ref == reference for ref, _ in history.events()), 'journal_invalid')
    event, observation = p2._load_entry(history, reference)
    attempt = p2._decode(history.read(observation['attempt_ref']))
    reservation = p2._decode(history.read(observation['reservation_ref']))
    owner._require(event['result_ref'] is not None and attempt['dispatch_state'] == 'applied_verified'
        and reservation['state'] == 'consumed' and attempt['result_ref'] == event['result_ref']
        and attempt['consumption_sequence'] is not None and attempt['consume_clock_ref'] is not None,
        'attempt_ambiguous')
    owner._require(p2._latest_attempt(history, event['identity'])[0] == reference, 'attempt_ambiguous')
    result = p2._decode(history.read(event['result_ref']))
    owner._require(set(result) == {'identity', 'effect', 'request_ref', 'state', 'observed'}
        and result['state'] == 'applied_verified' and result['identity'] == event['identity']
        and result['effect'] == event['effect'] and result['request_ref'] == event['request_ref'], 'resource_changed')
    original = history.read(event['request_ref'])
    request = thaw(parse_install_message(original, expected_kind='request').document)
    owner._require(request['identity'] == event['identity'] == attempt['identity']
        and event['origin_request_ref'] == attempt['origin_request_ref'] == event['request_ref'], 'journal_invalid')
    return {'ref': reference, 'event': event, 'observation': observation, 'attempt': attempt,
            'result': result, 'request': request, 'raw_request': original}


def _applied_lineage(history, request):
    """Immutable creation proof, not a current effect authorization."""
    from . import lifecycle as p2
    old = _settled_applied(history, request['journal_ref'])
    origin, identity = old['request'], request['identity']
    owner._require(origin['operation'] == 'install' and identity['installation_id'] is not None
        and identity['installation_id'] == origin['identity']['installation_id']
        and identity['product'] == origin['identity']['product'] == 'cortex'
        and identity['parent_attempt_id'] == origin['identity']['attempt_id']
        and identity['request_id'] != origin['identity']['request_id']
        and identity['attempt_id'] != origin['identity']['attempt_id'], 'scope_denied')
    if request['operation'] == 'rollback':
        owner._require(identity['operation_id'] == origin['identity']['operation_id']
            and identity['transaction_id'] == origin['identity']['transaction_id'], 'scope_denied')
    else:
        owner._require(request['operation'] in ('uninstall', 'purge')
            and identity['operation_id'] != origin['identity']['operation_id'], 'scope_denied')
    owner._require(all(request[k] == origin[k] for k in ('target', 'release', 'profile', 'roots', 'invocation'))
        and all(request['caller'][k] == origin['caller'][k] for k in ('owner_uid', 'host_identity_ref')),
        'scope_denied')
    owner._require(request['previous_receipt_ref'] is not None, 'journal_invalid')
    child = thaw(verify_product_receipt(history.read(request['previous_receipt_ref']), old['raw_request'],
        request_ref=old['event']['request_ref']).document)
    owner._require(child['result'] == 'PASS' and child['journal_ref'] == old['ref']
        and child['disposition'] in ('created_by_this_transaction', 'verified_owned_continuation'), 'resource_changed')
    witnesses = old['result']['observed'].get('resource_witnesses')
    owner._require(type(witnesses) is dict
        and set(witnesses) == {r['resource_id'] for r in child['resources']}, 'resource_changed')
    for resource in child['resources']:
        owner._require(resource['installation_id'] == identity['installation_id']
            and resource['product'] == identity['product']
            and resource['after_witness'] == witnesses[resource['resource_id']]
            and resource['source_installer_ref'] == origin['release']['installer_ref'], 'resource_changed')
        if resource['ownership'] != 'external_read_only':
            owner._require(resource['created_by'] == old['event']['request_ref']
                and resource['result_event_ref'] == old['ref'], 'resource_changed')
            intent, obs = p2._load_entry(history, resource['intent_event_ref'])
            reserved = p2._decode(history.read(obs['reservation_ref']))
            owner._require(intent['identity'] == origin['identity'] and intent['phase'] == 'create_intent'
                and reserved['state'] == 'reserved', 'resource_changed')
        _read_refs(history, resource)
    old['receipt'] = child
    return old


def _scope(history, context):
    from . import lifecycle as p2
    consent = p2._decode(history.read(context['consent_ref']))
    return p2._decode(history.read(consent['scope_ref']))


def _renewed_scope(history, request, context, original):
    from . import lifecycle as p2
    original_context = p2._decode(history.read(original['observation']['context_ref']))
    scope, prior_scope = _scope(history, context), _scope(history, original_context)
    owner._require(request['authority_ref'] == context['consent_ref']
        and context['consent_ref'] != original_context['consent_ref']
        and context['nonce'] != original_context['nonce']
        and context['host_lease']['fence'] >= original_context['host_lease']['fence']
        and scope['backend_binding_ref'] == prior_scope['backend_binding_ref'], 'scope_denied')
    owner._require(all(context['effect'][k] == original['event']['effect'][k]
        for k in ('product', 'resource_kind', 'reversal_contract_ref', 'data_policy_ref')), 'scope_denied')
    selected = set(scope['resource_ids'])
    for reference, event in history.events():
        if event['identity'] == context['identity'] or event['origin_request_ref'] != original['event']['request_ref']:
            continue
        q = thaw(parse_install_message(history.read(event['request_ref']), expected_kind='request').document)
        if q['operation'] not in ('rollback', 'uninstall', 'purge'):
            continue
        _, observation = p2._load_entry(history, reference)
        previous_context = p2._decode(history.read(observation['context_ref']))
        owner._require(not selected.intersection(_scope(history, previous_context)['resource_ids']), 'attempt_ambiguous')
    return scope
