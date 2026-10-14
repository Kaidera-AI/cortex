"""E0 frozen BEFORE E1/E2 source. Synthetic ports are NOT effect authority.

SurfaceAbsent is an assertion in the test body, not an import/setup failure.
Semantic assertions exercise an existing surface with valid synthetic inputs.
The shared tests/test_receipts.py owns phase/error classification.
"""
import asyncio
import copy
import importlib
import importlib.util
import inspect
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

from cortex_core.release.codec import sha256
from cortex_core.runtime import config, signed_member
from cortex_core.embeddings.pg_search import (
    CapabilityUnavailable, EmbeddingIdentity, Freshness, Hit, SearchResult,
)
from cortex_core.api.c11b2 import C11b2ReadPort
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble

NEXT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((NEXT / 'docs/runtime/rc0/fixtures/paired-complete.json').read_bytes())
IDENTITY = EmbeddingIdentity('openrouter', 'fixture-model', 'fixture-version', 768, 'fixture-only')


class SurfaceAbsent(AssertionError):
    """A named future interface is missing; no downstream behavior was run."""


class SemanticRed(AssertionError):
    """Existing callable returned behavior inconsistent with the E0 contract."""


def surface(module, name):
    if importlib.util.find_spec(module) is None:
        raise SurfaceAbsent(f'{module}:{name} absent')
    value = getattr(importlib.import_module(module), name, None)
    if value is None:
        raise SurfaceAbsent(f'{module}:{name} absent')
    return value


def field(value, name):
    if not hasattr(value, name):
        raise SurfaceAbsent(f'{type(value).__name__}.{name} absent')
    return getattr(value, name)


def require_parameter(call, name):
    if name not in inspect.signature(call).parameters:
        raise SurfaceAbsent(f'{call.__qualname__} parameter {name} absent')


def encode(value):
    return (json.dumps(value, indent=2, ensure_ascii=True) + '\n').encode()


def materials(mode=None):
    lock = copy.deepcopy(FIXTURE['lock'])
    cfg = copy.deepcopy(FIXTURE['mode_configs']['linux/arm64']['api'])
    cfg['runtime_lock_sha256'] = sha256(encode(lock))
    if mode is not None:
        cfg['ai_mode'] = mode
    if mode == 'off':
        del cfg['embedding']
        del cfg['resolver']['handles']['provider']
    return lock, cfg


def admit(lock, cfg, raw=None):
    double = NonAuthoritativeSignedMemberDouble(encode(lock))
    with double.installed(signed_member):
        member = signed_member.request_member('test-only-aggregate', 'runtime.lock.json')
    return config.admit_config(member, encode(cfg) if raw is None else raw,
        aggregate_id='test-only-aggregate', observed_target='linux/arm64', mode='api')


def admitted_ai(mode=None):
    try:
        plan = admit(*materials(mode))
    except signed_member.Refusal as exc:
        raise SemanticRed(f'existing admit_config refuses {mode}: {exc.reason}') from None
    return field(plan, 'ai')


