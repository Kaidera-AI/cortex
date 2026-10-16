"""Frozen B01 adapter controls; unit ports are not real-PG qualification."""
import asyncio
import ast
import copy
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from uuid import UUID

import httpx
import numpy as np
from cortex_core.embeddings.pg_search import EmbeddingIdentity, Freshness, Hit, SearchResult
from cortex_core.embeddings.query_cache import CachedEmbedding
from cortex_core.modules.providers.hosted import HostedProvider
from vector_baseline import corpus, load, oracle

NEXT = Path(__file__).resolve().parents[2]
IDENTITY = EmbeddingIdentity("openrouter", "fixture-model", "v1", 768, "preprocess-v1")
TA, PA = UUID(int=2), UUID(int=3)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class MissingSurface:
    """Explicit inert preimplementation seam: behavioral assertions fail."""
    async def evaluate(self, *args, **kwargs):
        return {"status": "NOT_RUN", "reason": "implementation_absent"}

    def client(self, *args, **kwargs):
        class Inert:
            async def open(self): pass
            async def request(self, query): return {"status": "NOT_RUN"}
            async def aclose(self): pass
        return Inert()

    def receipt(self, *args, **kwargs):
        return {"status": "NOT_RUN", "gtm_qualified": False,
                "whole_product_stack_complete": False, "engine_decision": "UNDECIDED",
                "load": {"records": [], "warmup_records": []}}


def surface():
    try:
        return importlib.import_module("vector_baseline.product_path")
    except ModuleNotFoundError as error:
        if error.name != "vector_baseline.product_path": raise
        return MissingSurface()


class UnitCache:
    """A unit port, with the REAL hosted adapter behind its cache miss."""
    def __init__(self, provider):
        self.provider, self.calls, self.values = provider, 0, {}

    async def get(self, subject, identity, text):
        self.calls += 1
        key = (subject, identity.key, text)
        if key in self.values: return CachedEmbedding(self.values[key], True)
        vector = tuple(await self.provider.embed(text, identity))
        self.values[key] = vector
        return CachedEmbedding(vector, False)


class UnitSearch:
    def __init__(self):
        self.calls, self.bad_hit, self.wrong_identity, self.failure = [], None, False, None

    async def search(self, subject, identity, vector, *, limit=20):
        self.calls.append((subject, identity.key, tuple(vector), limit))
        if self.failure: raise self.failure
        hits = (Hit("p-r1", "memory", 1, 0.), Hit("p-r2", "memory", 1, 0.),
                Hit("p-r3", "memory", 1, 1.))[:limit]
        if subject == "empty": hits = ()
        if self.bad_hit: hits = (self.bad_hit,) + hits[1:]
        return SearchResult(hits, Freshness("wrong" if self.wrong_identity else identity.key, len(hits), 0))


class ProductPathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="b01-query-input-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "corpus"
        vectors = np.zeros((3, 768), dtype="<f4")
        vectors[0, 0] = vectors[1, 0] = vectors[2, 1] = 1
        self.corpus = corpus.write_corpus(self.root, vectors,
            [{"id": "r1", "tenant": "ta", "project": "pa"},
             {"id": "r2", "tenant": "ta", "project": "pa"},
             {"id": "r3", "tenant": "ta", "project": "pa"}],
            {"provider": IDENTITY.provider, "model": IDENTITY.model, "dimension": 768,
             "metric": "cosine", "generation": "fixture-g1"})
        query = {"id": "q1", "text": "basis-a", "subject": "reader", "cell": "scope/dense",
                 "mode": "dense", "limit": 2, "tenant": "ta", "project": "pa",
                 "vector": vectors[0].tolist(), "sparse": None, "lo": None, "hi": None,
                 "kind": None, "time_lo": None, "time_hi": None,
                 "status": "READY", "generation": "fixture-g1"}
        empty = {**query, "id": "empty", "subject": "empty", "text": "empty-basis", "project": "empty"}
        self.spec = {"schema": "cortex.b01-product-input.v1", "identity": {
            "provider": IDENTITY.provider, "model": IDENTITY.model, "version": IDENTITY.version,
            "dimensions": IDENTITY.dimensions, "preprocessing": IDENTITY.preprocessing},
            "corpus_manifest_sha256": corpus.digest(self.root / "manifest.json"),
            "queries": [query, empty], "splits": {"tuning": [], "held_out": ["q1", "empty"]},
            "scope_map": [{"tenant": "ta", "project": "pa", "tenant_id": str(TA), "project_id": str(PA)},
                          {"tenant": "ta", "project": "empty", "tenant_id": str(TA), "project_id": str(UUID(int=4))}],
            "kind_map": {"0": "memory"},
            "record_map": {"r1": "p-r1", "r2": "p-r2", "r3": "p-r3"},
            "source_revisions": {"r1": 1, "r2": 1, "r3": 1},
            "source_sha256": {"src/cortex_core/embeddings/pg_search.py": corpus.digest(NEXT / "src/cortex_core/embeddings/pg_search.py")}}
        self.expected = hashlib.sha256(canonical(self.spec)).hexdigest()
        self.provider_calls = self.credential_calls = self.auth_calls = 0
        self.provider_vector = vectors[0].tolist()

        def respond(request):
            self.provider_calls += 1
            return httpx.Response(200, json={"data": [{"embedding": self.provider_vector}]})

        async def resolve(provider):
            self.credential_calls += 1
            return "public-synthetic-fixture-token"

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)
        self.addAsyncCleanup(self.http.aclose)
        self.provider = HostedProvider(self.http, resolve)
        self.cache, self.search = UnitCache(self.provider), UnitSearch()

    async def authorize(self, subject):
        self.auth_calls += 1
        return SimpleNamespace(tenant_id=TA, project_id=PA if subject != "empty" else UUID(int=4),
                               permission_generation=1)

    async def evaluate(self, qid="q1", *, spec=None, expected=None):
        return await surface().evaluate(self.root, spec if spec is not None else self.spec,
            expected if expected is not None else self.expected, qid, self.cache, self.search,
            self.authorize, source_root=NEXT)

    async def test_actual_calls_cold_warm_and_independent_tiny_truth(self):
        truth = oracle.rank(self.corpus, self.spec["queries"][0], modes=("dense",))
        self.assertEqual(truth.ids("dense", limit=3), ["r1", "r2", "r3"])
        self.assertEqual([truth.score("dense", rid) for rid in ["r1", "r2", "r3"]], [1., 1., 0.])
        cold, warm = await self.evaluate(), await self.evaluate()
        self.assertEqual(cold["status"], "MEASURED")
        self.assertEqual(warm["status"], "MEASURED")
        self.assertFalse(cold["cache_hit"])
        self.assertTrue(warm["cache_hit"])
        self.assertEqual((self.provider_calls, self.credential_calls, self.cache.calls, len(self.search.calls)), (1, 1, 2, 2))
        self.assertEqual(cold["ids"], ["r1", "r2"])
        self.assertEqual(cold["measurement"]["strict_recall"], 1.)
        self.assertEqual(cold["measurement"]["tie_recall"], 1.)
        self.assertGreaterEqual(self.auth_calls, 2)

    async def test_changed_input_mapping_refused_before_any_port(self):
        changed = copy.deepcopy(self.spec)
        changed["scope_map"][0]["project_id"] = str(UUID(int=9))
        row = await self.evaluate(spec=changed)
        self.assertEqual(row["reason"], "input_binding_changed")
        self.assertEqual((self.auth_calls, self.cache.calls, self.provider_calls, len(self.search.calls)), (0, 0, 0, 0))

    async def test_changed_corpus_and_dependency_bytes_refused(self):
        with (self.root / "vectors.npy").open("ab") as stream: stream.write(b"changed")
        row = await self.evaluate()
        self.assertEqual(row["status"], "INPUT_REFUSED")
        self.assertEqual(self.cache.calls, 0)

    async def test_wrong_identity_refused_before_ports(self):
        changed = copy.deepcopy(self.spec)
        changed["identity"]["model"] = "different"
        row = await self.evaluate(spec=changed, expected=hashlib.sha256(canonical(changed)).hexdigest())
        self.assertEqual(row["reason"], "identity_mismatch")
        self.assertEqual((self.auth_calls, self.cache.calls), (0, 0))

    async def test_split_overlap_refused_before_ports(self):
        changed = copy.deepcopy(self.spec); changed["splits"]["tuning"] = ["q1"]
        row = await self.evaluate(spec=changed, expected=hashlib.sha256(canonical(changed)).hexdigest())
        self.assertEqual(row["reason"], "split_overlap")
        self.assertEqual((self.auth_calls, self.cache.calls), (0, 0))

    async def test_current_scope_is_compared_with_frozen_mapping(self):
        async def other(subject):
            return SimpleNamespace(tenant_id=TA, project_id=UUID(int=99), permission_generation=1)
        row = await surface().evaluate(self.root, self.spec, self.expected, "q1", self.cache,
            self.search, other, source_root=NEXT)
        self.assertEqual(row["reason"], "current_scope_mismatch")
        self.assertEqual(self.cache.calls, 0)

    async def test_unsupported_filter_sparse_hybrid_and_metric_have_no_credit(self):
        for field, value in [("kind", 0), ("mode", "sparse"), ("mode", "hybrid")]:
            changed = copy.deepcopy(self.spec); changed["queries"][0][field] = value
            row = await self.evaluate(spec=changed, expected=hashlib.sha256(canonical(changed)).hexdigest())
            self.assertEqual(row["status"], "NOT_RUN")
            self.assertNotIn("measurement", row)
        self.assertEqual((self.auth_calls, self.cache.calls, self.provider_calls), (0, 0, 0))

    async def test_unbound_provider_vector_has_no_recall_credit(self):
        self.provider_vector = [0., 1.] + [0.] * 766
        row = await self.evaluate()
        self.assertEqual(row["reason"], "provider_vector_unbound")
        self.assertNotIn("measurement", row)
        self.assertEqual(len(self.search.calls), 0)

    async def test_unknown_foreign_duplicate_kind_and_revision_hits_refuse(self):
        for hit in [Hit("unknown", "memory", 1, 0.), Hit("p-r1", "different", 1, 0.),
                    Hit("p-r1", "memory", 2, 0.), Hit("p-r2", "memory", 1, 0.)]:
            self.search.bad_hit = hit
            row = await self.evaluate()
            self.assertEqual(row["status"], "RESULT_REFUSED")
            self.assertNotIn("measurement", row)

    async def test_wrong_freshness_identity_has_no_credit(self):
        self.search.wrong_identity = True
        row = await self.evaluate()
        self.assertEqual(row["reason"], "result_identity_mismatch")
        self.assertNotIn("measurement", row)

    async def test_empty_and_less_than_k_are_not_fictional_recall(self):
        empty = await self.evaluate("empty")
        self.assertEqual(empty["status"], "MEASURED")
        self.assertTrue(empty["measurement"]["empty_exact"])
        self.assertIsNone(empty["measurement"]["tie_recall"])
        changed = copy.deepcopy(self.spec); changed["queries"][0]["limit"] = 10
        row = await self.evaluate(spec=changed, expected=hashlib.sha256(canonical(changed)).hexdigest())
        self.assertEqual(row["measurement"]["expected_count"], 3)
        self.assertEqual(row["measurement"]["tie_recall"], 1.)

    async def test_unavailable_results_are_charged_as_load_errors(self):
        from cortex_core.embeddings.pg_search import CoreUnavailable
        self.search.failure = CoreUnavailable("private-dependency-message")
        module = surface()
        run = await load.run([self.spec["queries"][0]], lambda n: module.client(
            self.root, self.spec, self.expected, self.cache, self.search, self.authorize,
            source_root=NEXT), load.RunConfig(duration_seconds=.2, warmup_seconds=0, deadline_seconds=.1))
        self.assertEqual(len(run["records"]), 8)
        self.assertTrue(all(row["status"] == "ERROR" for row in run["records"]))
        self.assertTrue(all(row["latency_seconds"] >= .1 for row in run["records"]))
        self.assertNotIn("private-dependency-message", str(run))

    async def test_fixed_offers_and_receipt_preserve_errors_and_no_gtm_credit(self):
        module = surface()
        run = await load.run([self.spec["queries"][0]], lambda n: module.client(
            self.root, self.spec, self.expected, self.cache, self.search, self.authorize,
            source_root=NEXT), load.RunConfig(duration_seconds=.2, warmup_seconds=.2))
        receipt = module.receipt(run, self.expected, verification_kind="UNIT_PORTS",
                                 cleanup={"complete": True})
        self.assertEqual(len(receipt["load"]["records"]), 8)
        self.assertEqual(len(receipt["load"]["warmup_records"]), 8)
        self.assertEqual(run["clients_closed"], 8)
        self.assertFalse(receipt["gtm_qualified"])
        self.assertFalse(receipt["whole_product_stack_complete"])
        self.assertEqual(receipt["engine_decision"], "UNDECIDED")


