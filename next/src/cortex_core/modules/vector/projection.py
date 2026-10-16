"""Closed external-v2 complete-point contract. No engine or Core authority here."""
from dataclasses import dataclass
import hashlib
import json
import re
from uuid import UUID

from cortex_core.embeddings.pg_search import vector_literal
from cortex_core.embeddings.projection_revision import EmbeddingIdentity
from cortex_core.runtime.contracts import decode


@dataclass(frozen=True)
class ProjectionBinding:
    installation_id: UUID
    tenant_id: UUID
    project_id: UUID
    module_id: str
    consumer_generation: int
    collection_generation: UUID
    identity: EmbeddingIdentity

    def __post_init__(self):
        if (any(type(v) is not UUID or v.int == 0 for v in
                (self.installation_id, self.tenant_id, self.project_id, self.collection_generation))
                or type(self.module_id) is not str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,127}', self.module_id)
                or type(self.consumer_generation) is not int or not 0 < self.consumer_generation < 2**63
                or type(self.identity) is not EmbeddingIdentity):
            raise ValueError('invalid_projection_binding')
        self.identity.key

    @property
    def collection(self):
        return '/collections/cortex-' + self.collection_generation.hex


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value or UUID(value).int == 0:
        raise ValueError('invalid_projection_identity')
    return value