class AdmissionOracle(unittest.TestCase):
    def test_default_hosted_rerank_off_and_config_byte_revision(self):
        lock, cfg = materials()
        plan = admit(lock, cfg)
        ai = field(plan, 'ai')
        self.assertEqual(ai.mode, 'hosted')
        self.assertFalse(ai.rerank_enabled)
        self.assertEqual(ai.revision, sha256(encode(cfg)))
        self.assertEqual(ai.origin, 'https://openrouter.ai:443')
        self.assertEqual(ai.identity.dimensions, 768)
        self.assertFalse(plan.effect_authority)

    def test_off_admits_without_provider_identity_or_handle(self):
        lock, cfg = materials('off')
        try:
            plan = admit(lock, cfg)
        except signed_member.Refusal as exc:
            raise SemanticRed(f'OFF existing admission refused: {exc.reason}') from None
        ai = field(plan, 'ai')
        self.assertEqual(ai.mode, 'off')
        self.assertIsNone(ai.identity)
        self.assertNotIn('provider', plan.secret_handles)
        self.assertFalse(plan.effect_authority)

    def test_off_does_not_hide_other_original_owner_gaps(self):
        lock, cfg = materials('off')
        lock['owner_ports']['secret_resolver'] = {
            'state':'GAP', 'gap_id':'resolver', 'owner_role':'core-contract-owner',
            'reason':'original_owner_binding_not_delivered', 'blocks':['api']}
        cfg['runtime_lock_sha256'] = sha256(encode(lock))
        with self.assertRaises(signed_member.Refusal) as caught:
            admit(lock, cfg)
        if caught.exception.reason != 'required_binding_gap':
            raise SemanticRed(f'OFF shape prevents existing owner-gap check: {caught.exception.reason}')

    def test_unknown_selection_and_extra_origin_refuse(self):
        for name, value in [('ai_mode','local'), ('provider_origin','https://example.invalid')]:
            with self.subTest(name=name):
                lock, cfg = materials()
                cfg[name] = value
                with self.assertRaises(signed_member.Refusal):
                    admit(lock, cfg)

    def test_duplicate_config_keys_remain_strict(self):
        lock, cfg = materials()
        raw = encode(cfg).replace(b'{', b'{"mode":"api",', 1)
        with self.assertRaises(signed_member.Refusal):
            admit(lock, cfg, raw)

    def test_production_authority_remains_unbound(self):
        with self.assertRaises(signed_member.Refusal) as caught:
            signed_member.request_member('unbound-real-aggregate', 'runtime.lock.json')
        self.assertEqual(caught.exception.reason, 'required_binding_gap')


def read_port():
    """Existing method, synthetic C04/cache/search ports; no constructor bypass grant."""
    obj = object.__new__(C11b2ReadPort)
    obj.identity = IDENTITY
    obj.provider = NS(embed=None)
    obj.calls = []
    class Cache:
        async def get(self, *args, **kwargs):
            obj.calls.append(('cache', args, kwargs))
            return NS(vector=tuple([1.0] + [0.0] * 767))
    class Search:
        async def search(self, *args, **kwargs):
            obj.calls.append(('vector', args, kwargs))
            return SearchResult((Hit('r1', 'memory', 1, .1),), Freshness(IDENTITY.key, 1, 0))
    class Graph:
        async def search(self, *args, **kwargs):
            obj.calls.append(('graph', args, kwargs))
            return {'facts': ['existing-local-fact'], 'complete': True}
    async def freshness(*args, **kwargs):
        obj.calls.append(('freshness',))
    async def current(*args):
        obj.calls.append(('current_auth',))
    obj._ports = lambda principal: (Search(), Graph())
    obj._cache = lambda search: Cache()
    obj._post_read_freshness = freshness
    obj._current = current
    return obj


class ExistingReadOracle(unittest.IsolatedAsyncioTestCase):
    async def test_post_rerank_is_off_when_omitted(self):
        obj = read_port()
        value = await obj.search({'principal_id':'synthetic'},
            {'method':'POST', '_c11b_body':{'query':'known text'}}, None)
        if 'rerank' in value['degraded']:
            raise SemanticRed('POST omitted rerank still requests reranking')
        self.assertFalse(value['reranked'])

    async def test_get_rerank_is_off_when_omitted(self):
        obj = read_port()
        value = await obj.search({'principal_id':'synthetic'},
            {'method':'GET', 'query_string':b'q=known+text'}, None)
        if 'rerank' in value['degraded']:
            raise SemanticRed('GET omitted rerank still requests reranking')
        self.assertFalse(value['reranked'])

    async def test_runtime_passes_one_absolute_deadline_to_cache(self):
        obj = read_port()
        deadline = asyncio.get_running_loop().time() + .25
        await obj.search({'principal_id':'synthetic'}, {'method':'POST',
            '_c11b_body':{'query':'known text', 'rerank':False},
            '_cortex_deadline':deadline}, None)
        forwarded = next(row[2].get('deadline') for row in obj.calls if row[0] == 'cache')
        if forwarded is None or forwarded > deadline:
            raise SemanticRed('existing search starts cache without remaining absolute request deadline')

    async def test_separate_graph_reads_existing_facts_without_cache(self):
        obj = read_port()
        value = await obj.graph_search({'principal_id':'synthetic'},
            {'method':'GET', 'query_string':b'q=known+fact'}, None)
        self.assertEqual(value['facts'], ['existing-local-fact'])
        self.assertEqual([r[0] for r in obj.calls], ['graph','freshness','current_auth'])

    async def test_cache_surface_accepts_absolute_not_reset_deadline(self):
        from cortex_core.embeddings.query_cache import QueryEmbeddingCache
        require_parameter(QueryEmbeddingCache.get, 'deadline')
        seen = []
        obj = object.__new__(QueryEmbeddingCache)
        obj.wait_seconds = 12
        async def get(subject, identity, query, deadline):
            seen.append(deadline)
            return 'synthetic'
        obj._get = get
        deadline = asyncio.get_running_loop().time() + .1
        self.assertEqual(await obj.get('subject', IDENTITY, 'query', deadline=deadline), 'synthetic')
        self.assertLessEqual(seen[0], deadline)


