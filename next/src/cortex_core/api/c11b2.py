"""Credentialed C11 read bindings for Nemo's PostgreSQL search and graph ports."""
import asyncio
import hashlib
from types import SimpleNamespace
from contextlib import suppress
from urllib.parse import parse_qs
from uuid import UUID

from cortex_core.auth import AuthError
from cortex_core.coordination import JobError, Jobs
from .c11b import _credential
from .c11a import KeywordResponse
from cortex_core.embeddings.keyword import (KeywordSearch, KeywordResult, current_rows,
    source_state, request_deadline, check_deadline)
from cortex_core.embeddings.pg_search import (
    CapabilityUnavailable, CoreUnavailable, EmbeddingIdentity, PostgresSearch,
)
from cortex_core.embeddings.query_cache import CacheScope, QueryEmbeddingCache
from cortex_core.modules.graph.pg_graph import GraphScope, GraphUnavailable, PostgresGraph

PROBE_VECTOR = [1.0] + [0.0] * 767


class InvalidRequest(ValueError):
    code = 'invalid_input'


class D2Unavailable(Exception):
    code = 'capability_unavailable'

    def __init__(self, state):
        self.d2_state = state
        super().__init__(state)


def _one(parameters, key, default=None):
    values = parameters.get(key)
    if values is None:
        return default
    if len(values) != 1:
        raise InvalidRequest('duplicate query parameter')
    return values[0]


