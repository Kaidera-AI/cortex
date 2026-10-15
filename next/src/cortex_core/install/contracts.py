"""Shared operational bytes and closed types; parsing never confers effects.

R01 owns the codec. RC2 owns the only signed-member capability. This module
checks operational shape/original references, not signatures or live custody.
"""
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from cortex_core.release.codec import WireValidator, canonical_bytes, decode_json, relative_path, sha256
from cortex_core.release.contracts import freeze

SCHEMA = 'kaidera.install-operation.v1'
LIMITS = {'request': 1048576, 'receipt': 8388608, 'journal_event': 1048576}


class InstallRefusal(ValueError):
    """Stable, nonsecret operational reason; never include candidate/exception text."""

    def __init__(self, reason_code, *, unit=None):
        self.reason_code = reason_code
        self.unit = unit
        super().__init__(reason_code)


@dataclass(frozen=True)
class ParsedInstallMessage:
    raw: bytes
    sha256: str
    document: object
    effect_authority: bool = False
    native_qualified: bool = False


def _require(value, reason='bad_shape'):
    if not value:
        raise InstallRefusal(reason)


def absolute_path(value):
    _require(type(value) is str and value.startswith('/') and value != '/' and len(value.encode('utf-8')) <= 4096)
    _require('\\' not in value and '//' not in value and not value.endswith('/')
             and not any(part in ('.', '..') for part in value.split('/')))
    return value


def measure_original_ref(record, raw):
    _require(type(record) is dict and set(record) == {'path', 'bytes', 'sha256'})
    _require(type(record['path']) is str and type(record['sha256']) is str
             and len(record['sha256']) == 64 and all(c in '0123456789abcdef' for c in record['sha256']))
    try:
        relative_path(record['path'])
    except ValueError:
        raise InstallRefusal('bad_shape') from None
    _require(type(record['bytes']) is int and record['bytes'] >= 0)
    _require(type(raw) is bytes and len(raw) == record['bytes'] and sha256(raw) == record['sha256'], 'wrong_release')
    return raw


def _depth(value, level=0):
    _require(level <= 32)
    if isinstance(value, dict):
        for key, child in value.items():
            _depth(key, level + 1)
            _depth(child, level + 1)
    elif isinstance(value, list):
        for child in value:
            _depth(child, level + 1)


@lru_cache(maxsize=1)
def _schema():
    path = Path(__file__).resolve().parents[3] / 'contracts/install/operation.schema.json'
    try:
        return decode_json(path.read_bytes(), 1048576, canonical=False)
    except (OSError, ValueError):
        raise InstallRefusal('missing_contract') from None


def _paths(value):
    if isinstance(value, dict):
        if set(value) == {'path', 'bytes', 'sha256'}:
            try:
                relative_path(value['path'])
            except ValueError:
                raise InstallRefusal('bad_shape') from None
        for key, child in value.items():
            if key in ('absolute_path', 'cwd_root', 'executable_path', 'canonical_path', 'link_target') and child is not None:
                absolute_path(child)
            _paths(child)
    elif isinstance(value, list):
        for child in value:
            _paths(child)


def _time(value):
    _require(type(value) is str and (value.endswith('Z') or value.endswith('+00:00')))
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise InstallRefusal('bad_shape') from None
    _require(result.tzinfo is not None and result.utcoffset().total_seconds() == 0)
    return result


def _invocation(value):
    outcome = value['outcome']
    if outcome == 'not_executed':
        _require(all(value[key] is None for key in ('exit_code', 'signal', 'executable_witness_ref', 'started_at', 'ended_at')))
        _require(value['not_executed_reason'] is not None)
    else:
        _require(value['executable_witness_ref'] is not None and value['not_executed_reason'] is None)
        _require(_time(value['ended_at']) >= _time(value['started_at']))
        if outcome == 'exited':
            _require(type(value['exit_code']) is int and 0 <= value['exit_code'] <= 255 and value['signal'] is None)
        else:
            _require(value['exit_code'] is None and type(value['signal']) is int and 1 <= value['signal'] <= 64)