class SelectionOracle(unittest.IsolatedAsyncioTestCase):
    def gate(self, mode=None):
        cls = surface('cortex_core.runtime.selection', 'SelectionGate')
        return cls(admitted_ai(mode))

    async def test_off_never_enters_provider_cache_vector_or_queue_callback(self):
        gate = self.gate('off')
        effects = []
        async def forbidden(deadline):
            effects.extend(['resolver','http','cache','vector','queue'])
            return 'forbidden'
        with self.assertRaises(CapabilityUnavailable) as caught:
            await gate.run(forbidden, revision=gate.snapshot.revision,
                           deadline=asyncio.get_running_loop().time()+1)
        self.assertEqual(caught.exception.reason, 'hosted_disabled')
        self.assertEqual(effects, [])

    async def test_off_revision_cancels_inflight_and_drops_cancellation_resistant_result(self):
        gate = self.gate()
        entered, release = asyncio.Event(), asyncio.Event()
        cancelled, published = [], []
        async def work(deadline):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                await release.wait()
            return 'old-vector'
        async def request():
            value = await gate.run(work, revision=gate.snapshot.revision,
                                  deadline=asyncio.get_running_loop().time()+1)
            published.append(value)
        task = asyncio.create_task(request())
        await asyncio.wait_for(entered.wait(), .2)
        try:
            await asyncio.wait_for(gate.apply(admitted_ai('off')), .2)
            release.set()
            try:
                await asyncio.wait_for(task, .2)
            except (CapabilityUnavailable, asyncio.CancelledError):
                pass
            self.assertEqual(cancelled, [True])
            self.assertEqual(published, [])
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_old_revision_never_replays_after_reenable(self):
        gate = self.gate()
        old = gate.snapshot.revision
        await gate.apply(admitted_ai('off'))
        lock, cfg = materials('hosted')
        cfg['operation_id'] = '00000000-0000-0000-0000-000000000099'
        await gate.apply(field(admit(lock, cfg), 'ai'))
        calls = []
        async def work(deadline):
            calls.append(True)
        with self.assertRaises(CapabilityUnavailable):
            await gate.run(work, revision=old, deadline=asyncio.get_running_loop().time()+1)
        self.assertEqual(calls, [])

    async def test_expired_request_cannot_resolve_or_dispatch(self):
        gate = self.gate()
        calls = []
        async def work(deadline):
            calls.append(True)
        with self.assertRaises(CapabilityUnavailable):
            await gate.run(work, revision=gate.snapshot.revision,
                           deadline=asyncio.get_running_loop().time()-1)
        self.assertEqual(calls, [])

    async def test_gate_clamps_caller_budget_to_rc0_ten_seconds(self):
        gate = self.gate()
        start = asyncio.get_running_loop().time()
        seen = []
        async def work(deadline):
            seen.append(deadline)
            return 'ok'
        self.assertEqual(await gate.run(work, revision=gate.snapshot.revision,
            deadline=start+100), 'ok')
        self.assertLessEqual(seen[0], start+10.01)