def native_suite():
    """Only the admitted controller calls this; no ambient database discovery."""
    import asyncpg
    import core_db_fixture as core_fixture
    from cortex_core.auth import AuthError
    from cortex_core.embeddings.pg_search import CoreUnavailable, PostgresSearch
    from cortex_core.embeddings.query_cache import CacheScope, QueryEmbeddingCache
    import os

    class NativeProductPathTests(ProductPathTests):
        async def asyncSetUp(self):
            await super().asyncSetUp()
            self.core = core_fixture.Fixture()
            self.core.setUp()
            self.addCleanup(self.core.doCleanups)
            self.core_calls = 0
            self.core_auth_lock = asyncio.Lock()
            self.spec["scope_map"] = [
                {"tenant": "ta", "project": "pa", "tenant_id": core_fixture.uid(2),
                 "project_id": core_fixture.uid(3)},
                {"tenant": "tb", "project": "empty", "tenant_id": core_fixture.uid(21),
                 "project_id": core_fixture.uid(3)}]
            self.spec["queries"][1]["tenant"] = "tb"
            self.spec["record_map"] = {"r1": core_fixture.uid(6), "r2": core_fixture.uid(1001),
                                       "r3": core_fixture.uid(1002)}
            # Canonical synthetic sources, before projection population. No public
            # product write or fabricated grant replaces the existing Core fixture.
            for record in (1001, 1002):
                with self.core.admin.transaction():
                    self.core.admin.execute("""INSERT INTO core.records
                        (tenant_id,project_id,id,kind,current_revision,tombstone)
                        VALUES (%s,%s,%s,'memory',1,false)""",
                        (core_fixture.uid(2), core_fixture.uid(3), core_fixture.uid(record)))
                    self.core.admin.execute("""INSERT INTO core.record_revisions
                        (tenant_id,project_id,record_id,revision,payload_ref,tombstone)
                        VALUES (%s,%s,%s,1,%s,false)""",
                        (core_fixture.uid(2), core_fixture.uid(3), core_fixture.uid(record), core_fixture.uid(5)))
            for name in ("001-pg-search.sql", "002-query-cache.sql"):
                self.core.admin.execute((NEXT / "schema/retrieval" / name).read_text())
            self.core.admin.execute("""GRANT USAGE ON SCHEMA retrieval TO search_runtime;
                GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA retrieval TO search_runtime;
                GRANT USAGE ON ALL SEQUENCES IN SCHEMA retrieval TO search_runtime""")
            self.pool = await asyncpg.create_pool(os.environ["B01_OWNED_SEARCH_DSN"], min_size=1, max_size=8)
            self.addAsyncCleanup(self.pool.close)

            async def authorize_component(connection, subject):
                current = await self.authorize(subject)
                partition = hashlib.sha256(canonical([str(v) for v in current.cache_partition])).hexdigest()
                return CacheScope(current.tenant_id, current.project_id, partition)

            self.cache = QueryEmbeddingCache(self.pool, authorize_component, self.provider.embed)
            self.search = PostgresSearch(self.pool, authorize_component)
            for subject in ("reader", "empty"):
                await self.search.configure(subject, IDENTITY, "ready")
            for index, public_id in enumerate(("r1", "r2", "r3")):
                record_id = self.spec["record_map"][public_id]
                await self.search.note_revision("reader", record_id, "memory", 1)
                await self.search.store_embedding("reader", record_id, 1, IDENTITY,
                                                  self.corpus.vectors[index].tolist())
            self.spec["source_sha256"] = {name: corpus.digest(NEXT / name) for name in (
                "src/cortex_core/auth.py", "src/cortex_core/embeddings/pg_search.py",
                "src/cortex_core/embeddings/query_cache.py", "src/cortex_core/modules/providers/hosted.py")}
            self.expected = hashlib.sha256(canonical(self.spec)).hexdigest()
            self.cache.calls, self.search.calls = 0, []
            actual_get, actual_search = self.cache.get, self.search.search

            async def observe_get(*args, **kwargs):
                self.cache.calls += 1
                return await actual_get(*args, **kwargs)

            async def observe_search(*args, **kwargs):
                self.search.calls.append((args, kwargs))
                return await actual_search(*args, **kwargs)

            self.cache.get, self.search.search = observe_get, observe_search
            self.core_calls = self.auth_calls = 0

        async def authorize(self, subject):
            self.core_calls += 1
            self.auth_calls += 1
            bindings = {"reader": (core_fixture.READ_A, 1), "empty": (core_fixture.READ_B, 20)}
            if subject not in bindings: raise PermissionError("fixture_subject_unknown")
            key, installation = bindings[subject]
            def current_scope():
                try:
                    # The returned value remains private until Core's context exits.
                    with self.core.auth(key=key, installation=installation) as current:
                        result = current
                    return result
                except AuthError as error:
                    if error.code == "core_unavailable": raise CoreUnavailable() from None
                    raise PermissionError("core_fixture_refused") from None
            # The existing fixture is synchronous. Serialize its one connection
            # off-loop so its SQL/refusal cannot stall the fixed-offer dispatcher.
            async with self.core_auth_lock:
                return await asyncio.to_thread(current_scope)

        async def test_native_actual_cache_provider_search_and_core_scope(self):
            self.assertIsInstance(self.cache, QueryEmbeddingCache)
            self.assertIsInstance(self.search, PostgresSearch)
            await self.test_actual_calls_cold_warm_and_independent_tiny_truth()
            self.assertGreaterEqual(self.core_calls, 6)

        async def test_native_empty_under_k_and_current_permission_generation(self):
            await self.test_empty_and_less_than_k_are_not_fictional_recall()
            before = self.provider_calls
            # Existing Core grant update owns its generation, no invented counter.
            self.core.admin.execute("""UPDATE auth.project_grants SET permissions=ARRAY['read','write']
                WHERE tenant_id=%s AND project_id=%s AND principal_id=%s""",
                (core_fixture.uid(2), core_fixture.uid(3), core_fixture.uid(8)))
            row = await self.evaluate()
            self.assertEqual(row["status"], "MEASURED")
            self.assertFalse(row["cache_hit"])
            self.assertEqual(self.provider_calls, before + 1)

        async def test_native_unsupported_inputs_and_frozen_binding_no_effect(self):
            before = self.core_calls
            await self.test_unsupported_filter_sparse_hybrid_and_metric_have_no_credit()
            self.assertEqual(self.core_calls, before)
            await self.test_changed_input_mapping_refused_before_any_port()

        async def test_native_fixed_offered_load_receipt_and_cleaned_connections(self):
            await self.test_fixed_offers_and_receipt_preserve_errors_and_no_gtm_credit()
            self.assertGreater(len(self.search.calls), 0)
            self.assertGreater(self.provider_calls, 0)

        async def test_native_warm_cache_core_down_has_no_favorable_credit(self):
            first = await self.evaluate()
            self.assertEqual(first["status"], "MEASURED")
            self.assertFalse(first["cache_hit"])
            before = (self.cache.calls, self.provider_calls, len(self.search.calls))
            self.core.request.close()
            row = await self.evaluate()
            self.assertEqual(row["status"], "UNAVAILABLE")
            self.assertNotIn("measurement", row)
            self.assertEqual((self.cache.calls, self.provider_calls, len(self.search.calls)), before)

        async def test_native_current_core_scope_must_match_frozen_mapping(self):
            changed = copy.deepcopy(self.spec)
            changed["scope_map"][0]["project_id"] = str(UUID(int=99))
            row = await self.evaluate(spec=changed, expected=hashlib.sha256(canonical(changed)).hexdigest())
            self.assertEqual(row["reason"], "current_scope_mismatch")
            self.assertGreater(self.core_calls, 0)
            self.assertEqual((self.cache.calls, self.provider_calls, len(self.search.calls)), (0, 0, 0))

        async def test_native_provider_vector_must_match_frozen_query(self):
            await self.test_unbound_provider_vector_has_no_recall_credit()
            self.assertEqual((self.cache.calls, self.provider_calls), (1, 1))

        async def test_native_projected_source_must_match_frozen_corpus(self):
            record = self.spec["record_map"]["r1"]
            await self.search.note_revision("reader", record, "session", 2)
            await self.search.store_embedding("reader", record, 2, IDENTITY, self.corpus.vectors[0].tolist())
            row = await self.evaluate()
            self.assertEqual(row["status"], "RESULT_REFUSED")
            self.assertEqual(row["reason"], "hit_source_mismatch")
            self.assertNotIn("measurement", row)
            self.assertEqual((self.cache.calls, self.provider_calls, len(self.search.calls)), (1, 1, 1))

        async def test_native_core_down_load_is_charged_as_error(self):
            first = await self.evaluate()
            self.assertEqual(first["status"], "MEASURED")
            before = (self.cache.calls, self.provider_calls, len(self.search.calls))
            self.core.request.close()
            module = surface()
            run = await load.run([self.spec["queries"][0]], lambda n: module.client(
                self.root, self.spec, self.expected, self.cache, self.search, self.authorize,
                source_root=NEXT), load.RunConfig(duration_seconds=.2, warmup_seconds=0, deadline_seconds=.1))
            self.assertEqual(len(run["records"]), 8)
            self.assertTrue(all(row["status"] == "ERROR" for row in run["records"]))
            self.assertTrue(all(row["latency_seconds"] >= .1 for row in run["records"]))
            self.assertEqual((self.cache.calls, self.provider_calls, len(self.search.calls)), before)

    methods = [name for name in dir(NativeProductPathTests) if name.startswith("test_native_")]
    return unittest.TestSuite(NativeProductPathTests(name) for name in methods)