def _integer(value, maximum, minimum=1):
    if not isinstance(value,(int,str)) or isinstance(value,bool):
        raise InvalidRequest('invalid page limit')
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

    def __init__(self, record_port, pool, identity, provider, extractor_identity,
                 project_repos, *, selection=None, keyword=None):
        hosted=selection is None or selection.snapshot.mode!='off'
        if ((hosted and (not isinstance(identity, EmbeddingIdentity)
                         or not callable(getattr(provider, 'embed', None))))
                or not isinstance(project_repos, dict)
                or not all(k in record_port.projects and isinstance(v, str) and v
                           for k, v in project_repos.items())):
            raise ValueError('C11b2 read ports are incomplete')
        if (not hosted and keyword is None
                and not callable(getattr(record_port,'keyword_current',None))):
            raise ValueError('canonical read source is unbound')
        if keyword is not None and not callable(getattr(keyword,'search',None)):
            raise ValueError('keyword source is invalid')
        self.record_port, self.pool = record_port, pool
        self.identity, self.provider = identity, provider
        self.selection,self.keyword=selection,keyword
        self.extractor_identity, self.project_repos = extractor_identity, dict(project_repos)

    async def search_admission(self, principal, scope):
        """OFF uses actual current Core admission, never fabricated pg_search ready."""
        selection=self.selection
        if selection is None:return None
        deadline=scope['_cortex_deadline'];revision=selection.snapshot.revision
        scope['_cortex_selection_revision']=revision
        mode=selection.snapshot.mode
        scope['_cortex_search_check']=lambda:selection.check(
            revision=revision,deadline=deadline,mode=mode)
        if selection.snapshot.mode!='off':return None
        selection.check(revision=revision,deadline=deadline,mode='off')
        if not callable(getattr(self.record_port,'keyword_current',None)):
            raise CapabilityUnavailable('canonical_read_unbound')
        await self._current(principal)
        selection.check(revision=revision,deadline=deadline,mode='off')
        await self.record_port.keyword_current(principal,scope,limit=1,deadline=deadline)
        selection.check(revision=revision,deadline=deadline,mode='off')
        return True

    def _keyword_for(self, principal, scope):
        explicit=getattr(self,'keyword',None)
        if explicit is not None:return explicit
        async def authorize(subject):
            if subject!=principal['principal_id']:raise AuthError('forbidden')
            await self._current(principal)
        async def current(subject, *, limit, deadline):
            if subject!=principal['principal_id']:raise AuthError('forbidden')
            return await self.record_port.keyword_current(principal,scope,limit=limit,deadline=deadline)
        return KeywordSearch(read_current=current,authorize=authorize)

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
            return CacheScope(UUID(principal['tenant_id']), UUID(principal['project_id']),
                              str(principal['permission_generation']))

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

    def _cache(self, search):
        return QueryEmbeddingCache(self.pool, search.authorize, self.provider.embed)

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

    async def status_degradation(self, principal, _scope, _request_key):
        """Keep observed search lag distinct from canonical-write availability."""
        await self._current(principal)
        if getattr(self,'selection',None) is not None and self.selection.snapshot.mode=='off':
            return {'search':{'state':'unavailable','reason':'hosted_disabled'},
                    'canonical_write':{'state':'unavailable','reason':'write_observation_unbound'}}
        search, _graph = self._ports(principal)
        try:
            async with search._request(principal['principal_id']) as (conn, bound):
                state = await search._state(conn, bound, self.identity.key)
                freshness = await search._freshness(conn, bound, self.identity.key)
            core_unprojected = await self._core_pending(search, principal)
            result = {'state': ('lagging' if state == 'ready' and
                                (freshness.pending_records or core_unprojected) else state),
                      'identity': self.identity.key,
                      'source_pending': freshness.pending_records,
                      'core_unprojected': core_unprojected}
        except CapabilityUnavailable as error:
            result = {'state': 'unavailable', 'reason': error.reason}
        except CoreUnavailable:
            raise
        await self._current(principal)
        return {'search': result,
                'canonical_write': {'state': 'unavailable',
                                    'reason': 'write_observation_unbound'}}

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
        if name=='pg_search' and getattr(self,'selection',None) is not None and self.selection.snapshot.mode=='off':
            return {'state':'unavailable','reason':'hosted_disabled'}
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

    @staticmethod
    def _target(body):
        if 'after' not in body:
            if 'wait_ms' in body:
                raise InvalidRequest('wait requires a record target')
            return None, 0
        value = body['after']
        if type(value) is not dict or set(value) != {'record_id', 'revision'}:
            raise InvalidRequest('invalid record target')
        try:
            record_id = UUID(value['record_id'])
        except (ValueError, TypeError, AttributeError):
            raise InvalidRequest('invalid target record id') from None
        revision = value['revision']
        if type(revision) is not int or not 0 < revision < 2**63:
            raise InvalidRequest('invalid target revision')
        wait_ms = body.get('wait_ms', 0)
        if type(wait_ms) is not int or not 0 <= wait_ms <= 10000:
            raise InvalidRequest('invalid wait bound')
        return {'record_id': record_id, 'revision': revision}, wait_ms

    async def _observe_target(self, search, principal, target):
        async def current(subject, bound):
            if (subject != principal['principal_id']
                    or str(bound.tenant_id) != principal['tenant_id']
                    or str(bound.project_id) != principal['project_id']):
                raise AuthError('scope_mismatch')
            await self._current(principal)
            return True
        result = await search.projection_revision(
            principal['principal_id'], target, self.identity, recheck=current)
        if result.state == 'unavailable':
            if result.reason == 'target_unavailable':
                raise AuthError('scope_mismatch')
            raise D2Unavailable('unavailable')
        return result

    async def _wait_target(self, search, principal, target, wait_ms, disconnected,
                           *, deadline=None, observe=None):
        wait_deadline = asyncio.get_running_loop().time() + wait_ms / 1000
        if deadline is not None:wait_deadline=min(wait_deadline,deadline)
        backoff = 0.05
        while True:
            if disconnected.is_set():
                raise asyncio.CancelledError
            if deadline is not None:check_deadline(deadline)
            observation = (await observe() if observe is not None else
                           await self._observe_target(search, principal, target))
            if deadline is not None:check_deadline(deadline)
            if observation.state == 'visible':
                return
            remaining = wait_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise D2Unavailable('pending')
            try:
                await asyncio.wait_for(disconnected.wait(), min(backoff, remaining))
            except TimeoutError:
                pass
            else:
                raise asyncio.CancelledError
            backoff = min(0.25, backoff * 2)

    @staticmethod
    async def _watch_disconnect(receive, disconnected):
        try:
            while True:
                if (await receive()).get('type') == 'http.disconnect':
                    disconnected.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            disconnected.set()

    async def search(self, principal, scope, _request_key):
        deadline=request_deadline(scope.get('_cortex_deadline',asyncio.get_running_loop().time()+10))
        selection=getattr(self,'selection',None)
        mode=selection.snapshot.mode if selection is not None else 'hosted'
        revision=scope.get('_cortex_selection_revision',selection.snapshot.revision if selection is not None else None)
        def current():
            check_deadline(deadline)
            if selection is not None:selection.check(revision=revision,deadline=deadline,mode=mode)
        try:
            async with asyncio.timeout_at(deadline):
                return await self._search(principal,scope,_request_key,deadline,mode,current)
        except TimeoutError:
            raise CapabilityUnavailable('request_timeout') from None

    async def _search(self, principal, scope, _request_key, deadline, mode, current):
        current()
        target, wait_ms = None, 0
        if scope['method'] == 'POST':
            body = scope.get('_c11b_body')
            if not isinstance(body, dict) or set(body) - {
                    'query', 'top_k', 'rerank', 'room', 'hall', 'enable_graph',
                    'after', 'wait_ms'}:
                raise InvalidRequest('invalid search body')
            target, wait_ms = self._target(body)
            query = body.get('query')
            limit = _integer(body.get('top_k', 25), 100)
            rerank = _boolean(body.get('rerank', False))
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
            rerank = _boolean(_one(params, 'rerank', 'false'))
            graph_requested = _boolean(_one(params, 'graph', 'false'))
            hall, room = _one(params, 'hall', 'project'), _one(params, 'room')
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 512 or hall != 'project' or room is not None:
            raise InvalidRequest('unsupported search scope')
        if graph_requested:raise CapabilityUnavailable('graph_fusion_unbound')
        search=None if mode=='off' else self._ports(principal)[0]
        async def observe():
            current()
            value=await self.record_port.keyword_target(principal,scope,target,deadline=deadline)
            current()
            if value is None:raise AuthError('scope_mismatch')
            if value.tombstone:raise D2Unavailable('unavailable')
            return SimpleNamespace(state='visible' if value.revision>=target['revision'] else 'pending')
        disconnected = asyncio.Event()
        watcher = None
        if target is not None:
            receive = scope.get('_c11b_receive')
            if not callable(receive):
                raise InvalidRequest('target wait requires a disconnect channel')
            watcher = asyncio.create_task(self._watch_disconnect(receive, disconnected))
        try:
            if target is not None:
                await self._wait_target(search, principal, target, wait_ms, disconnected,
                                       deadline=deadline,observe=observe if mode=='off' else None)
                current()
            if mode=='off':
                keyword=self._keyword_for(principal,scope)
                value=await keyword.search(principal['principal_id'],query,limit=limit,deadline=deadline)
                current()
                await self._current(principal)
                current()
                if isinstance(value,KeywordResult):
                    rows=await self.record_port.keyword_current(principal,scope,
                        limit=keyword.max_records+1,deadline=deadline)
                    current()
                    if source_state(current_rows(rows,keyword.max_records+1))!=value.source_state:
                        raise CapabilityUnavailable('source_changed')
                if target is not None and (await observe()).state!='visible':raise D2Unavailable('pending')
                current()
                response=KeywordResponse(query=query,results=[dict(id=h.record_id,kind=h.kind,
                    revision=h.source_revision,score=h.score) for h in value.hits],
                    degraded=['hosted_disabled']+(['rerank'] if rerank else []),reranked=False,
                    hall=hall,room=room,graph=False,complete=value.complete,
                    state=('ready' if value.hits else 'ready_empty') if value.complete else 'partial',
                    unsupported=[dict(id=u.record_id,reason=u.reason) for u in value.unsupported],
                    freshness=dict(source='canonical',state='current',complete=value.complete,
                                   scanned_records=getattr(value,'scanned_records',len(value.hits)+len(value.unsupported))))
                if not value.complete:
                    response['error']=dict(code='capability_unavailable',capability='search',state='partial',
                                          reason=value.reason,retryable=False)
                return response
            try:
                embedded = await self._cache(search).get(principal['principal_id'],
                                                         self.identity, query)
                current()
            except (CapabilityUnavailable, AuthError, PermissionError):
                raise
            except Exception:
                raise CapabilityUnavailable('provider_unavailable') from None
            result = await search.search(principal['principal_id'], self.identity,
                                         embedded.vector, limit=limit)
            current()
            await self._post_read_freshness(result, principal, search=search)
            current()
            degraded = ['rerank'] if rerank else []
            if graph_requested:
                # There is no released C11 graph-fusion port. Do not claim fused hits.
                raise CapabilityUnavailable('graph_fusion_unbound')
            if target is not None:
                observation = await self._observe_target(search, principal, target)
                if observation.state != 'visible':
                    raise D2Unavailable('pending')
                if disconnected.is_set():
                    raise asyncio.CancelledError
            await self._current(principal)
            current()
            response = {'query': query, 'results': [{'id': hit.record_id, 'kind': hit.kind,
                     'revision': hit.source_revision, 'distance': hit.distance}
                     for hit in result.hits], 'degraded': degraded, 'reranked': False,
                    'hall': hall, 'room': room, 'graph': graph_requested,
                    'freshness': {'state': result.freshness.status,
                                  'pending_records': result.freshness.pending_records,
                                  'indexed_records': result.freshness.indexed_records,
                                  'identity': result.freshness.identity,
                                  'model_identity': {'provider': self.identity.provider,
                                      'model': self.identity.model,
                                      'version': self.identity.version,
                                      'dimensions': self.identity.dimensions,
                                      'preprocessing': self.identity.preprocessing}}}
            response['complete'] = True
            response['state'] = 'ready_empty' if not response['results'] else 'ready'
            response['freshness']['complete'] = True
            return response
        finally:
            if watcher is not None:
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher

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

    def _graph_job(self, principal, scope, job_id):
        project = self.record_port._project(scope)
        with self.record_port.connection_factory() as connection:
            return Jobs(connection, _credential(scope),
                        self.record_port.installation_id, project).get(job_id)

    async def graph_job(self, principal, scope, _request_key):
        try:
            job_id = UUID(scope['path'].rsplit('/', 1)[1])
        except (KeyError, ValueError, TypeError):
            raise InvalidRequest('invalid graph job id') from None
        await self._current(principal)
        view = await asyncio.to_thread(self._graph_job, principal, scope, job_id)
        if view is None or view.kind != 'graph.build':
            raise JobError('not_found')
        project_key = next((key for key, value in self.record_port.projects.items()
                            if value == UUID(principal['project_id'])), None)
        repo = self.project_repos.get(project_key)
        if repo is None:
            raise AuthError('forbidden')
        status = {'pending': 'queued', 'running': 'running',
                  'succeeded': 'completed', 'failed': 'failed',
                  'canceled': 'failed', 'unresolved': 'failed'}.get(view.state)
        if status is None:
            raise D2Unavailable('unavailable')
        await self._current(principal)
        return {'job_id': str(view.job_id), 'status': status, 'repo': repo}

    @property
    def handlers(self):
        return {'C01-R006': self.status_degradation,
                'C01-R021': self.search, 'C01-R022': self.search,
                'C01-R023': self.graph_search, 'C01-R024': self.graph_stats,
                'C01-R025': self.graph_memory, 'C01-R034': self.graph_repository_stats,
                'C01-R037': self.graph_job}