class ProviderOracle(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_provider_refuses_before_key(self):
        import httpx
        from cortex_core.modules.providers.hosted import HostedProvider
        calls = []
        async def resolver(*args):
            calls.append('resolver')
            return 'SYNTHETIC-NOT-A-KEY'
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None)) as client:
            provider = HostedProvider(client, resolver)
            other = EmbeddingIdentity('unknown', 'model', 'v1', 768, 'text-v1')
            with self.assertRaises(CapabilityUnavailable) as caught:
                await provider.embed('synthetic permitted text', other)
            self.assertEqual(caught.exception.reason, 'provider_not_supported')
        self.assertEqual(calls, [])

    async def test_redirect_cannot_leave_the_one_origin_even_with_permissive_client(self):
        import httpx
        from cortex_core.modules.providers.hosted import HostedProvider, ENDPOINT
        requests = []
        async def resolver(*args):
            return 'SYNTHETIC-NOT-A-KEY'
        def mock(request):
            requests.append((str(request.url), request.content.decode()))
            if len(requests) == 1:
                return httpx.Response(307, headers={'location':'https://other.invalid/steal'})
            return httpx.Response(200, json={'data':[{'embedding':[1.0]+[0.0]*767}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock), follow_redirects=True) as client:
            provider = HostedProvider(client, resolver)
            try:
                await provider.embed('SYNTHETIC-SCOPE-TEXT', IDENTITY)
            except CapabilityUnavailable:
                pass
        if [r[0] for r in requests] != [ENDPOINT]:
            raise SemanticRed('hosted adapter followed redirect outside its sole admitted origin (mock only)')

    async def test_unadmitted_model_refuses_before_key_or_http(self):
        from cortex_core.modules.providers.hosted import HostedProvider
        require_parameter(HostedProvider, 'selection')
        calls = []
        async def resolver(*args):
            calls.append('resolver')
            return 'SYNTHETIC-NOT-A-KEY'
        provider = HostedProvider(NS(), resolver, selection=lambda: admitted_ai())
        other = EmbeddingIdentity('openrouter', 'different-model', IDENTITY.version, 768, IDENTITY.preprocessing)
        with self.assertRaises(CapabilityUnavailable) as caught:
            await provider.embed('synthetic permitted text', other)
        self.assertEqual(caught.exception.reason, 'selection_mismatch')
        self.assertEqual(calls, [])


class CanonicalTextOracle(unittest.TestCase):
    def codec(self):
        return surface('cortex_core.embeddings.text', 'project_text')

    def unsupported(self, kind, payload, reason):
        codec = self.codec()
        error = surface('cortex_core.embeddings.text', 'UnsupportedText')
        with self.assertRaises(error) as caught:
            codec(kind, payload)
        self.assertEqual(caught.exception.code, 'unsupported_text')
        self.assertEqual(caught.exception.reason, reason)

    def test_memory_section_content_only(self):
        self.assertEqual(self.codec()('memory', encode({'section':'Decisions',
            'content':'Use café\nKeep Unicode.', 'category':'hidden', 'source':'SECRET-PATH'})),
            'Decisions\nUse café\nKeep Unicode.')

    def test_session_message_order_roles_and_content_only(self):
        self.assertEqual(self.codec()('session', encode({'agent':'SECRET-AGENT',
            'task':'SECRET-TASK', 'source_path':'SECRET-PATH', 'metadata':{'secret':'NO'},
            'messages':[{'role':'human','content':'First'},
                        {'role':'agent','content':'Second','metadata':{'secret':'NO'}}]})),
            'human: First\nagent: Second')

    def test_unknown_kind_is_typed_not_skipped(self):
        self.unsupported('future-kind', encode({'content':'text'}), 'unknown_kind')

    def test_binary_kind_is_typed_not_empty(self):
        self.unsupported('binary', b'\x00\xff\x89PNG', 'unknown_kind')

    def test_binary_payload_is_typed(self):
        self.unsupported('memory', b'\x00\xff\x89PNG', 'invalid_payload')

    def test_wrong_content_type_is_typed(self):
        self.unsupported('memory', encode({'section':'S','content':23}), 'invalid_payload')

    def test_duplicate_keys_are_not_canonical_text(self):
        self.unsupported('memory', b'{"section":"S","content":"a","content":"b"}', 'invalid_payload')

    def test_no_silent_provider_length_truncation(self):
        self.unsupported('memory', encode({'section':'S','content':'x'*(16*1024)}), 'text_too_large')


class KeywordOracle(unittest.IsolatedAsyncioTestCase):
    def keyword(self, records, *, max_records=100, authorize=None):
        cls = surface('cortex_core.embeddings.keyword', 'KeywordSearch')
        self.calls = []
        async def auth(subject):
            self.calls.append('authorize')
            if authorize:
                await authorize(subject)
        async def read(subject, *, limit, deadline):
            self.calls.append(('read',limit,deadline))
            return records[:limit]
        return cls(read_current=read, authorize=auth, max_records=max_records)

    def row(self, rid, content, kind='memory'):
        return NS(record_id=rid, kind=kind, source_revision=1,
                  payload=encode({'section':'S','content':content}))

    async def test_keyword_off_usable_without_identity_provider_or_pool(self):
        search = self.keyword([self.row('b','other'), self.row('a','Café decision')])
        result = await search.search('subject', 'CAFÉ', limit=5,
                                    deadline=asyncio.get_running_loop().time()+1)
        self.assertEqual([r.record_id for r in result.hits], ['a'])
        self.assertTrue(result.complete)
        self.assertEqual(self.calls[0], 'authorize')
        self.assertEqual(self.calls[-1], 'authorize')

    async def test_keyword_limit_and_ties_are_deterministic(self):
        search = self.keyword([self.row('b','same'), self.row('a','same')])
        result = await search.search('subject', 'same', limit=1,
                                    deadline=asyncio.get_running_loop().time()+1)
        self.assertEqual([r.record_id for r in result.hits], ['a'])
        self.assertTrue(result.complete)  # result top-k is not scan truncation

    async def test_scan_bound_is_typed_partial_not_ready_empty(self):
        search = self.keyword([self.row('a','miss'),self.row('b','match')], max_records=1)
        result = await search.search('subject', 'match', limit=5,
                                    deadline=asyncio.get_running_loop().time()+1)
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, 'scan_limit')

    async def test_unknown_record_reports_typed_partial(self):
        search = self.keyword([self.row('a','known'), self.row('b','data','future-kind')])
        result = await search.search('subject', 'known', limit=5,
                                    deadline=asyncio.get_running_loop().time()+1)
        self.assertFalse(result.complete)
        self.assertEqual([(x.record_id,x.reason) for x in result.unsupported], [('b','unknown_kind')])

    async def test_revoked_final_auth_suppresses_prepared_hits(self):
        n = 0
        async def auth(subject):
            nonlocal n
            n += 1
            if n == 2:
                raise PermissionError('synthetic revocation')
        search = self.keyword([self.row('a','match')], authorize=auth)
        with self.assertRaises(PermissionError):
            await search.search('subject', 'match', limit=5,
                                deadline=asyncio.get_running_loop().time()+1)

    async def test_expired_keyword_request_does_not_read(self):
        search = self.keyword([self.row('a','match')])
        with self.assertRaises(CapabilityUnavailable):
            await search.search('subject', 'match', limit=5,
                                deadline=asyncio.get_running_loop().time()-1)
        self.assertFalse(any(isinstance(x,tuple) for x in self.calls))


