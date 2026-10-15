"""Finite inverse policy over existing P1 resource/owner observations.

This DATA projection cannot grant authority. The lifecycle repeats current C0
admission, reserves/consumes in its original history and checks actual results.
"""
from cortex_core.release.codec import WireValidator
from cortex_core.release.contracts import thaw
from . import contracts, owner, recovery

_RETAIN = frozenset(('state', 'authoritative_data', 'derived_data', 'backup'))


def _plan(history, request, context, original):
    from . import lifecycle as p2
    scope = recovery._scope(history, context)
    before = p2._decode(history.read(context['effect']['expected_before_ref']))
    restrictions = p2._decode(history.read(scope['cleanup_restrictions_ref']))
    owner._require(set(before) == {'resources'} and type(before['resources']) is list
        and 0 < len(before['resources']) <= 128, 'resource_changed')
    resources = before['resources']
    resource_schema = {'$defs': contracts._schema()['$defs'], '$ref': '#/$defs/ResourceEntry'}
    owner._require(all(next(WireValidator(resource_schema).iter_errors(r), None) is None for r in resources), 'resource_changed')
    contracts._paths(resources)
    ids = [r['resource_id'] for r in resources]
    owner._require(len(set(ids)) == len(ids) and set(ids) == set(scope['resource_ids']), 'resource_changed')
    originals = {r['resource_id']: r for r in original['receipt']['resources']}
    owner._require(set(ids) <= set(originals), 'resource_changed')
    purge = request['operation'] == 'purge'
    if purge:
        selection = request['purge_selection']
        owner._require(selection == restrictions['purge_selection']
            and selection['product'] == request['identity']['product'] == 'cortex'
            and selection['exact_data_roots'] and selection['confirmation_refs']
            and len(set(selection['exact_data_roots'])) == len(selection['exact_data_roots']), 'scope_denied')
        for path in selection['exact_data_roots']:
            contracts.absolute_path(path)
        recovery._read_refs(history, selection)
    targets, retained, reasons = {}, [], {}
    for resource in resources:
        rid = resource['resource_id']; previous = originals[rid]
        owner._require({k: v for k, v in resource.items() if k not in ('after_witness', 'consumers_ref')}
            == {k: v for k, v in previous.items() if k not in ('after_witness', 'consumers_ref')}, 'resource_changed')
        recovery._read_refs(history, resource)
        reason = None
        if resource['ownership'] == 'external_read_only' or resource['removal_policy'] == 'retain_external':
            reason = 'external_read_only'
        else:
            old, current = previous['after_witness'], resource['after_witness']
            owner._require(type(old) is dict and type(current) is dict
                and ((old['filesystem_identity'] is not None and current['filesystem_identity'] is not None
                      and old['uid'] is not None and current['uid'] is not None)
                    or (old['backend_id'] is not None and current['backend_id'] is not None
                        and old['backend_witness_ref'] is not None and current['backend_witness_ref'] is not None)),
                'resource_changed')
            owner._require({k: v for k, v in old.items() if k != 'content_ref'}
                == {k: v for k, v in current.items() if k != 'content_ref'}, 'resource_changed')
            owner._require(resource['consumers_ref'] is not None, 'resource_changed')
            consumers = p2._decode(history.read(resource['consumers_ref']))
            owner._require(set(consumers) == {'resource_id', 'consumers'}
                and consumers['resource_id'] == rid and type(consumers['consumers']) is list
                and len(consumers['consumers']) <= 128
                and all(type(v) is str and 0 < len(v) <= 128 for v in consumers['consumers']), 'resource_changed')
            if old['content_ref'] != current['content_ref']:
                reason = 'modified'
            elif consumers['consumers']:
                reason = 'shared_consumer'
            elif resource['data_class'] in ('credential_custody', 'shared_support'):
                reason = 'retain_private_or_shared'
            elif resource['data_class'] in _RETAIN and not purge:
                reason = 'retain_data'
            elif resource['removal_policy'] in ('retain_data', 'hold') and not purge:
                reason = 'retained_by_policy'
            if reason is None:
                if purge:
                    path = current['canonical_path']
                    owner._require(path is not None and any(path == root or path.startswith(root + '/')
                        for root in request['purge_selection']['exact_data_roots']), 'scope_denied')
                    targets[rid] = None
                elif resource['removal_policy'] == 'reverse_if_unchanged' or request['operation'] == 'rollback':
                    before_witness = previous['before_witness']
                    owner._require(type(before_witness) is dict, 'resource_changed')
                    targets[rid] = None if before_witness['kind'] == 'absent' else before_witness
                else:
                    owner._require(resource['removal_policy'] == 'remove_if_unchanged_unshared', 'resource_changed')
                    targets[rid] = None
        if reason is not None:
            retained.append({'resource_id': rid, 'reason': reason,
                             'witness_ref': context['effect']['expected_before_ref']})
            reasons[rid] = reason
    return {'targets': targets, 'retained_items': retained, 'resources': resources, 'reasons': reasons}


