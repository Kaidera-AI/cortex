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


if __name__ == "__main__":
    unittest.main(verbosity=2)