class PreparedMemoryAdmissionTests(unittest.TestCase):
    """Exact controller functions with synthetic observations; never engine proof."""
    def setUp(self):
        source = (NEXT / "tests/benchmarks/mutate_b01_product_path.py").read_text()
        native = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "native")
        methods = [n for n in native.body if isinstance(n, ast.FunctionDef)
                   and n.name in ("engine_available_memory", "admit_resource_memory", "cached_runner")]
        self.memory = {"memTotal": 1000, "memFree": 350}
        self.available = None
        self.commands = []
        self.stack = SimpleNamespace(name="owned-fixture", owned=set())
        def command(arguments, **kwargs):
            if arguments[0] == "info": return json.dumps({"host": self.memory})
            if arguments[:3] == ["system", "connection", "list"]:
                return json.dumps([{"Default": True, "Name": "kos-e020-uat", "IsMachine": True,
                                    "URI": "ssh://core@127.0.0.1:1234/run/podman/podman.sock"}])
            if arguments[:2] == ["machine", "ssh"]:
                available = self.memory["memFree"] if self.available is None else self.available
                return f'MemTotal: {self.memory["memTotal"]} kB\nMemAvailable: {available} kB\nMemFree: {self.memory["memFree"]} kB\n'
            self.commands.append(arguments)
            return "synthetic-owned-id"
        namespace = {"json": json, "stack": self.stack, "memory_admissions": [],
            "postgres": SimpleNamespace(podman=command),
            "qdrant": SimpleNamespace(preflight=lambda **kwargs: {"free_percent": 44})}
        exec(compile(ast.Module(body=methods, type_ignores=[]), "<exact-memory-admission>", "exec"), namespace)
        self.runner = namespace["cached_runner"]

    def test_below_floor_refuses_before_create(self):
        self.memory["memFree"] = 349
        with self.assertRaises(RuntimeError): self.runner(["create", "--name", "fixture"])
        self.assertEqual(self.commands, [])

    def test_exact_floor_accepts_cached_only_create(self):
        self.runner(["create", "--name", "fixture"])
        self.assertEqual(self.commands, [["create", "--pull=never", "--name", "fixture"]])

    def test_memory_drop_after_create_refuses_start(self):
        self.runner(["create", "--name", "fixture"])
        self.stack.owned.add("container")
        self.memory["memFree"] = 349
        with self.assertRaises(RuntimeError): self.runner(["start", "synthetic-owned-id"])
        self.assertEqual(len(self.commands), 1)

    def test_available_memory_admits_reclaimable_cache_without_lowering_floor(self):
        self.memory["memFree"] = 100
        self.available = 350
        try: self.runner(["create", "--name", "fixture"])
        except RuntimeError as error: self.fail("accepted35percent available-memory fixture must admit: " + str(error))
        self.assertEqual(self.commands, [["create", "--pull=never", "--name", "fixture"]])


