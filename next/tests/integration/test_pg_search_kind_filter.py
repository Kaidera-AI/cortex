"""Kind-filter port controls; inert controls and separately admitted real-PG tests."""
import asyncio
from contextlib import asynccontextmanager
import inspect
import os
from uuid import UUID

import pytest
from cortex_core.embeddings.pg_search import EmbeddingIdentity, Freshness, PostgresSearch, Scope
import test_pg_search as existing

IDENTITY = EmbeddingIdentity('fixture', 'kind-model', 'v1', 768, 'kind-preprocess')
VECTOR = [1.0] + [0.0] * 767
SCOPE = Scope(UUID(int=1), UUID(int=11))


class Connection:
    def __init__(self): self.calls = []
    async def execute(self, text, *args): self.calls.append(('execute',text,args))
    async def fetch(self, text, *args):
        self.calls.append(('fetch',text,args))
        return [{'record_id':'known','kind':'memory','source_revision':1,'distance':0.25}]


class InertSearch(PostgresSearch):
    def __init__(self): super().__init__(None, lambda *args: None); self.connection=Connection();self.effects=[]
    @asynccontextmanager
    async def _request(self,*args,**kwargs):
        self.effects.append('request');yield self.connection,SCOPE
    async def _state(self,*args):return 'ready'
    async def _freshness(self,*args):return Freshness(IDENTITY.key,1,1)


def call(search, kind, **kwargs):
    try:return asyncio.run(search.search('fixture-subject',IDENTITY,VECTOR,kind=kind,**kwargs)),None
    except Exception as error:return None,error


def test_offline_declared_optional_kind_is_a_supported_keyword():
    parameter=inspect.signature(PostgresSearch.search).parameters.get('kind')
    assert parameter is not None and parameter.kind==inspect.Parameter.KEYWORD_ONLY and parameter.default is None


@pytest.mark.parametrize('kind',[False,1,{},[], '', ' ', ' padded', 'padded ', 'a'*65, 'é'*33, 'memory\x00', 'memory\n'])
def test_offline_invalid_kind_refuses_before_any_database_effect(kind):
    search=InertSearch();result,error=call(search,kind)
    assert result is None and isinstance(error,ValueError), 'invalid kind must be a typed input refusal, not an absent API'
    assert search.effects==[] and search.connection.calls==[]


@pytest.mark.parametrize('kind',['memory','decision','é'*32,"memory' OR 1=1--"])
def test_offline_kind_is_bound_as_a_value_with_existing_scope_and_limit(kind):
    search=InertSearch();result,error=call(search,kind,limit=7)
    assert error is None and result is not None, 'kind keyword is not implemented'
    text,args=next((text,args) for name,text,args in search.connection.calls if name=='fetch')
    assert args[:3]==(SCOPE.tenant_id,SCOPE.project_id,IDENTITY.key)
    assert args[4:]==(7,kind), 'kind must be a bound value after unchanged limit'
    assert kind not in text and '$6' in text, 'untrusted kind must not become SQL syntax'
    assert result.freshness.pending_records==1


def test_offline_default_call_retains_prior_hits_and_whole_scope_freshness():
    search=InertSearch();result=asyncio.run(search.search('fixture-subject',IDENTITY,VECTOR,limit=7))
    assert [row.record_id for row in result.hits]==['known']
    assert result.freshness==Freshness(IDENTITY.key,1,1)
    text,args=next((text,args) for name,text,args in search.connection.calls if name=='fetch')
    assert args[:5]==(SCOPE.tenant_id,SCOPE.project_id,IDENTITY.key,'['+','.join(str(float(v)) for v in VECTOR)+']',7)


class TestNativeKindFilter(existing.SearchTests):
    """Existing forced-RLS PG fixture, synthetic authority; no C04 authority claim."""
    async def asyncSetUp(self):
        if os.environ.get('NEMO_KIND_FILTER_OWNED_FIXTURE')!='1':
            raise RuntimeError('explicit owned disposable fixture authority required; no ambient DSN')
        await super().asyncSetUp()

    async def seed_kind(self,subject,rid,kind,vector=existing.VECTOR,revision=1):
        await self.store.note_revision(subject,rid,kind,revision)
        await self.store.store_embedding(subject,rid,revision,existing.IDENTITY,vector)

    async def selected(self,kind,limit=20):
        try:return await self.store.search('alice',existing.IDENTITY,existing.VECTOR,limit=limit,kind=kind)
        except TypeError:return None # absent source API becomes a deciding BODY assertion below

    async def test_native_kind_predicate_precedes_top_k(self):
        await self.seed_kind('alice','closer-wrong','memory')
        await self.seed_kind('alice','wanted','decision',[0.8,0.6]+[0.0]*766)
        result=await self.selected('decision',1)
        assert result is not None and [h.record_id for h in result.hits]==['wanted']

    async def test_native_unknown_and_sql_like_kind_return_empty(self):
        await self.seed_kind('alice','known','memory')
        for kind in ['unregistered',"memory' OR 1=1--"]:
            result=await self.selected(kind)
            assert result is not None and result.hits==()

    async def test_native_kind_never_hides_pending_projection_freshness(self):
        await self.seed_kind('alice','wanted','decision')
        await self.store.note_revision('alice','pending-memory','memory',2)
        result=await self.selected('decision')
        assert result is not None and [h.record_id for h in result.hits]==['wanted']
        assert result.freshness.pending_records==1 and result.freshness.status=='lagging'

    async def test_native_kind_preserves_scope_identity_and_current_revision(self):
        await self.seed_kind('alice','wanted','decision')
        await self.seed_kind('bob','foreign-tenant','decision')
        await self.seed_kind('alice-other','foreign-project','decision')
        await self.seed_kind('alice','stale','decision')
        await self.store.note_revision('alice','stale','decision',2)
        result=await self.selected('decision')
        assert result is not None and [h.record_id for h in result.hits]==['wanted']
        assert result.freshness.pending_records==1

    async def test_native_default_and_explicit_none_preserve_all_kinds(self):
        await self.seed_kind('alice','memory-row','memory')
        await self.seed_kind('alice','decision-row','decision',[0.8,0.6]+[0.0]*766)
        before=await self.store.search('alice',existing.IDENTITY,existing.VECTOR)
        after=await self.selected(None)
        assert after is not None and before==after
        assert {h.record_id for h in after.hits}=={'memory-row','decision-row'}

    async def test_native_valid_kind_over_empty_source_is_successful_empty(self):
        result=await self.selected('decision')
        assert result is not None and result.hits==() and result.freshness.status=='current'
