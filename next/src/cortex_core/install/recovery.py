"""Read original P1 lineage and measure exact original-owner recovery truth.

No reconciliation provider, snapshot inference or old grant is introduced.
Unproved outcomes and intersecting attempts stay held for the original owner.
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


def _settled_applied(history, reference, *, states=('applied_verified',)):
    from . import lifecycle as p2
    reference = thaw(reference)
    owner._require(reference is not None and any(ref == reference for ref, _ in history.events()), 'journal_invalid')
    event, observation = p2._load_entry(history, reference)
    attempt = p2._decode(history.read(observation['attempt_ref']))
    reservation = p2._decode(history.read(observation['reservation_ref']))
    owner._require(event['result_ref'] is not None and attempt['dispatch_state'] in states
        and reservation['state'] == 'consumed' and attempt['result_ref'] == event['result_ref']
        and attempt['consumption_sequence'] is not None and attempt['consume_clock_ref'] is not None,
        'attempt_ambiguous')
    owner._require(p2._latest_attempt(history, event['identity'])[0] == reference, 'attempt_ambiguous')
    result = p2._decode(history.read(event['result_ref']))
    owner._require(set(result) == {'identity', 'effect', 'request_ref', 'state', 'observed'}
        and result['state'] == attempt['dispatch_state'] and type(result['observed']) is dict
        and result['identity'] == event['identity']
        and result['effect'] == event['effect'] and result['request_ref'] == event['request_ref'], 'resource_changed')
    original = history.read(event['request_ref'])
    request = thaw(parse_install_message(original, expected_kind='request').document)
    owner._require(request['identity'] == event['identity'] == attempt['identity']
        and event['origin_request_ref'] == attempt['origin_request_ref'], 'journal_invalid')
    ancestors = []
    if event['origin_request_ref'] != event['request_ref']:
        parent = _retry_parent(history, request)
        owner._require(parent['event']['origin_request_ref'] == event['origin_request_ref']
            and parent['event']['effect'] == event['effect']
            and attempt['predecessor_result_refs'] == [parent['event']['result_ref']], 'journal_invalid')
        ancestors = [parent] + parent['ancestors']
    return {'ref': reference, 'event': event, 'observation': observation, 'attempt': attempt,
            'result': result, 'request': request, 'raw_request': original, 'ancestors': ancestors}


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
    creators = {}
    predecessors = [old['event']['result_ref']]
    for resource in child['resources']:
        creator = _settled_applied(history, resource['result_event_ref']) if resource['ownership'] != 'external_read_only' else _settled_applied(history, old['ref'])
        creation = creator['request']
        owner._require(creation['operation'] == 'install'
            and all(creation['identity'][k] == origin['identity'][k]
                for k in ('installation_id', 'product', 'operation_id', 'transaction_id'))
            and all(creation[k] == origin[k] for k in ('target', 'release', 'profile', 'roots', 'invocation'))
            and all(creation['caller'][k] == origin['caller'][k] for k in ('owner_uid', 'host_identity_ref')),
            'resource_changed')
        observed = creator['result']['observed']
        witnesses, metadata = observed.get('resource_witnesses'), observed.get('resource_metadata')
        rid = resource['resource_id']
        owner._require(type(witnesses) is dict and type(metadata) is dict
            and resource['after_witness'] == witnesses.get(rid)
            and {k: v for k, v in resource.items() if k not in ('after_witness', 'result_event_ref')} == metadata.get(rid),
            'resource_changed')
        owner._require(resource['installation_id'] == identity['installation_id']
            and resource['product'] == identity['product']
            and resource['source_installer_ref'] == origin['release']['installer_ref'], 'resource_changed')
        if resource['ownership'] != 'external_read_only':
            owner._require(resource['created_by'] == creator['event']['request_ref'], 'resource_changed')
            intent, obs = p2._load_entry(history, resource['intent_event_ref'])
            reserved = p2._decode(history.read(obs['reservation_ref']))
            owner._require(intent['identity'] == creation['identity'] and intent['phase'] == 'create_intent'
                and intent['effect'] == creator['event']['effect']
                and reserved['state'] == 'reserved', 'resource_changed')
        creators[rid] = creator
        if creator['event']['result_ref'] not in predecessors:
            predecessors.append(creator['event']['result_ref'])
        _read_refs(history, resource)
    old['creators'] = creators
    old['predecessor_result_refs'] = predecessors
    old['receipt'] = child
    return old


def _scope(history, context):
    from . import lifecycle as p2
    consent = p2._decode(history.read(context['consent_ref']))
    return p2._decode(history.read(consent['scope_ref']))


def _renewed_scope(history, request, context, original):
    from . import lifecycle as p2
    scope = _scope(history, context)
    owner._require(request['authority_ref'] == context['consent_ref'], 'scope_denied')
    selected = set(scope['resource_ids'])
    owner._require(selected <= set(original['creators']), 'resource_changed')
    for rid in selected:
        creator = original['creators'][rid]
        original_context = p2._decode(history.read(creator['observation']['context_ref']))
        prior_scope = _scope(history, original_context)
        owner._require(context['consent_ref'] != original_context['consent_ref']
            and context['nonce'] != original_context['nonce']
            and context['host_lease']['fence'] >= original_context['host_lease']['fence']
            and scope['backend_binding_ref'] == prior_scope['backend_binding_ref'], 'scope_denied')
        owner._require(all(context['effect'][k] == creator['event']['effect'][k]
            for k in ('product', 'resource_kind', 'reversal_contract_ref', 'data_policy_ref')), 'scope_denied')
    anchors = [original['event']['request_ref']] + [
        creator['event']['request_ref'] for creator in original['creators'].values()]
    anchors += [subject['event']['origin_request_ref']
        for subject in [original, *original['creators'].values()]
        if subject['event']['origin_request_ref'] != subject['event']['request_ref']]
    for reference, event in history.events():
        if event['identity'] == context['identity']:
            continue
        later_phase = (event['sequence'] > original['event']['sequence']
            and p2._operation(event['identity']) == p2._operation(original['event']['identity'])
            and event['identity']['transaction_id'] == original['event']['identity']['transaction_id'])
        if event['origin_request_ref'] not in anchors and not later_phase:
            continue
        q = thaw(parse_install_message(history.read(event['request_ref']), expected_kind='request').document)
        if later_phase and q['operation'] == 'install':
            anchors.append(event['request_ref'])
        if q['operation'] not in ('rollback', 'uninstall', 'purge') or event['origin_request_ref'] not in anchors:
            continue
        _, observation = p2._load_entry(history, reference)
        previous_context = p2._decode(history.read(observation['context_ref']))
        owner._require(not selected.intersection(_scope(history, previous_context)['resource_ids']), 'attempt_ambiguous')
    return scope


def _retry_parent(history, request):
    """Previously checked authoritative NOT_APPLIED, never snapshot inference."""
    owner._require(request['operation'] == 'install' and request['journal_ref'] is not None,
                   'attempt_ambiguous')
    parent = _settled_applied(history, request['journal_ref'], states=('not_applied_verified',))
    old, identity = parent['request'], request['identity']
    owner._require(old['operation'] == 'install'
        and parent['attempt']['reconciliation_ref'] == parent['event']['result_ref'], 'attempt_ambiguous')
    owner._require(identity['parent_attempt_id'] == old['identity']['attempt_id']
        and identity['installation_id'] is not None
        and all(identity[k] == old['identity'][k] for k in
            ('product','installation_id','operation_id','transaction_id'))
        and all(identity[k] != old['identity'][k] for k in ('request_id','attempt_id'))
        and {k:v for k,v in request.items() if k not in ('identity','journal_ref')}
            == {k:v for k,v in old.items() if k not in ('identity','journal_ref')}, 'scope_denied')
    parent['predecessor_result_refs'] = [parent['event']['result_ref']]
    return parent


def _renewed_child(history, proposal, inputs, parent):
    from . import lifecycle as p2
    context = thaw(proposal.context)
    owner._require(type(inputs) is owner._Inputs
        and owner._decode(inputs.context_raw,'owner_unavailable') == context, 'owner_unavailable')
    old = p2._decode(history.read(parent['observation']['context_ref']))
    owner._require(context['effect'] == parent['event']['effect']
        and context['target'] == old['target'], 'scope_denied')
    owner._require(all(context[k] != old[k] for k in ('owner_epoch','session_id','nonce')),
                   'reservation_invalidated')
    owner._require(context['host_lease']['holder'] == old['host_lease']['holder']
        and context['host_lease']['fence'] > old['host_lease']['fence'], 'fence_changed')
    consent = owner._original(inputs,context['consent_ref'])
    scope, prior = owner._original(inputs,consent['scope_ref']), _scope(history,old)
    owner._require(scope['backend_binding_ref'] == prior['backend_binding_ref']
        and context['private_binding']['kind'] == old['private_binding']['kind'], 'scope_denied')
    if old['private_binding']['kind'] == 'private':
        previous = p2._decode(history.read(old['private_binding']['binding_ref']))
        current = owner._private(inputs,context)
        owner._require(all(current[k] == previous[k] for k in
            ('phase','holder','session_user_oid','session_user_name','installation_id','operation_id'))
            and current['fence'] > previous['fence'], 'fence_changed')


def _execute_recovery(history, original_request, current, primitives):
    """Measure the original subject, retaining v2 SQL exclusion through P1 ACK."""
    from . import lifecycle as p2
    from cortex_core.release.contracts import freeze
    request = thaw(parse_install_message(original_request,expected_kind='request').document)
    anchor = request['journal_ref']
    event, observation = p2._load_entry(history,anchor)
    context = p2._decode(history.read(observation['context_ref']))
    owner._require(request['identity']['parent_attempt_id'] == event['identity']['attempt_id'], 'scope_denied')
    owner._require(thaw(current.context['effect']) == context['effect']
        and thaw(current.context['target']) == context['target']
        and all(current.context['identity'][k] == event['identity'][k] for k in
            ('product','installation_id','operation_id','transaction_id')), 'scope_denied')
    primitive = primitives.get(context['effect']['resource_kind'])
    owner._require(primitive is not None, 'backend_unavailable')
    attempt = p2._decode(history.read(observation['attempt_ref']))
    reservation = p2._decode(history.read(observation['reservation_ref']))
    owner._require(reservation['state'] == 'consumed'
        and attempt['dispatch_state'] in ('consumed_result_unknown','applied_verified','not_applied_verified')
        and type(attempt['consumption_sequence']) is int
        and attempt['consume_clock_ref'] is not None, 'attempt_ambiguous')
    mandated = attempt.get('sql_mandate_ref') is not None
    original = {'context':context,'journal_ref':anchor,'preimages_ref':observation['preimages_ref']}
    original['deadline'] = current.deadline
    head = history.head()[0]
    if mandated:p2._sql_prefix(history,anchor,head)
    latest_ref, latest, obs = p2._latest_attempt(history,event['identity'])
    latest_attempt = p2._decode(history.read(obs['attempt_ref']))
    if not mandated:
        latest_reservation = p2._decode(history.read(obs['reservation_ref']))
        owner._require(all(latest[k] == event[k] for k in
                ('identity','effect','request_ref','origin_request_ref','release_binding_ref'))
            and obs['preimages_ref'] == observation['preimages_ref']
            and all(latest_attempt[k] == attempt[k] for k in
                ('identity','effect','request_ref','origin_request_ref','context_ref',
                 'consumption_sequence','consume_clock_ref','predecessor_result_refs'))
            and latest_reservation['state'] == 'consumed'
            and all(latest_reservation[k] == reservation[k] for k in
                ('expires_at','expires_monotonic_ns','reservation_id')), 'journal_invalid')
    first_result = (latest_attempt['dispatch_state'] == 'consumed_result_unknown'
        and latest_attempt['result_ref'] is None and latest_attempt['reconciliation_ref'] is None)
    with p2._measurement(primitive,freeze(original),mandated=mandated,recovery=True,require_held=first_result) as answer:
        owner._require(type(answer) is tuple and len(answer) == 2
            and answer[0] in ('APPLIED','NOT_APPLIED','UNKNOWN'), 'attempt_ambiguous')
        state, raw = answer
        owner._require(state != 'UNKNOWN' and type(raw) is bytes, 'attempt_ambiguous')
        if mandated:
            prefix = p2._sql_prefix(history,anchor,head)
            latest_ref,latest,attempt = prefix['ref'],prefix['event'],prefix['attempt']
            obs = p2._load_entry(history,latest_ref)[1]
            if attempt['dispatch_state'] != 'consumed_result_unknown':
                owner._require(history.read(latest['result_ref']) == raw,'resource_changed')
        else:
            latest_ref, latest, obs = p2._latest_attempt(history,event['identity'])
            attempt = p2._decode(history.read(obs['attempt_ref']))
            reference = (attempt['reconciliation_ref'] if attempt['dispatch_state'] == 'consumed_result_unknown'
                         else attempt['result_ref'])
            owner._require(latest['result_ref'] == attempt['result_ref'], 'journal_invalid')
            if reference is None:
                owner._require(first_result and attempt['dispatch_state'] == 'consumed_result_unknown'
                    and attempt['result_ref'] is None and attempt['reconciliation_ref'] is None, 'journal_invalid')
            else:
                owner._require(history.read(reference) == raw, 'journal_invalid')
        result = p2._decode(raw)
        expected_state = 'applied_verified' if state == 'APPLIED' else 'not_applied_verified'
        owner._require(set(result) == {'identity','effect','request_ref','state','observed'}
            and result['identity'] == event['identity'] and result['effect'] == event['effect']
            and result['request_ref'] == event['request_ref'] and result['state'] == expected_state
            and type(result['observed']) is dict, 'resource_changed')
        observed_head = history.head()[0]
        _, final = p2._current_owner(original_request,current.deadline)
        owner._same_context(current,final)
        owner._require(observed_head == head, 'generation_changed')
        if mandated:
            completed = p2._record_result(history,anchor,raw,publication_head=head,
                publication_revision=final.observed_state_revision,deadline=final.deadline,
                publication_context=thaw(final.context))
            published = attempt['dispatch_state'] == 'consumed_result_unknown'
        elif attempt['dispatch_state'] == 'consumed_result_unknown':
            completed = p2._record_result(history,latest_ref,raw,publication_head=head,
                publication_revision=final.observed_state_revision,deadline=final.deadline,
                publication_context=thaw(final.context))
            published = True
        else:
            owner._require(attempt['dispatch_state'] == expected_state, 'attempt_ambiguous')
            if state == 'APPLIED' or attempt['reconciliation_ref'] == reference:
                return freeze(latest_ref)
            # Legacy checked NOT_APPLIED still uses its original reconciliation slot.
            reservation = p2._decode(history.read(obs['reservation_ref']))
            old_context = p2._decode(history.read(obs['context_ref']))
            owner._require(history.current_revision() == final.observed_state_revision,'generation_changed')
            old_context['state_revision'] = final.observed_state_revision
            clock = p2._decode(history.read(attempt['consume_clock_ref']))
            reservation['expected_state_revision'] = final.observed_state_revision
            attempt['reservation_ref'] = history.retain(p2.canonical_bytes(reservation))
            attempt['reconciliation_ref'] = reference
            completed = p2._publish_entry(history,context=old_context,reservation=reservation,
                attempt=attempt,preimages_ref=obs['preimages_ref'],request_ref=latest['request_ref'],
                release_ref=latest['release_binding_ref'],expected_ref=head,phase='held',
                recorded_at=clock['now_utc'],result_ref=reference,deadline=final.deadline,
                expected_context=thaw(final.context))
            published = True
        # Truth may survive an expired/lost reply, but no old effect is redispatched.
        _, after = p2._current_owner(original_request,final.deadline)
        p2._continuity(thaw(final.context),thaw(after.context))
        owner._require(after.observed_state_revision == final.observed_state_revision+int(published),
                       'generation_changed')
        return freeze(completed)
