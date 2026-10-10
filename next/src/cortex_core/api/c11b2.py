"""Credentialed C11 read bindings for Nemo's PostgreSQL search and graph ports."""
import hashlib
from urllib.parse import parse_qs
from uuid import UUID

from cortex_core.auth import AuthError
from .c11b import _credential
from cortex_core.embeddings.pg_search import (
    CapabilityUnavailable, EmbeddingIdentity, PostgresSearch, Scope as SearchScope,
)
from cortex_core.modules.graph.pg_graph import GraphScope, GraphUnavailable, PostgresGraph

PROBE_VECTOR = [1.0] + [0.0] * 767


class InvalidRequest(ValueError):
    code = 'invalid_input'


def _one(parameters, key, default=None):
    values = parameters.get(key)
    if values is None:
        return default
    if len(values) != 1:
        raise InvalidRequest('duplicate query parameter')
    return values[0]


def _integer(value, maximum, minimum=1):
    try:
        result = int(value)
    except (ValueError, TypeError):
        raise InvalidRequest('invalid page limit') from None
    if isinstance(value, bool) or not minimum <= result <= maximum:
        raise InvalidRequest('invalid page limit')
    return result


def _boolean(value):
    if value in ('true', '1', True):
        return True
    if value in ('false', '0', False):
        return False
    raise InvalidRequest('invalid boolean selector')


