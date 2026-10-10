"""C02 new-consumer fixture checks; no OpenKai client or live API calls."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from uuid import UUID


CONTRACTS = Path(__file__).resolve().parents[2] / 'contracts'
SOURCE_COMMIT = '83b9409989ca9ca7f6a08473523d6a171c63d44b'
EVIDENCE_SHA256 = 'aa6e5a2a5e27aeddf4e0aef98b530055bde3d71cf54ecd23d026b1f291c6abe9'
OPENAPI_SHA256 = '4b9e58be0219926c760c62b799974c63991c5fbbe751b6833c8889eb5cfe8de4'
ROUTES_SHA256 = 'c66eff6fb7af46e4c7baaed7ad2a3944d912ede0c6f3aa6423d5c576e0d9ce29'
SOURCE_FILES_SHA256 = 'f9a4979dc569211e0605d6b30abe1001a316f3c8c126ceea8bdb0699511ee645'
OBSERVED = {
    ('GET', '/beat/embeddings/backlog'), ('GET', '/degradation'),
    ('GET', '/health'), ('GET', '/projects/{project_key}'),
    ('GET', '/workers/health'), ('POST', '/artifacts'),
    ('POST', '/memory'), ('POST', '/search'), ('POST', '/sessions/ingest'),
}
SDK_ONLY = {
    ('DELETE', '/skills/{slug}'), ('GET', '/events'),
    ('GET', '/sessions/ingested-ids'), ('GET', '/skills'),
    ('POST', '/skills'), ('POST', '/skills/{slug}/bind'),
}
SCOPES = {'public_health', 'project_read', 'project_write', 'operator_read'}
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_BEHAVIOR = re.compile(r'B\d{2}\Z')


class ContractRefusal(ValueError):
    """A proposed OpenKai fixture is ambiguous, stale, or unsafe."""


def _need(condition, reason):
    if not condition:
        raise ContractRefusal(reason)


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _digest(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _operation(openapi, row):
    try:
        return openapi['paths'][row['path']][row['method'].lower()]['operationId']
    except (KeyError, TypeError, AttributeError):
        raise ContractRefusal('unmapped_operation') from None


def validate_packet(packet, openapi):
    """Pin released source inventory to exactly the nine observed routes."""
    try:
        _need(isinstance(packet, dict) and packet['format'] == 'openkai-consumer-v1',
              'unsupported_packet')
        source = packet['source']
        _need(source['repository'] == 'Kaidera-AI/kaideraos'
              and source['tag'] == 'openkai/v0.1.15'
              and source['commit'] == SOURCE_COMMIT
              and source['package_version'] == '0.1.15'
              and source['evidence_sha256'] == EVIDENCE_SHA256
              and source['c01_openapi_sha256'] == OPENAPI_SHA256
              and source['route_inventory_sha256'] == ROUTES_SHA256
              and source['source_files_sha256'] == SOURCE_FILES_SHA256,
              'source_pin_mismatch')
        _need(hashlib.sha256((CONTRACTS/'openapi.json').read_bytes()).hexdigest()
              == OPENAPI_SHA256, 'openapi_bytes_changed')
        files = source['files']
        _need(isinstance(files, dict) and len(files) == 33
              and all(isinstance(path, str) and path.startswith('products/openkai/')
                      and _digest(digest) for path, digest in files.items()),
              'source_files_invalid')
        file_bytes = json.dumps(files, sort_keys=True, separators=(',', ':')).encode()
        _need(hashlib.sha256(file_bytes).hexdigest() == SOURCE_FILES_SHA256,
              'source_file_snapshot_changed')
        observed, sdk = packet['observed'], packet['sdk_only']
        _need(isinstance(observed, list) and isinstance(sdk, list), 'route_lists_invalid')
        raw = json.dumps([observed, sdk], sort_keys=True,
                         separators=(',', ':')).encode()
        _need(hashlib.sha256(raw).hexdigest() == ROUTES_SHA256,
              'route_source_snapshot_changed')
        for rows, expected, sdk_row in ((observed, OBSERVED, False),
                                        (sdk, SDK_ONLY, True)):
            pairs = [(row['method'], row['path']) for row in rows]
            _need(len(pairs) == len(expected) and set(pairs) == expected,
                  'route_inventory_changed')
            for row in rows:
                _need(row['operation_id'] == _operation(openapi, row),
                      'operation_id_mismatch')
                _need((row['scope'] == 'sdk_surface_unfrozen') if sdk_row
                      else row['scope'] in SCOPES, 'scope_unfrozen')
                behaviors = row['behavior_ids']
                _need(isinstance(behaviors, list) and behaviors
                      and len(set(behaviors)) == len(behaviors)
                      and all(isinstance(value, str) and _BEHAVIOR.fullmatch(value)
                              for value in behaviors), 'behavior_source_missing')
                sites = row['call_sites']
                _need(isinstance(sites, list) and sites, 'call_site_missing')
                for site in sites:
                    _need(site['file'] in files
                          and site['sha256'] == files[site['file']]
                          and type(site['start_line']) is int and site['start_line'] > 0
                          and type(site['end_line']) is int
                          and site['end_line'] >= site['start_line'],
                          'call_site_mismatch')
        _need(packet['upgrade_origin'] == {
            'v0.1.002': 'not_signed_origin',
            'first_signed_candidate': 'v0.1.003',
            'status': 'pending_cto_signature',
            'pre_signed_path': 'backup_fresh_install_restore'},
              'upgrade_origin_mismatch')
        _need(packet['unbound_contract_deltas'] == [
            'authorized_core_record_read_by_id', 'indexed_revision_wait',
            'standalone_status_control_jobs'], 'unbound_delta_missing')
        return len(observed), len(sdk)
    except (KeyError, TypeError, OSError):
        raise ContractRefusal('packet_malformed') from None


def _receipt(value, request_key):
    _need(isinstance(value, dict) and _uuid(value.get('record_id'))
          and _uuid(value.get('event_id'))
          and type(value.get('aggregate_version')) is int
          and value['aggregate_version'] > 0
          and value.get('durability') == 'canonical_committed'
          and value.get('projection') in ('pending', 'visible')
          and value.get('request_key') == request_key,
          'uncommitted_or_invalid_receipt')


def _write_ack_read(case):
    request, ack, read = case['request'], case['ack'], case['read']
    _need(_uuid(request['project_id']) and _uuid(request['writer_id'])
          and _digest(request['payload_sha256'])
          and isinstance(request['request_key'], str) and request['request_key'],
          'invalid_write_intent')
    _need(ack['status'] == 200, 'write_not_acknowledged')
    receipt = ack['receipt']
    _receipt(receipt, request['request_key'])
    _need(read['status'] == 200 and read['authorized'] is True
          and read['binding'] == 'UNBOUND_CONTRACT_DELTA'
          and read['project_id'] == request['project_id']
          and read['record_id'] == receipt['record_id']
          and read['revision'] == receipt['aggregate_version'],
          'read_after_write_broken')


def _idempotency(case):
    first, same, conflict = (case[key] for key in
                             ('first', 'same_retry', 'conflicting_retry'))
    key = first['request_key']
    _need(isinstance(key, str) and key
          and _digest(first['payload_sha256'])
          and same['request_key'] == key
          and same['payload_sha256'] == first['payload_sha256'],
          'retry_identity_changed')
    _receipt(first['receipt'], key)
    _receipt(same['receipt'], key)
    _need(same['receipt'] == first['receipt'], 'duplicate_retry_receipt')
    _need(conflict['request_key'] == key
          and _digest(conflict['payload_sha256'])
          and conflict['payload_sha256'] != first['payload_sha256']
          and conflict['status'] == 409 and conflict['error'] == 'conflict',
          'conflicting_retry_accepted')


def _timeout(case):
    _need(_digest(case['request']['payload_sha256'])
          and isinstance(case['request']['request_key'], str)
          and case['request']['request_key']
          and case['transport'] == 'timeout_after_dispatch'
          and case['outcome'] == 'unknown'
          and case['retry'] == {'same_key': True,
                                'resolution': 'lookup_request_key'},
          'ambiguous_write_misclassified')


def _search(case):
    state = case['state']
    _need(type(case['complete']) is bool,
          'search_state_malformed')
    if state == 'ready_empty':
        _need(type(case['status']) is int and case['status'] == 200
              and case['complete'] is True
              and case['freshness'] == 'current' and case.get('results') == []
              and 'error' not in case, 'false_empty_search')
    elif state == 'pending':
        _need(type(case['status']) is int and case['status'] == 503
              and case['complete'] is False
              and case['freshness'] == 'lagging' and 'results' not in case
              and case['error'] == {'code':'capability_unavailable',
                                    'retryable':True}, 'lag_as_empty_search')
    elif state == 'partial':
        _need(case['status'] is None and case['complete'] is False
              and case['wire_state'] == 'proposed_unbound'
              and case['freshness'] == 'lagging'
              and isinstance(case.get('results'), list)
              and case['error'] == {'code':'partial_result','retryable':True},
              'partial_as_complete_search')
    elif state == 'unavailable':
        _need(type(case['status']) is int and case['status'] == 503
              and case['complete'] is False
              and case['freshness'] == 'disabled' and 'results' not in case
              and case['error'] == {'code':'capability_unavailable',
                                    'retryable':False}, 'unavailable_as_empty_search')
    else:
        raise ContractRefusal('unknown_search_state')


def _alone(case):
    kind = case['kind']
    if kind == 'scope_refusal':
        request = case['request']
        _need(_uuid(request['principal_project'])
              and _uuid(request['target_project'])
              and type(request['writer_authorized']) is bool
              and (request['principal_project'] != request['target_project']
                   or request['writer_authorized'] is False)
              and case['response'] == {'status':403,'error':'forbidden'}
              and case['committed'] is False, 'scope_refusal_bypassed')
    elif kind == 'off':
        _need(case['selected'] is False and case['calls'] == []
              and case['outcome'] == 'off', 'off_made_remote_call')
    elif kind == 'session_mirror':
        request = case['request']
        stamps = case['timestamps']
        _need(case['opt_in'] is True and _uuid(case['project_id'])
              and _uuid(case['session_uuid'])
              and isinstance(case['source_path'], str)
              and case['source_path'].startswith('/')
              and request['project_id'] == case['project_id']
              and request['session_uuid'] == case['session_uuid']
              and request['source_path'] == case['source_path']
              and type(request['message_count']) is int
              and request['message_count'] == len(stamps)
              and len(stamps) > 0, 'session_mirror_unbound')
        for stamp in stamps:
            _need(isinstance(stamp, str) and 'T' in stamp
                  and datetime.fromisoformat(stamp.replace('Z','+00:00')).tzinfo
                  is not None, 'non_iso_session_timestamp')
    elif kind == 'stale_recall':
        requested, current = case['requested_context'], case['current_context']
        _need(_uuid(requested['project_id']) and _uuid(current['project_id'])
              and type(requested['connection_epoch']) is int
              and type(current['connection_epoch']) is int
              and requested != current
              and isinstance(case['response'].get('results'), list)
              and case['decision'] == 'discarded', 'stale_recall_displayed')
    else:
        raise ContractRefusal('unknown_alone_case')


def _route_exchange(case):
    key = (case['method'], case['path'])
    _need(key in OBSERVED and case['operation_id'] == _operation(
        json.loads((CONTRACTS/'openapi.json').read_text()), case),
          'unobserved_route_exchange')
    request, success, error = (case[name] for name in
                               ('request', 'success', 'error'))
    _need(case['binding'] == 'proposed_new_consumer'
          and case['runtime_state'] == 'not_executed'
          and request['method'] == case['method']
          and request['path'] == case['path'],
          'route_exchange_mislabelled')
    headers = request['headers']
    _need(isinstance(headers, dict), 'request_headers_invalid')
    if key == ('GET', '/health'):
        _need(headers == {}, 'health_requires_no_credential')
    else:
        _need(headers.get('Authorization') == 'Bearer <fixture-scoped-token>'
              and headers.get('X-Project') == 'fixture-project',
              'request_scope_unbound')
    if case['method'] == 'POST':
        _need(headers.get('Content-Type') == 'application/json'
              and isinstance(request.get('body'), dict)
              and request['body'], 'request_body_invalid')
    else:
        _need('body' not in request, 'read_must_not_mutate')
    _need(type(success['status']) is int and 200 <= success['status'] < 300
          and isinstance(success['body'], dict) and success['body'],
          'success_exchange_invalid')
    _need(type(error['status']) is int and 400 <= error['status'] < 600
          and isinstance(error['body'], dict)
          and isinstance(error['body'].get('error'), dict)
          and isinstance(error['body']['error'].get('code'), str)
          and error['body']['error']['code']
          and type(error['body']['error'].get('retryable')) is bool,
          'typed_error_missing')
    if case['path'] in ('/memory', '/artifacts', '/sessions/ingest'):
        _need(isinstance(request.get('idempotency_key'), str)
              and request['idempotency_key'], 'write_identity_missing')
        _receipt(success['body'].get('receipt'), request['idempotency_key'])
    if case['path'] == '/search':
        body = success['body']
        _need(body.get('results') == [] and body.get('complete') is True
              and body.get('freshness') == {'state':'current','complete':True}
              and error['body']['error']['code'] == 'capability_unavailable'
              and 'results' not in error['body'], 'search_exchange_false_empty')


def validate_case(case):
    """Validate a frozen semantic trace; does not execute a client or server."""
    try:
        _need(isinstance(case, dict) and isinstance(case['id'], str)
              and case['id'], 'invalid_case')
        kind = case['kind']
        if kind == 'write_ack_read':
            _write_ack_read(case)
        elif kind == 'idempotency':
            _idempotency(case)
        elif kind == 'timeout':
            _timeout(case)
        elif kind == 'search_state':
            _search(case)
        elif kind == 'route_exchange':
            _route_exchange(case)
        else:
            _alone(case)
        return case['id']
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, ContractRefusal):
            raise
        raise ContractRefusal('case_malformed') from None