class S4ProductPathTests(unittest.IsolatedAsyncioTestCase):
    """Actual C11/C04/cache/S3 components over explicit memory/HTTP boundaries.

    SQL observations below are canned source-fixture values, never database proof.
    Existing S2/S3 transport and C07 fixtures are reused rather than reimplemented.
    """
    authorize = ProductPathTests.authorize

    async def asyncSetUp(self):
        await ProductPathTests.asyncSetUp(self)
        import importlib.util
        from contextlib import asynccontextmanager
        from dataclasses import asdict
        from unittest.mock import patch
        from cortex_core.api.c11b2 import C11b2ReadPort
        from cortex_core.auth import Scope
        from cortex_core.modules.vector.search import QdrantProjection
        path = NEXT / 'tests/receipt/test_c09_qdrant.py'
        loader = importlib.util.spec_from_file_location('s4_owned_s3_fixtures', path)
        self.fx = importlib.util.module_from_spec(loader); loader.loader.exec_module(self.fx)
        self.binding = self.fx.S3Tests().binding(identity=IDENTITY)
        self.server = self.fx.QueryProjectionServer()
        self.target_rows = []
        self.pending = 0; self.revoked = False; self.generation = 1; self.indexed = 3
        self.sql_calls = []; self.boundary = []; self.cache_rows = {}; self.recorded = []
        self.spec['record_map'] = {sid: str(UUID(int=10+i)) for i, sid in enumerate(('r1','r2','r3'))}
        self.expected = hashlib.sha256(canonical(self.spec)).hexdigest()
        self.network_deny = patch('socket.socket.connect', side_effect=AssertionError('IP forbidden'))
        self.network_deny.start(); self.addCleanup(self.network_deny.stop)
        test = self
        self.private = self.fx.S2Tests().client(self.server)
        await self.private.__aenter__(); self.addAsyncCleanup(self.private.__aexit__, None, None, None)
        # Projection uses the real C07 state machine, sink, S2 client and v2 receipt.
        from cortex_core.modules.vector.consumer_adapter import QdrantSink
        self.sink_clients = []
        def factory():
            client = test.fx.S2Tests().client(test.server); test.sink_clients.append(client); return client
        async def embed(identity, payload):
            test.boundary.append('projection-provider')
            return test.corpus.vectors[len(test.recorded)].tolist()
        self.sink = QdrantSink(self.binding, factory, embed)
        self.addCleanup(self.sink.close)
        # Reuse the existing C07 transaction-boundary fixture, not a stubbed
        # _record: original validated v2 JSON reaches the real _record/_call.
        import sys
        sys.path.insert(0,str(NEXT/'tests/receipt'))
        try: from test_c09_qdrant_s3b import CoreBoundaryTests
        finally: sys.path.pop(0)
        consumer_fixture = CoreBoundaryTests()
        self.addCleanup(consumer_fixture.doCleanups)
        def checkpoint():
            receipt=json.loads(consumer_fixture.record_attempts[-1][-1])
            test.fx.s3.validate_receipt(receipt,test.binding,test.event)
            test.recorded.append(receipt);test.boundary.append('core-checkpoint')
        self.consumer = consumer_fixture.port(after_checkpoint=checkpoint)
        self.consumer.sink = self.sink
        self.consumer.external_binding = self.binding
        self.core_fixture = consumer_fixture
        for i in range(3):
            event = self.fx.S3Tests().event(aggregate_id=str(UUID(int=10+i)), event_id=str(UUID(int=100+i)))
            self.event=event
            await asyncio.to_thread(self.consumer._apply_event, {'generation':1}, event)
        for point in self.server.points.values():
            p = point['payload']
            self.target_rows.append(dict(record_id=p['aggregate_id'],kind=p['aggregate_kind'],revision=p['revision'],
                tombstone=p['tombstone'],indexed_revision=p['revision'],event_id=p['event_id'],
                payload_sha256=p['payload_sha256'],effect_sha256=p['effect_sha256'],point_id=point['id'],indexed=True))
        class Connection:
            def is_closed(self): return False
            def is_in_transaction(self): return False
            @asynccontextmanager
            async def transaction(self, **options):
                test.boundary.append('c04-enter')
                yield
                test.boundary.append('c04-exit')
            async def execute(self, sql, *args):
                test.sql_calls.append(sql)
                if sql.startswith('DELETE FROM retrieval.query_embeddings') and 'embedding IS NULL' in sql:
                    test.cache_rows.pop(args[:5], None)
            async def fetchrow(self, sql, *args):
                test.sql_calls.append(sql)
                if 'auth.resolve_scope' in sql:
                    if test.revoked: return None
                    return asdict(Scope(test.binding.installation_id,TA,PA,UUID(int=50),test.generation,'read'))
                if 'current_setting' in sql:
                    return (hashlib.sha256(b'synthetic').hexdigest(), str(test.binding.installation_id), str(PA), 'read')
                if 'retrieval.query_embeddings' in sql:
                    row = test.cache_rows.get(args[:5])
                    return None if row is None else dict(vector=row, fresh=True)
                raise AssertionError('unrecognized canned fetchrow')
            async def fetchval(self, sql, *args):
                test.sql_calls.append(sql)
                if 'coordination.c09_state' in sql:
                    return dict(binding=test.fx.s3.binding_contract(test.binding),indexed_records=test.indexed,
                                pending_records=test.pending,targets=[copy.deepcopy(r) for r in test.target_rows
                                    if UUID(r['record_id']) in args[1]])
                if 'SELECT count(*) FROM retrieval.query_embeddings' in sql: return len(test.cache_rows)
                if 'SELECT count(*) FROM core.records' in sql: return 0
                if sql.lstrip().startswith('INSERT INTO retrieval.query_embeddings'):
                    test.cache_rows[args[:5]] = None; return 1
                if sql.lstrip().startswith('UPDATE retrieval.query_embeddings'):
                    test.cache_rows[args[:5]] = args[7]; return 1
                if 'auth.bind_scope' in sql or 'current_user' in sql: return True
                raise AssertionError('unrecognized canned fetchval: '+sql)
        class Pool:
            @asynccontextmanager
            async def acquire(self, **options): yield Connection()
        class Records:
            installation_id = test.binding.installation_id
            projects = {'owned': PA}
            def _project(self, scope): return PA
            async def principal(self, scope):
                test.boundary.append('c11-current')
                if test.revoked: raise PermissionError('synthetic-private-auth')
                return dict(principal_id=str(UUID(int=50)), tenant_id=str(TA), project_id=str(PA),
                            permission_generation=test.generation)
        self.read = C11b2ReadPort(Records(),Pool(),IDENTITY,self.provider,'synthetic',{},
                                 vector_projection=QdrantProjection(self.binding,self.private))

    def make_client(self, **options):
        def scope_for(query):
            return dict(type='http',method='POST',path='/search',headers=[(b'authorization',b'Bearer synthetic')])
        return surface().client(self.root,self.spec,self.expected,self.cache,self.search,self.authorize,
                                source_root=NEXT,read_port=self.read,scope_for=scope_for,**options)

    async def response(self, client=None, query=None):
        client = client or self.make_client()
        if client._prepared is None: await client.open()
        caught = None; response = None
        try: response = await client.request(query or self.spec['queries'][0])
        except Exception as error: caught = error
        self.assertIsNone(caught, 'actual C11 SOURCE_FIXTURE request must complete: '+str(type(caught)))
        return client, response

    async def test_s4_actual_c11_cache_qdrant_response_and_offline_truth(self):
        client, cold = await self.response()
        _, warm = await self.response(client)
        self.assertEqual((cold['cache_hit'],warm['cache_hit']), (False,True))
        self.assertEqual((cold['provider_embed_calls'],warm['provider_embed_calls']), (1,0))
        self.assertEqual(self.provider_calls,1, 'count actual MockTransport requests, not misses')
        self.assertTrue(any('auth.resolve_scope' in q for q in self.sql_calls))
        self.assertTrue(any('/points/query' in call[1] for call in self.server.calls))
        self.assertEqual(len(self.search.calls),0, 'diagnostic direct-search port must not run')
        self.assertEqual(client.compare('q1',cold)['tie_recall'],1.)
        self.assertNotIn('measurement',cold, 'offline comparison belongs after load')
        self.assertFalse(cold['api_qualified'])

    async def test_s4_no_truth_or_corpus_scan_inside_request(self):
        from unittest.mock import patch
        client = self.make_client(); await client.open()
        with patch.object(oracle,'rank',side_effect=AssertionError('timed oracle forbidden')), \
             patch.object(corpus,'load',side_effect=AssertionError('timed corpus read forbidden')):
            _, response = await self.response(client)
        self.assertEqual(client.compare('q1',response)['strict_recall'],1.)

    async def test_s4_serialization_completes_before_return(self):
        from unittest.mock import patch
        from cortex_core.api import c11a
        actual = c11a._respond; events=[]
        async def observed(send,status,body):
            events.append('serialization-start'); await actual(send,status,body); events.append('body-sent')
        with patch.object(c11a,'_respond',observed):
            client, response = await self.response()
            events.append('request-return')
        self.assertEqual(events,['serialization-start','body-sent','request-return'])
        body = json.loads(response['body'])
        self.assertEqual(body['results'][0]['id'],self.spec['record_map']['r1'])
        self.assertEqual(hashlib.sha256(response['body'].encode()).hexdigest(),response['response_sha256'])

    async def test_s4_frozen_input_and_lookup_survive_caller_mutation(self):
        client = self.make_client(); await client.open()
        query = copy.deepcopy(self.spec['queries'][0])
        self.spec['record_map']['r1']='foreign'
        self.spec['queries'][0]['text']='changed'
        _, response = await self.response(client, query)
        self.assertEqual(client.compare('q1',response)['tie_recall'],1.)
        self.assertEqual(json.loads(response['body'])['query'],'basis-a')

    async def test_s4_changed_offer_cannot_substitute_frozen_query(self):
        client = self.make_client(); await client.open()
        changed = {**self.spec['queries'][0], 'text':'different'}
        caught=None
        try: await client.request(changed)
        except Exception as error: caught=error
        self.assertIsInstance(caught,surface().UnfulfilledResult)
        self.assertEqual(self.provider_calls,0)
        self.assertFalse(any('/points/query' in c[1] for c in self.server.calls))

    async def test_s4_fresh_cache_still_obeys_current_core_and_no_engine_on_revoke(self):
        client, _ = await self.response()
        before = len(self.server.calls); self.revoked=True
        with self.assertRaises(surface().UnfulfilledResult): await client.request(self.spec['queries'][0])
        self.assertEqual(len(self.server.calls),before)
        self.assertEqual(self.provider_calls,1)

    async def test_s4_lag_and_changed_permission_are_charged_unfulfilled(self):
        client = self.make_client(); await client.open(); self.pending=1
        with self.assertRaises(surface().UnfulfilledResult): await client.request(self.spec['queries'][0])
        self.pending=0
        self.server.on_query=lambda: setattr(self,'generation',2)
        with self.assertRaises(surface().UnfulfilledResult): await client.request(self.spec['queries'][0])
        self.assertEqual(self.provider_calls,1)

    async def test_s4_unbound_provider_vector_has_no_c11_result_credit(self):
        self.provider_vector=[0.,1.]+[0.]*766
        client=self.make_client(); await client.open()
        with self.assertRaises(surface().UnfulfilledResult): await client.request(self.spec['queries'][0])
        self.assertEqual(self.provider_calls,1)
        self.assertFalse(any('/points/query' in c[1] for c in self.server.calls))

    async def test_s4_offline_unknown_foreign_duplicate_kind_revision_and_identity_refuse(self):
        client, response=await self.response()
        for field,value in [('id','unknown'),('kind','foreign'),('revision',2),('distance',float('nan'))]:
            with self.subTest(field=field):
                altered=copy.deepcopy(response);body=json.loads(altered['body']);body['results'][0][field]=value
                altered['body']=json.dumps(body);altered['response_sha256']=hashlib.sha256(altered['body'].encode()).hexdigest()
                with self.assertRaises(surface().Refusal):client.compare('q1',altered)
        body=json.loads(response['body']);body['results'][1]=copy.deepcopy(body['results'][0])
        altered={**response,'body':json.dumps(body)};altered['response_sha256']=hashlib.sha256(altered['body'].encode()).hexdigest()
        with self.assertRaises(surface().Refusal):client.compare('q1',altered)
        body=json.loads(response['body']);body['freshness']['identity']='foreign'
        altered={**response,'body':json.dumps(body)};altered['response_sha256']=hashlib.sha256(altered['body'].encode()).hexdigest()
        with self.assertRaises(surface().Refusal):client.compare('q1',altered)

    async def test_s4_response_input_truth_and_bytes_are_bound(self):
        client,response=await self.response()
        for key in ('input_sha256','truth_sha256','response_sha256','query_id'):
            with self.subTest(key=key),self.assertRaises(surface().Refusal):client.compare('q1',{**response,key:'changed'})

    async def test_s4_shared_frozen_truth_precedes_client_open_and_survives_close(self):
        prepare=getattr(surface(),'prepare_input',None)
        self.assertTrue(callable(prepare),'one shared admitted-input seam required')
        frozen=prepare(self.root,self.spec,self.expected,source_root=NEXT)
        from unittest.mock import patch
        with patch.object(oracle,'rank',side_effect=AssertionError('truth already frozen')):
            client=self.make_client(prepared=frozen);await client.open()
            _,response=await self.response(client)
        await client.aclose()
        self.assertEqual(client.compare('q1',response)['tie_recall'],1.)

    async def test_s4_prepare_refuses_unbound_native_state_and_miss_budget(self):
        client=self.make_client();await client.open()
        prepare=getattr(client,'prepare_measured',None)
        self.assertTrue(callable(prepare),'phase seam must refuse rather than invent native/cache evidence')
        config=load.RunConfig(warmup_seconds=0)
        refusal=await prepare([],config)
        self.assertEqual((refusal['status'],refusal['capability'],refusal['sustained_all_miss']),
                         ('NOT_RUN','UNAVAILABLE','NOT_RUN'))
        self.assertEqual(refusal['input_sha256'],self.expected)
        self.assertEqual(len(refusal['preparation_sha256']),64)
        self.assertNotIn('native_qualified',refusal)

    async def test_s4_preparation_is_bound_to_each_config_and_warmup_inventory(self):
        client=self.make_client();await client.open()
        prepare=getattr(client,'prepare_measured',None);self.assertTrue(callable(prepare))
        a=await prepare([],load.RunConfig(warmup_seconds=0))
        b=await prepare([{'offer_id':0,'status':'ERROR'}],load.RunConfig(warmup_seconds=.025))
        self.assertNotEqual(a['preparation_sha256'],b['preparation_sha256'])
        self.assertEqual(b['warmup_errors'],1, 'warmup failures must remain charged')
        self.assertEqual(b['status'],'NOT_RUN')

    async def test_s4_capability_stays_unavailable_and_projection_custody_closes(self):
        client,response=await self.response()
        scope=dict(method='GET',headers=[(b'authorization',b'Bearer synthetic')])
        principal=await self.read.principal(scope)
        self.assertEqual((await self.read.capability_source('pg_search',principal))['state'],'unavailable')
        self.assertEqual(len(self.recorded),3)
        self.assertTrue(all(r['version']==2 for r in self.recorded))
        self.assertEqual(len(self.core_fixture.record_attempts),3)
        self.assertEqual(len(self.core_fixture.transaction_exits),6)
        self.assertEqual([json.loads(args[-1]) for args in self.core_fixture.record_attempts],self.recorded)
        self.assertEqual(self.boundary[:6],['projection-provider','core-checkpoint']*3)
        self.assertTrue(all(c._http.is_closed and c.status()['pending_operations']==0 for c in self.sink_clients))
        self.assertFalse(self.private.status()['module_ready'])
        await client.aclose();self.assertIsNone(client._prepared)

    async def test_s4_engine_failure_load_keeps_every_offer_and_scrubs_secrets(self):
        self.server.fault='query_failure'
        run=await load.run([self.spec['queries'][0]],lambda n:self.make_client(),
                           load.RunConfig(duration_seconds=.2,warmup_seconds=0,deadline_seconds=.1))
        self.assertEqual(len(run['records']),8)
        self.assertTrue(all(r['status']=='ERROR' for r in run['records']))
        self.assertEqual((run['clients_opened'],run['clients_closed']),(8,8))
        self.assertNotIn('SYNTHETIC-PRIVATE',str(run))
        self.assertEqual(len(self.search.calls),0)

    async def test_s4_public_gateway_preserves_unavailable_admission(self):
        from cortex_core.api.c11a import ConsumerGateway
        async def probe(): return True
        async def health(): return {'component':'cortex','status':'unavailable'}
        gateway=ConsumerGateway(core_probe=probe,principal_resolver=self.read.principal,
            permission_recheck=self.read.permission_recheck,capability_source=self.read.capability_source,
            health=health,handlers=self.read.handlers,parse_search_body=True,
            search_admission=self.read.search_admission)
        messages=[]
        async def receive(): return {'type':'http.request','body':b'{"query":"basis-a","top_k":2}'}
        async def send(message): messages.append(message)
        await gateway.app(dict(type='http',method='POST',path='/search',
            headers=[(b'authorization',b'Bearer synthetic')]),receive,send)
        self.assertEqual(messages[0]['status'],503)
        self.assertEqual(json.loads(messages[1]['body'])['error']['code'],'capability_unavailable')
        self.assertEqual(self.provider_calls,0)
        self.assertFalse(any('/points/query' in c[1] for c in self.server.calls))

    async def test_s4_empty_and_under_k_use_offline_frozen_truth(self):
        self.spec['queries'][0]['limit']=10
        self.expected=hashlib.sha256(canonical(self.spec)).hexdigest()
        client,response=await self.response()
        self.assertEqual(client.compare('q1',response)['expected_count'],3)
        self.assertEqual(client.compare('q1',response)['tie_recall'],1.)
        # A different admitted corpus with no records in the requested scope.
        self.root=self.root.parent/'empty-corpus'
        c=corpus.write_corpus(self.root,self.corpus.vectors,
            [{'id':sid,'tenant':'ta','project':'empty'} for sid in ('r1','r2','r3')],self.corpus.identity)
        self.spec['corpus_manifest_sha256']=corpus.digest(self.root/'manifest.json')
        self.expected=hashlib.sha256(canonical(self.spec)).hexdigest()
        self.indexed=0;self.target_rows=[];self.server.points.clear()
        client,response=await self.response()
        measurement=client.compare('q1',response)
        self.assertTrue(measurement['empty_exact']);self.assertIsNone(measurement['tie_recall'])

    async def test_s4_serialization_over_deadline_is_unfulfilled(self):
        from unittest.mock import patch
        from cortex_core.api import c11a
        actual=c11a._respond
        async def delayed(*args): await asyncio.sleep(.6); await actual(*args)
        client=self.make_client();await client.open()
        client.options['scope_for']=lambda q:dict(headers=[(b'authorization',b'Bearer synthetic')],
            _cortex_deadline=asyncio.get_running_loop().time()+.5)
        with patch.object(c11a,'_respond',delayed),self.assertRaises(surface().UnfulfilledResult):
            await client.request(self.spec['queries'][0])

    async def test_s4_closed_client_refuses_without_dependency_exception(self):
        client=self.make_client();await client.open();await client.aclose()
        caught=None
        try:await client.request(self.spec['queries'][0])
        except Exception as error:caught=error
        self.assertIsInstance(caught,surface().UnfulfilledResult)

    async def test_s4_provider_refusal_is_not_counted_as_actual_http_call(self):
        async def unavailable(provider):raise RuntimeError('synthetic-private-credential')
        self.provider.resolve_key=unavailable
        client=self.make_client();await client.open()
        with self.assertRaises(surface().UnfulfilledResult):await client.request(self.spec['queries'][0])
        self.assertEqual(self.provider_calls,0)
        self.assertEqual(client._observed['provider_embed_calls'],1)
        self.assertFalse(any('/points/query' in c[1] for c in self.server.calls))

    async def test_s4_pg_c11_control_keeps_credentialed_cache_without_qdrant(self):
        from unittest.mock import patch
        from cortex_core.runtime.pg_adapters import _C04Search
        self.read.vector_projection=None
        async def search(port,subject,identity,vector,**options):
            # This is an explicit PG result fixture; no SQL or native PG credit.
            self.assertIsInstance(port,_C04Search)
            return SearchResult(tuple(Hit(self.spec['record_map'][sid],'memory',1,0.) for sid in ('r1','r2')),
                                Freshness(identity.key,3,0))
        with patch.object(_C04Search,'search',search):client,response=await self.response()
        self.assertEqual(client.compare('q1',response)['tie_recall'],1.)
        self.assertEqual(self.provider_calls,1)
        self.assertTrue(any('auth.resolve_scope' in q for q in self.sql_calls))
        self.assertFalse(any('/points/query' in c[1] for c in self.server.calls))


    async def test_s4_empty_held_out_set_is_fixed_admission_refusal(self):
        self.spec['splits']={'tuning':['q1','empty'],'held_out':[]}
        self.expected=hashlib.sha256(canonical(self.spec)).hexdigest()
        client=self.make_client();caught=None
        try:await client.open()
        except Exception as error:caught=error
        self.assertIsInstance(caught,surface().UnfulfilledResult)
        self.assertEqual(self.provider_calls,0)
        self.assertFalse(any('/points/query' in c[1] for c in self.server.calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
