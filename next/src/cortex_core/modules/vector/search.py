"""Controlled C10/C11 source binding. Native/runtime capability stays UNAVAILABLE.

Core snapshots come only through the existing credentialed _C04Search request
transaction. The engine supplies candidates, never scope, permission or freshness.
"""
from dataclasses import dataclass
from uuid import UUID

from cortex_core.embeddings.projection_revision import (
    CapabilityUnavailable, Freshness, Hit, SearchResult, ProjectionRevision,
    finish_observation, validate_target,
)
from cortex_core.runtime.pg_adapters import _C04Search, _json
from .projection import ProjectionBinding, binding_contract
from .qdrant import ProtocolFailure
from .qdrant_client import PrivateClient


@dataclass(frozen=True)
class QdrantProjection:
    binding: ProjectionBinding
    client: PrivateClient

    def __post_init__(self):
        if type(self.binding) is not ProjectionBinding or type(self.client) is not PrivateClient:
            raise ValueError('invalid_vector_projection')

    def bind(self, core):
        return QdrantSearch(core,self)


class QdrantSearch:
    external_projection = True

    def __init__(self, core, projection):
        if type(core) is not _C04Search or type(projection) is not QdrantProjection:
            raise ValueError('credentialed_core_projection_required')
        binding = projection.binding; expected = core._material.expected
        if (binding.installation_id,binding.tenant_id,binding.project_id) != (
                expected.installation_id,expected.tenant_id,expected.project_id):
            raise PermissionError('Projection is outside the current Core scope')
        self.core, self.projection = core, projection
        self._material = core._material
        self._request = core._request  # The existing C11 query-cache/status boundary.

    def _identity(self, identity):
        if identity != self.projection.binding.identity:
            raise CapabilityUnavailable('identity_mismatch')

    def _binding_matches(self, value):
        expected = binding_contract(self.projection.binding)
        return (type(value) is dict and set(value) == set(expected)
                and all(type(value[k]) is type(v) and value[k] == v for k,v in expected.items()))

    async def _observation(self, connection, ids=()):
        value = _json(await connection.fetchval('SELECT coordination.c09_state($1,$2::uuid[])',
            self.projection.binding.module_id,list(ids)))
        required = {'binding','indexed_records','pending_records','targets'}
        if (type(value) is not dict or set(value) != required
                or not self._binding_matches(value['binding'])
                or any(type(value[k]) is not int or not 0 <= value[k] < 2**63
                       for k in ('indexed_records','pending_records'))
                or type(value['targets']) is not list or len(value['targets']) > len(ids)):
            raise CapabilityUnavailable('projection_state_unavailable')
        found = set()
        for target in value['targets']:
            if (type(target) is not dict or set(target) != {'record_id','kind','revision','tombstone',
                    'indexed_revision','event_id','payload_sha256','effect_sha256','point_id','indexed'}
                    or target['record_id'] not in {str(i) for i in ids} or target['record_id'] in found
                    or type(target['kind']) is not str or not 1 <= len(target['kind']) <= 64
                    or type(target['revision']) is not int or not 0 < target['revision'] < 2**63
                    or type(target['tombstone']) is not bool or type(target['indexed']) is not bool
                    or target['indexed_revision'] is not None and
                       (type(target['indexed_revision']) is not int or not 0 < target['indexed_revision'] < 2**63)):
                raise CapabilityUnavailable('projection_state_unavailable')
            found.add(target['record_id'])
        return value

    async def _snapshot(self, subject, ids=()):
        async with self._request(subject) as (connection, scope):
            value = await self._observation(connection,ids)
        return value, scope  # Private until C04 has revalidated and exited.

    async def _state(self, connection, scope, key):
        if key != self.projection.binding.identity.key:
            raise CapabilityUnavailable('identity_mismatch')
        await self._observation(connection)
        return 'unavailable'  # A controlled Core snapshot never grants capability READY.

    async def _freshness(self, connection, scope, key):
        if key != self.projection.binding.identity.key:
            raise CapabilityUnavailable('identity_mismatch')
        value = await self._observation(connection)
        return self._fresh(value)

    def _fresh(self, value):
        return Freshness(self.projection.binding.identity.key,value['indexed_records'],value['pending_records'])

    async def pending(self, subject):
        value,_ = await self._snapshot(subject)
        return value['pending_records']

    @staticmethod
    def _matches(point, target):
        payload = point['payload']
        return (target is not None and target['indexed'] and not target['tombstone']
                and target['indexed_revision'] == target['revision']
                and target['record_id'] == payload['aggregate_id']
                and target['kind'] == payload['aggregate_kind']
                and target['revision'] == payload['revision']
                and target['event_id'] == payload['event_id']
                and target['payload_sha256'] == payload['payload_sha256']
                and target['effect_sha256'] == payload['effect_sha256']
                and target['point_id'] == point['id'])

    async def search(self, subject, identity, vector, *, limit=20, kind=None):
        self._identity(identity)
        before,_ = await self._snapshot(subject)
        if before['pending_records']:
            raise CapabilityUnavailable('index_lagging',self._fresh(before))
        try:
            candidates = await self.projection.client.query_projection(
                self.projection.binding,vector,limit=limit,kind=kind)
        except ProtocolFailure:
            raise CapabilityUnavailable('vector_engine_unavailable') from None
        if not candidates and before['indexed_records'] and kind is None:
            raise CapabilityUnavailable('vector_result_unproved')
        ids = tuple(UUID(p['payload']['aggregate_id']) for p,_ in candidates)
        after,_ = await self._snapshot(subject,ids)
        targets = {r['record_id']:r for r in after['targets']}
        if (after['pending_records'] or self._fresh(after) != self._fresh(before)
                or any(not self._matches(p,targets.get(p['payload']['aggregate_id']))
                       for p,_ in candidates)):
            raise CapabilityUnavailable('index_lagging',self._fresh(after))
        return SearchResult(tuple(Hit(p['payload']['aggregate_id'],p['payload']['aggregate_kind'],
            p['payload']['revision'],distance) for p,distance in candidates),self._fresh(after))

    async def recheck_result(self, subject, result):
        if result.freshness.identity != self.projection.binding.identity.key:
            raise CapabilityUnavailable('identity_mismatch')
        value,_ = await self._snapshot(subject,tuple(UUID(h.record_id) for h in result.hits))
        rows = {r['record_id']:r for r in value['targets']}
        if value['pending_records'] or self._fresh(value) != result.freshness or any(
                h.record_id not in rows or not rows[h.record_id]['indexed']
                or rows[h.record_id]['tombstone'] or rows[h.record_id]['kind'] != h.kind
                or rows[h.record_id]['revision'] != h.source_revision
                or rows[h.record_id]['indexed_revision'] != h.source_revision for h in result.hits):
            raise CapabilityUnavailable('index_lagging',self._fresh(value))
        return True

    async def projection_revision(self, subject, target, identity, *, recheck):
        record_id, required = validate_target(target,recheck)
        self._identity(identity)
        def observation(state,indexed=None,reason=''):
            return ProjectionRevision(state,record_id,required,indexed,identity.key,
                generation=self.projection.binding.collection_generation,reason=reason)
        value,scope = await self._snapshot(subject,(record_id,))
        row = value['targets'][0] if value['targets'] else None
        if row is None:
            result = observation('unavailable',reason='target_unavailable')
        elif row['tombstone']:
            result = observation('unavailable',reason='tombstone')
        elif not row['indexed'] or row['revision'] < required:
            result = observation('pending',row['indexed_revision'],'index_pending')
        else:
            try:
                point = await self.projection.client.read_projection(self.projection.binding,record_id)
            except ProtocolFailure:
                point = None
            value,scope = await self._snapshot(subject,(record_id,))
            row = value['targets'][0] if value['targets'] else None
            if point is not None and self._matches(point,row) and row['revision'] >= required:
                result = observation('visible',row['indexed_revision'])
            else:
                result = observation('unavailable',reason='vector_engine_unavailable')
        return await finish_observation(recheck,subject,scope,result)