def parse_install_message(original_bytes, expected_kind=None, admitted_context=None):
    """Return immutable original operational bytes. Context is not authority."""
    try:
        value = decode_json(original_bytes, max(LIMITS.values()))
    except (ValueError, TypeError, RecursionError):
        raise InstallRefusal('bad_shape') from None
    _depth(value)
    _require(type(value) is dict and type(value.get('kind')) is str and value['kind'] in LIMITS)
    kind = value['kind']
    _require(len(original_bytes) <= LIMITS[kind] and (expected_kind is None or kind == expected_kind))
    _require(next(WireValidator(_schema()).iter_errors(value), None) is None)
    _paths(value)
    if kind != 'journal_event':
        caller = value['caller']
        _require((caller['route'] == 'manual' and caller['parent_product'] is None and caller['parent_transaction_id'] is None)
                 or (caller['route'] == 'kos_prerequisite' and caller['parent_product'] == 'kos' and caller['parent_transaction_id'] is not None))
        roots = value['roots']
        _require(len({r['purpose'] for r in roots}) == len(roots) and len({r['absolute_path'] for r in roots}) == len(roots))
        paths = [r['absolute_path'] for r in roots]
        _require(not any(a != b and b.startswith(a + '/') for a in paths for b in paths))
    if kind == 'request':
        _require((value['operation'] == 'purge') == (value['purge_selection'] is not None))
        local = value['acquisition']['local_members']
        _require(len({r['path'] for r in local}) == len(local))
    elif kind == 'receipt':
        _invocation(value['invocation_result'])
        if value['readiness']['state'] == 'ready':
            _require(value['product_state'] is not None and value['disposition'] in ('created_by_this_transaction', 'verified_owned_continuation')
                     and all(value['readiness'][k] is not None for k in ('descriptor_ref', 'connection_custody_ref', 'checks_ref')))
        ids = [r['resource_id'] for r in value['resources']]
        _require(len(ids) == len(set(ids)))
        if value['result'] == 'PASS' and value['invocation_result']['outcome'] != 'not_executed':
            _require(value['invocation_result']['outcome'] == 'exited' and value['invocation_result']['exit_code'] == 0)
        for resource in value['resources']:
            if resource['ownership'] == 'external_read_only':
                _require(all(resource[k] is None for k in ('created_by', 'intent_event_ref', 'result_event_ref')))
            else:
                _require(all(resource[k] is not None for k in ('created_by', 'intent_event_ref', 'result_event_ref')))
            if resource['data_class'] == 'credential_custody':
                _require(resource['after_witness'] is None or resource['after_witness']['content_ref'] is None)
    else:
        _time(value['recorded_at'])
        _require((value['sequence'] == 0) == (value['previous_event_ref'] is None))
    return ParsedInstallMessage(original_bytes, sha256(original_bytes), freeze(value))


def validate_journal(events, origin_request, receipt_records=None, live_readers=None):
    """Read-only sequence/original-lineage check, never repair/replay a journal.

    This P1 byte check is not a native recovery capability. P2 supplies live
    ownership/idempotency/lock admission and separately scoped effects.
    """
    request = parse_install_message(origin_request, expected_kind='request')
    result = []
    previous = None
    for sequence, raw in enumerate(events):
        event = parse_install_message(raw, expected_kind='journal_event')
        doc = event.document
        _require(doc['sequence'] == sequence and dict(doc['identity']) == dict(request.document['identity']), 'state_drift')
        _require(doc['origin_request_ref']['sha256'] == request.sha256 and doc['origin_request_ref']['bytes'] == len(origin_request), 'state_drift')
        if previous is not None:
            _require(doc['previous_event_ref']['sha256'] == previous.sha256 and doc['previous_event_ref']['bytes'] == len(previous.raw), 'state_drift')
        previous = event
        result.append(event)
    _require(bool(result), 'state_drift')
    return tuple(result)


def admit_install_request(original_bytes, **context):
    """P1 is an effect-free source plan. The sole member seam stays in planner."""
    from .planner import plan
    return plan(original_bytes, **context)
