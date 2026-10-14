"""Effect-free import/behavior controls; only asyncpg is absent, no dummy driver."""
import asyncio
from contextlib import contextmanager
import importlib
import importlib.abc
import json
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch


@contextmanager
def missing_driver():
    names=lambda n:n=='asyncpg' or n.startswith('asyncpg.') or n=='cortex_core' or n.startswith('cortex_core.')
    saved={n:m for n,m in sys.modules.items() if names(n)}
    for name in saved:del sys.modules[name]
    attempts=[]
    class MissingAsyncpg(importlib.abc.MetaPathFinder):
        def find_spec(self,fullname,path=None,target=None):
            if fullname=='asyncpg' or fullname.startswith('asyncpg.'):
                attempts.append(fullname)
                raise ModuleNotFoundError("No module named 'asyncpg'",name='asyncpg')
    finder=MissingAsyncpg();sys.meta_path.insert(0,finder)
    try:yield attempts
    finally:
        sys.meta_path.remove(finder)
        for name in list(sys.modules):
            if names(name):del sys.modules[name]
        sys.modules.update(saved)


class DriverFree(unittest.IsolatedAsyncioTestCase):
    def load(self,name):
        try:return importlib.import_module(name)
        except Exception as error:self.fail(f'effect-free import {name} failed: {type(error).__name__}: {error}')

    def test_config_import_does_not_require_the_driver(self):
        with missing_driver() as attempts:
            self.load('cortex_core.runtime.config')
            self.assertEqual(attempts,[])

    def test_selection_import_and_off_refusal_do_not_require_the_driver(self):
        with missing_driver() as attempts:
            selection=self.load('cortex_core.runtime.selection')
            gate=selection.SelectionGate(selection.AiSelection('off','fixture',None))
            error=None
            try:gate.check(revision=gate.snapshot.revision,deadline=asyncio.get_running_loop().time()+1,mode='hosted')
            except Exception as caught:error=caught
            self.assertIsNotNone(error);self.assertEqual(error.code,'capability_unavailable')
            self.assertEqual(error.reason,'hosted_disabled');self.assertEqual(attempts,[])

    async def test_keyword_search_without_pg_keeps_literal_matching_and_authorization(self):
        with missing_driver() as attempts:
            keyword=self.load('cortex_core.embeddings.keyword');calls=[]
            row=NS(record_id='fixture',kind='memory',source_revision=1,payload=json.dumps(dict(section='S',content='Straße [a.*]',metadata='HIDDEN')).encode())
            async def auth(subject):calls.append('auth');return True
            async def read(subject,**kwargs):calls.append('read');return [row]
            search=keyword.KeywordSearch(read_current=read,authorize=auth)
            for query,wanted in [('STRASSE',1),('[a.*]',1),('aZZ',0),('HIDDEN',0)]:
                result=await search.search('subject',query,deadline=asyncio.get_running_loop().time()+1)
                self.assertEqual(len(result.hits),wanted);self.assertTrue(result.complete)
            self.assertEqual(calls,['auth','read','read','auth']*4);self.assertEqual(attempts,[])

    def test_shared_pure_types_deadlines_and_legacy_aliases_are_identical(self):
        with missing_driver() as attempts:
            pure=self.load('cortex_core.embeddings.projection_revision')
            pg=self.load('cortex_core.embeddings.pg_search')
            keyword=self.load('cortex_core.embeddings.keyword')
            selection=self.load('cortex_core.runtime.selection')
            gateway=self.load('cortex_core.api.c11a')
            for name in ('Scope','EmbeddingIdentity','Freshness','Hit','SearchResult','CapabilityUnavailable','CoreUnavailable','StaleEmbedding'):
                self.assertTrue(hasattr(pure,name),name+' has no shared pure home')
                self.assertIs(getattr(pg,name),getattr(pure,name))
            self.assertIs(keyword.CapabilityUnavailable,pure.CapabilityUnavailable)
            self.assertIs(selection.EmbeddingIdentity,pure.EmbeddingIdentity)
            self.assertIs(gateway.CapabilityUnavailable,pure.CapabilityUnavailable)
            for name in ('request_deadline','check_deadline'):
                self.assertIs(getattr(keyword,name),getattr(pure,name))
                self.assertIs(getattr(selection,name),getattr(pure,name))
                self.assertIs(getattr(gateway,name),getattr(pure,name))
            identity=pure.EmbeddingIdentity('openrouter','fixture','1',768,'v1')
            self.assertEqual(identity.key,'96b64d280271856159d52f666614e547c7bea628143d46b3a00f4151f3698e6a')
            self.assertEqual(attempts,[])

    def test_all_adapter_modules_import_without_opening_driver_boundary(self):
        with missing_driver() as attempts:
            for name in ('cortex_core.embeddings.pg_search','cortex_core.embeddings.query_cache','cortex_core.modules.graph.pg_graph','cortex_core.conductor.supervisor','cortex_core.runtime.factory'):
                self.load(name)
            self.assertEqual(attempts,[])

    async def test_missing_driver_refuses_pg_boundary_with_typed_error_before_pool_acquire(self):
        for kind in ('search','supervisor'):
            with self.subTest(kind=kind),missing_driver() as attempts:
                pg=self.load('cortex_core.embeddings.pg_search');calls=[]
                class Pool:
                    def acquire(self,**kwargs):calls.append('acquire');raise AssertionError('driver missing must refuse before pool acquisition')
                if kind=='search':boundary=pg.PostgresSearch(Pool(),lambda *a:None)._request('subject')
                else:
                    supervisor=self.load('cortex_core.conductor.supervisor')
                    boundary=supervisor.Supervisor(Pool(),__import__('uuid').UUID(int=1))._transaction()
                error=None
                try:
                    async with boundary:self.fail('missing PG driver was accepted')
                except Exception as caught:error=caught
                self.assertIsInstance(error,pg.CoreUnavailable)
                self.assertEqual(error.code,'core_unavailable');self.assertIn('asyncpg',str(error))
                self.assertEqual(calls,[]);self.assertEqual(attempts,['asyncpg'])

    def test_broken_transitive_dependency_is_not_masked_as_missing_driver(self):
        pg=self.load('cortex_core.embeddings.pg_search')
        self.assertTrue(hasattr(pg,'_require_asyncpg'),'lazy driver boundary absent')
        original=__import__('builtins').__import__
        def broken(name,*args,**kwargs):
            if name=='asyncpg':raise ModuleNotFoundError('broken dependency',name='other_dependency')
            return original(name,*args,**kwargs)
        error=None
        with patch('builtins.__import__',broken):
            try:pg._require_asyncpg()
            except Exception as caught:error=caught
        self.assertIsInstance(error,ModuleNotFoundError)
        self.assertEqual(error.name,'other_dependency')

    async def test_installed_driver_retry_and_non_driver_errors_keep_their_types(self):
        pg=self.load('cortex_core.embeddings.pg_search');driver=importlib.import_module('asyncpg');calls=[]
        @pg.retry_authorized_transaction
        async def operation():
            calls.append('operation')
            if len(calls)<3:raise driver.SerializationError('fixture')
            return 'done'
        self.assertEqual(await operation(),'done');self.assertEqual(len(calls),3)
        calls.clear()
        @pg.retry_authorized_transaction
        async def invalid():calls.append('invalid');raise ValueError('fixture')
        with self.assertRaises(ValueError):await invalid()
        self.assertEqual(calls,['invalid'])