def _revision(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError('invalid_projection_revision')
    return value


def _sha(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('invalid_projection_digest')
    return value


def point_key(binding, aggregate_id):
    if type(binding) is not ProjectionBinding or type(aggregate_id) is not UUID or not aggregate_id.int:
        raise ValueError('invalid_projection_key')
    return dict(installation_id=str(binding.installation_id), tenant_id=str(binding.tenant_id),
                project_id=str(binding.project_id), aggregate_id=str(aggregate_id),
                model_identity=binding.identity.key, module_id=binding.module_id,
                consumer_generation=binding.consumer_generation,
                collection_generation=str(binding.collection_generation))


def point_id(key):
    # Full key remains in payload and is checked on every read: truncated ID is
    # an address, never collision evidence or an authorization boundary.
    return str(UUID(hex=_digest(key)[:32], version=5))


def _event(binding, event):
    e = event.envelope
    if type(e) is not dict or type(event.payload) is not bytes:
        raise ValueError('invalid_projection_event')
    aggregate = UUID(_uuid(e['aggregate_id']))
    key = point_key(binding, aggregate)
    if (any(_uuid(e[k]) != key[k] for k in ('installation_id','tenant_id','project_id'))
            or type(e['schema_version']) is not int or e['schema_version'] != 1
            or type(e['tombstone']) is not bool
            or e['operation'] != ('delete' if e['tombstone'] else 'upsert')
            or type(e['aggregate_kind']) is not str or not 1 <= len(e['aggregate_kind']) <= 64
            or hashlib.sha256(event.payload).hexdigest() != _sha(e['payload_sha256'])):
        raise ValueError('invalid_projection_event')
    _revision(e['aggregate_revision']); _uuid(e['event_id'])
    return key, e


def _vector(vector, dimensions):
    # Reuse the pgvector float32/nonzero/norm admission and the ONE strict JSON
    # decoder. Both engines receive the same normalized components.
    return decode(vector_literal(vector, dimensions).encode(), limit=65536, text_payload=True)


def build_point(binding, event, vector):
    key, e = _event(binding, event)
    values = _vector(vector, binding.identity.dimensions)
    payload = key | dict(revision=e['aggregate_revision'], event_id=e['event_id'],
                        payload_sha256=e['payload_sha256'], tombstone=e['tombstone'],
                        aggregate_kind=e['aggregate_kind'])
    payload['effect_sha256'] = _digest(dict(payload=payload, vector=values))
    return dict(id=point_id(key), payload=payload, vector=values)


def validate_point(point, binding, aggregate_id):
    key = point_key(binding, aggregate_id)
    required = set(key) | {'revision','event_id','payload_sha256','tombstone','aggregate_kind','effect_sha256'}
    if (type(point) is not dict or set(point) != {'id','payload','vector'}
            or type(point['id']) is not str or point['id'] != point_id(key)
            or type(point['payload']) is not dict or set(point['payload']) != required):
        raise ValueError('invalid_projection_point')
    payload = point['payload']
    if (any(type(payload[k]) is not type(v) or payload[k] != v for k,v in key.items())
            or type(payload['tombstone']) is not bool
            or type(payload['aggregate_kind']) is not str or not 1 <= len(payload['aggregate_kind']) <= 64):
        raise ValueError('invalid_projection_point')
    _revision(payload['revision']); _uuid(payload['event_id']); _sha(payload['payload_sha256'])
    vector = _vector(point['vector'], binding.identity.dimensions)
    body = {k:v for k,v in payload.items() if k != 'effect_sha256'}
    if _sha(payload['effect_sha256']) != _digest(dict(payload=body, vector=vector)):
        raise ValueError('invalid_projection_point')
    return dict(id=point['id'], payload=dict(payload), vector=vector)


def make_receipt(binding, event, desired, observed, operation_id):
    payload = observed['payload']; e = event.envelope
    receipt = dict(version=2, sink='qdrant', key=point_key(binding, UUID(e['aggregate_id'])),
        point_id=observed['id'], event_id=e['event_id'], aggregate_id=e['aggregate_id'],
        revision=e['aggregate_revision'], tombstone=e['tombstone'], payload_sha256=e['payload_sha256'],
        generation=binding.consumer_generation, stale=payload['revision'] > e['aggregate_revision'],
        effect_sha256=desired['payload']['effect_sha256'],
        observed={k:payload[k] for k in ('revision','event_id','payload_sha256','tombstone','effect_sha256')},
        proof='completed_keyed_readback', operation_id=operation_id)
    validate_receipt(receipt, binding, event)
    return receipt


def validate_receipt(receipt, binding, event):
    key, e = _event(binding, event)
    required = {'version','sink','key','point_id','event_id','aggregate_id','revision','tombstone',
                'payload_sha256','generation','stale','effect_sha256','observed','proof','operation_id'}
    if (type(receipt) is not dict or set(receipt) != required
            or type(receipt['version']) is not int or receipt['version'] != 2
            or receipt['sink'] != 'qdrant' or receipt['proof'] != 'completed_keyed_readback'
            or type(receipt['key']) is not dict or set(receipt['key']) != set(key)
            or any(type(receipt['key'][k]) is not type(v) or receipt['key'][k] != v for k,v in key.items())
            or receipt['point_id'] != point_id(key)
            or type(receipt['operation_id']) is not int or receipt['operation_id'] < 0
            or type(receipt['generation']) is not int or receipt['generation'] != binding.consumer_generation
            or type(receipt['tombstone']) is not bool or type(receipt['stale']) is not bool
            or receipt['tombstone'] != e['tombstone']
            or _revision(receipt['revision']) != e['aggregate_revision']
            or _uuid(receipt['event_id']) != e['event_id']
            or _uuid(receipt['aggregate_id']) != e['aggregate_id']
            or _sha(receipt['payload_sha256']) != e['payload_sha256']):
        raise ValueError('invalid_external_receipt')
    _sha(receipt['effect_sha256'])
    observed = receipt['observed']
    if (type(observed) is not dict or set(observed) !=
            {'revision','event_id','payload_sha256','tombstone','effect_sha256'}
            or type(observed['tombstone']) is not bool):
        raise ValueError('invalid_external_receipt')
    _revision(observed['revision']); _uuid(observed['event_id'])
    _sha(observed['payload_sha256']); _sha(observed['effect_sha256'])
    if receipt['stale']:
        if observed['revision'] <= receipt['revision']:
            raise ValueError('invalid_external_receipt')
    elif any(observed[k] != receipt[k] for k in observed):
        raise ValueError('invalid_external_receipt')
    return 'stale' if receipt['stale'] else 'applied'