class OffReadIntegrationOracle(unittest.IsolatedAsyncioTestCase):
    async def test_off_search_routes_keyword_while_graph_keeps_its_existing_port(self):
        require_parameter(C11b2ReadPort, 'selection')
        require_parameter(C11b2ReadPort, 'keyword')
        obj = read_port()
        obj.identity, obj.provider = None, None
        obj.selection = surface('cortex_core.runtime.selection', 'SelectionGate')(admitted_ai('off'))
        async def keyword(subject, query, *, limit, deadline):
            obj.calls.append(('keyword',))
            return NS(hits=(NS(record_id='k1',kind='memory',source_revision=1,score=1.0),),
                      complete=True, reason=None, unsupported=())
        obj.keyword = NS(search=keyword)
        response = await obj.search({'principal_id':'synthetic'}, {'method':'POST',
            '_c11b_body':{'query':'known text'},
            '_cortex_deadline':asyncio.get_running_loop().time()+1}, None)
        self.assertEqual([x['id'] for x in response['results']], ['k1'])
        self.assertFalse(response['reranked'])
        self.assertIn('hosted_disabled', response['degraded'])
        self.assertFalse(any(r[0] in ('cache','vector') for r in obj.calls))
        graph = await obj.graph_search({'principal_id':'synthetic'},
            {'method':'GET','query_string':b'q=known'}, None)
        self.assertEqual(graph['facts'], ['existing-local-fact'])


if __name__ == '__main__':
    unittest.main()