class C11b2ReadPort:
    """Recheck C04 on each Nemo query and again before any result is emitted."""

    def __init__(self, record_port, pool, identity, query_vector, extractor_identity,
                 project_repos):
        if (not isinstance(identity, EmbeddingIdentity) or not callable(query_vector)
                or not isinstance(project_repos, dict)
                or not all(k in record_port.projects and isinstance(v, str) and v
                           for k, v in project_repos.items())):
            raise ValueError('C11b2 read ports are incomplete')
        self.record_port, self.pool = record_port, pool
        self.identity, self.query_vector = identity, query_vector
        self.extractor_identity, self.project_repos = extractor_identity, dict(project_repos)

    async def principal(self, scope):
        value = await self.record_port.principal(scope)
        return {**value, '_request_scope': scope}

    async def _current(self, principal):
        request = principal['_request_scope']
        # Search is a read even though the released API uses POST.
        read_request = {**request, 'method': 'GET'}
        value = await self.record_port.principal(read_request)
        if any(value[key] != principal[key] for key in
               ('principal_id', 'project_id', 'tenant_id', 'permission_generation')):
            raise AuthError('forbidden')
        return value

    def _ports(self, principal):
        async def search_authorize(_conn, subject):
            await self._current(principal)
            if subject != principal['principal_id']:
                raise PermissionError('principal changed')
            request = principal['_request_scope']
            digest = hashlib.sha256(_credential(request)).hexdigest()
            installation = self.record_port.installation_id
            project = UUID(principal['project_id'])
            bound = await _conn.fetchrow('SELECT * FROM auth.resolve_scope($1,$2,$3,$4)',
                                         digest, installation, project, 'read')
            if (bound is None or str(bound['principal_id']) != principal['principal_id']
                    or str(bound['tenant_id']) != principal['tenant_id']
                    or bound['permission_generation'] != principal['permission_generation']):
                raise AuthError('forbidden')
            if not await _conn.fetchval('SELECT auth.bind_scope($1,$2,$3,$4)',
                                        digest, installation, project, 'read'):
                raise AuthError('forbidden')
            names = ['cortex.credential_digest', 'cortex.installation_id',
                     'cortex.project_id', 'cortex.action', 'cortex.tenant_id',
                     'cortex.principal_id', 'cortex.permission_generation']
            values = [digest, str(installation), str(project), 'read',
                      principal['tenant_id'], principal['principal_id'],
                      str(principal['permission_generation'])]
            await _conn.execute('SELECT set_config(name,value,true) FROM '
                                'unnest($1::text[],$2::text[]) AS bound(name,value)',
                                names, values)
            return SearchScope(UUID(principal['tenant_id']), UUID(principal['project_id']))

        async def graph_authorize(_conn, subject):
            scope = await search_authorize(_conn, subject)
            project_key = next((key for key, value in self.record_port.projects.items()
                                if value == scope.project_id), None)
            if project_key not in self.project_repos:
                raise PermissionError('graph project is not registered')
            return GraphScope(scope.tenant_id, scope.project_id, project_key,
                              self.project_repos[project_key])

        async def deny(_conn, _subject):
            raise PermissionError('graph mutation port is not bound')

        search = PostgresSearch(self.pool, search_authorize)
        graph = PostgresGraph(self.pool, authorize_read=graph_authorize,
                              authorize_control=deny, authorize_writer=deny,
                              extractor_identity=self.extractor_identity)
        return search, graph

    async def permission_recheck(self, principal, _capability):
        await self._current(principal)
        return True

    async def _core_pending(self, search, principal):
        """Refuse success if a committed Core head has not reached Nemo's source list."""
        async with search._request(principal['principal_id']) as (conn, scope):
            return await conn.fetchval('''SELECT count(*) FROM core.records r
                LEFT JOIN retrieval.search_sources s ON
                  (s.tenant_id,s.project_id,s.record_id)=(r.tenant_id,r.project_id,r.id::text)
                WHERE r.tenant_id=$1 AND r.project_id=$2 AND NOT r.tombstone
                  AND (s.record_id IS NULL OR s.source_revision<>r.current_revision)''',
                scope.tenant_id, scope.project_id)

    async def _post_read_freshness(self, result, principal, *, search=None):
        """Require the returned projection to remain current after the gate."""
        fresh = result.freshness if search is not None else result.get('freshness')
        state = getattr(fresh, 'status', None) if search is not None else (
            fresh.get('state') if isinstance(fresh, dict) else None)
        pending = getattr(fresh, 'pending_records', None) if search is not None else (
            fresh.get('pending_records') if isinstance(fresh, dict) else None)
        if state != 'current' or type(pending) is not int or pending != 0:
            if search is not None:
                raise CapabilityUnavailable('index_lagging', fresh)
            raise GraphUnavailable('index_lagging')
        if search is not None and await self._core_pending(search, principal):
            raise CapabilityUnavailable('index_lagging', fresh)

    async def capability_source(self, name, principal):
        search, graph = self._ports(principal)
        try:
            if name == 'pg_search':
                result = await search.search(principal['principal_id'], self.identity,
                                             PROBE_VECTOR, limit=1)
                fresh = result.freshness
                if fresh.pending_records or await self._core_pending(search, principal):
                    return {'state': 'lagging'}
                return {'state': 'ready', 'model_id': self.identity.key,
                        'generation': self.identity.key,
                        'freshness': {'complete': True, 'applied_cursor':
                                      f'indexed:{fresh.indexed_records}',
                                      'indexed_records': fresh.indexed_records,
                                      'pending_records': fresh.pending_records}}
            if name == 'pg_graph':
                stats = await graph.stats(principal['principal_id'])
                fresh = stats['freshness']
                if fresh['pending_records']:
                    return {'state': 'lagging'}
                return {'state': 'ready', 'model_id': self.extractor_identity,
                        'generation': fresh['generation'],
                        'freshness': {'complete': True,
                                      'applied_cursor': fresh['generation'], **fresh}}
        except (CapabilityUnavailable, GraphUnavailable):
            return {'state': 'unavailable'}
        return {'state': 'unavailable'}

    async def search(self, principal, scope, _request_key):
        if scope['method'] == 'POST':
            body = scope.get('_c11b_body')
            if not isinstance(body, dict) or set(body) - {'query', 'top_k', 'rerank', 'room', 'hall', 'enable_graph'}:
                raise InvalidRequest('invalid search body')
            query = body.get('query')
            limit = _integer(body.get('top_k', 25), 100)
            rerank = _boolean(body.get('rerank', True))
            graph_requested = _boolean(body.get('enable_graph', False))
            hall, room = body.get('hall', 'project'), body.get('room')
        else:
            try:
                params = parse_qs(scope.get('query_string', b'').decode('utf-8'),
                                  keep_blank_values=True, strict_parsing=True)
            except (UnicodeError, ValueError):
                raise InvalidRequest('invalid search query') from None
            if set(params) - {'q', 'limit', 'rerank', 'room', 'hall', 'graph', 'type'}:
                raise InvalidRequest('unsupported search selector')
            query = _one(params, 'q')
            limit = _integer(_one(params, 'limit', '20'), 100)
            rerank = _boolean(_one(params, 'rerank', 'true'))
            graph_requested = _boolean(_one(params, 'graph', 'false'))
            hall, room = _one(params, 'hall', 'project'), _one(params, 'room')
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 512 or hall != 'project' or room is not None:
            raise InvalidRequest('unsupported search scope')
        search, _graph = self._ports(principal)
        vector = await self.query_vector(query, self.identity)
        result = await search.search(principal['principal_id'], self.identity, vector, limit=limit)
        await self._post_read_freshness(result, principal, search=search)
        degraded = ['rerank'] if rerank else []
        if graph_requested:
            # There is no released C11 graph-fusion port. Do not claim fused hits.
            raise CapabilityUnavailable('graph_fusion_unbound')
        await self._current(principal)
        return {'query': query, 'results': [{'id': hit.record_id, 'kind': hit.kind,
                 'revision': hit.source_revision, 'distance': hit.distance}
                 for hit in result.hits], 'degraded': degraded, 'reranked': False,
                'hall': hall, 'room': room, 'graph': graph_requested,
                'freshness': {'state': result.freshness.status,
                              'pending_records': result.freshness.pending_records,
                              'indexed_records': result.freshness.indexed_records,
                              'identity': result.freshness.identity}}

    async def graph_search(self, principal, scope, _request_key):
        try:
            params = parse_qs(scope.get('query_string', b'').decode('utf-8'),
                              keep_blank_values=True, strict_parsing=True)
        except (UnicodeError, ValueError):
            raise InvalidRequest('invalid graph query') from None
        if set(params) - {'q', 'limit', 'expand', 'high', 'low', 'depth'}:
            raise InvalidRequest('unsupported graph selector')
        query = _one(params, 'q')
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 512:
            raise InvalidRequest('invalid graph query')
        limit = _integer(_one(params, 'limit', '100'), 500)
        options = {key: _boolean(_one(params, key, 'false')) for key in ('expand', 'high', 'low')}
        depth = _integer(_one(params, 'depth', '1'), 3, 0)
        _, graph = self._ports(principal)
        result = await graph.search(principal['principal_id'], query, limit=limit,
                                    depth=depth, **options)
        await self._post_read_freshness(result, principal)
        await self._current(principal)
        return result

    async def graph_stats(self, principal, _scope, _request_key):
        _, graph = self._ports(principal)
        result = await graph.stats(principal['principal_id'])
        await self._post_read_freshness(result, principal)
        await self._current(principal)
        return result

    async def graph_repository_stats(self, principal, _scope, _request_key):
        _, graph = self._ports(principal)
        result = await graph.repository_stats(principal['principal_id'])
        await self._post_read_freshness(result, principal)
        await self._current(principal)
        return result

    async def graph_memory(self, principal, scope, _request_key):
        try:
            params = parse_qs(scope.get('query_string', b'').decode('utf-8'),
                              keep_blank_values=True, strict_parsing=True)
        except (UnicodeError, ValueError):
            raise InvalidRequest('invalid graph query') from None
        if set(params) - {'limit'}:
            raise InvalidRequest('unsupported graph selector')
        limit = _integer(_one(params, 'limit', '500'), 1000)
        _, graph = self._ports(principal)
        result = await graph.memory(principal['principal_id'], limit=limit)
        await self._post_read_freshness(result, principal)
        await self._current(principal)
        return result

    @property
    def handlers(self):
        return {'C01-R021': self.search, 'C01-R022': self.search,
                'C01-R023': self.graph_search, 'C01-R024': self.graph_stats,
                'C01-R025': self.graph_memory, 'C01-R034': self.graph_repository_stats}