def _current_plan(history, request, proposal, inputs, original):
    context = thaw(proposal.context)
    owner._require(type(inputs) is owner._Inputs
        and owner._decode(inputs.context_raw, 'owner_unavailable') == context, 'owner_unavailable')
    scope = recovery._renewed_scope(history, request, context, original)
    # Current provider bytes, not an old retained fact standing in for a missing observation.
    owner._original(inputs, context['effect']['expected_before_ref'])
    restrictions = owner._original(inputs, scope['cleanup_restrictions_ref'])
    owner._require(set(restrictions) == {'expires_at', 'expires_monotonic_ns', 'purge_selection'}, 'owner_unavailable')
    owner._integer(restrictions['expires_at'], owner._UTC_MAX, 'time_unavailable')
    owner._integer(restrictions['expires_monotonic_ns'], owner._INT_MAX, 'time_unavailable')
    now = owner._decode(inputs.current_clock_raw, 'time_unavailable')
    owner._clock(now, context['owner_epoch'])
    owner._require(now['now_utc'] + now['uncertainty_seconds'] < restrictions['expires_at']
        and now['monotonic_ns'] < restrictions['expires_monotonic_ns'], 'reservation_expired')
    return _plan(history, request, context, original)


def _verify_result(history, plan, raw):
    from . import lifecycle as p2
    result = p2._decode(raw)
    owner._require(result['state'] == 'applied_verified', 'attempt_ambiguous')
    measured = result['observed'].get('resources')
    owner._require(type(measured) is list and len(measured) == len(plan['resources']), 'resource_changed')
    originals = {r['resource_id']: r for r in plan['resources']}
    owner._require(all(type(r) is dict and 'resource_id' in r for r in measured)
        and len({r['resource_id'] for r in measured}) == len(measured)
        and {r['resource_id'] for r in measured} == set(originals), 'resource_changed')
    for resource in measured:
        before = originals[resource['resource_id']]
        owner._require({k: v for k, v in resource.items() if k != 'after_witness'}
            == {k: v for k, v in before.items() if k != 'after_witness'}, 'resource_changed')
        expected = plan['targets'].get(resource['resource_id'], before['after_witness'])
        owner._require(resource.get('after_witness') == expected, 'resource_changed')
        recovery._read_refs(history, resource)


def _cleanup_projection(history, completed_ref):
    """Historical P1 cleanup fields only; never current authority or readiness."""
    from . import lifecycle as p2
    event, observation = p2._load_entry(history, thaw(completed_ref))
    owner._require(event['result_ref'] is not None and event['phase'] == 'finished', 'attempt_ambiguous')
    q = thaw(contracts.parse_install_message(history.read(event['request_ref']), expected_kind='request').document)
    context = p2._decode(history.read(observation['context_ref']))
    plan = _plan(history, q, context, recovery._applied_lineage(history, q))
    _verify_result(history, plan, history.read(event['result_ref']))
    actions = []
    for resource in plan['resources']:
        rid = resource['resource_id']
        outcome = ('removed' if plan['targets'][rid] is None else 'restored') if rid in plan['targets'] else 'kept'
        actions.append({'resource_id': rid, 'outcome': outcome,
                        'reason': plan['reasons'].get(rid, 'exact_owned_inverse'), 'witness_ref': event['result_ref']})
    retained = [{**r, 'witness_ref': event['result_ref']} for r in plan['retained_items']]
    return {'cleanup': {'state': 'complete', 'actions': actions,
        'remaining_owned_ids': [r['resource_id'] for r in plan['resources']
            if r['resource_id'] not in plan['targets'] and r['ownership'] != 'external_read_only'],
        'evidence_ref': event['result_ref']}, 'retained_items': retained}
