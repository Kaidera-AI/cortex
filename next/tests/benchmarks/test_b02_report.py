"""Hand-computed accounting and admission negatives, frozen before source."""
import copy
import importlib
from pathlib import Path
import unittest


def fixture():
    rows = [{"offer_id": i, "client": i % 8, "query_id": "q", "status": "OK",
             "phase": "measured", "scheduled_offset_seconds": i / 40,
             "scheduled_at": 100 + i / 40, "dispatched_at": 100 + i / 40,
             "dispatcher_lag_seconds": 0.0, "started_at": 100 + i / 40,
             "completed_at": 100 + i / 40 + .001, "latency_seconds": .001, "queue_seconds": 0,
             "measurement": {"safe": True, "strict_recall": 1., "tie_recall": 1.,
                             "eligible_count": 12, "expected_count": 10}} for i in range(40)]
    return {"config": {"duration_seconds": 1, "warmup_seconds": 0, "rps": 40, "clients": 8,
                       "deadline_seconds": 10}, "records": rows, "warmup_records": [],
            "clients_opened": 8, "clients_closed": 8, "close_errors": [],
            "observations": [{"scope": "fixture-only"}], "observation_errors": [],
            "measured_start": 100, "measured_end": 105}


class ReportTests(unittest.TestCase):
    def surface(self, name="report"):
        path = Path(__file__).resolve().parents[2] / f"src/vector_baseline/{name}.py"
        self.assertTrue(path.is_file(), f"B02 {name} surface absent")
        return importlib.import_module("vector_baseline." + name)

    def summarize(self, run):
        return self.surface().summarize(run, latency_budget_ms=100, bindings={"dataset": "synthetic"})

    def test_nearest_rank_p95_uses_exact_boundary(self):
        report = self.surface()
        self.assertEqual(report.nearest_rank(list(range(1, 21)), .95), 19)
        self.assertEqual(report.nearest_rank([5], .95), 5)
        with self.assertRaises(ValueError):
            report.nearest_rank([], .95)

    def test_failure_latencies_are_included_and_success_floor_is_independent(self):
        run = fixture()
        for row in run["records"][-4:]:
            row.update(status="TIMEOUT", latency_seconds=10)
            row.pop("measurement")
        result = self.summarize(run)
        self.assertEqual(result["diagnostic"]["offered"], 40)
        self.assertEqual(result["diagnostic"]["successful_rps"], 36)
        self.assertEqual(result["diagnostic"]["p95_ms"], 10000)
        self.assertEqual(result["diagnostic"]["verdict"], "FAIL")

    def test_throughput_uses_scheduled_interval_not_drain_duration(self):
        result = self.summarize(fixture())
        self.assertEqual(result["diagnostic"]["successful_rps"], 40)
        self.assertEqual(result["diagnostic"]["throughput_status"], "PASS")

    def test_high_mean_cannot_hide_a_forbidden_or_duplicate_hit(self):
        run = fixture()
        run["records"][0]["measurement"]["safe"] = False
        result = self.summarize(run)
        self.assertEqual(result["diagnostic"]["mean_tie_recall"], 1)
        self.assertEqual(result["diagnostic"]["recall_status"], "FAIL")
        self.assertEqual(result["diagnostic"]["verdict"], "FAIL")

    def test_empty_only_has_no_fictional_recall_pass(self):
        run = fixture()
        for row in run["records"]:
            row["measurement"].update(tie_recall=None, strict_recall=None, eligible_count=0,
                                      expected_count=0, empty_exact=True)
        result = self.summarize(run)
        self.assertIsNone(result["diagnostic"]["mean_tie_recall"])
        self.assertEqual(result["diagnostic"]["recall_status"], "NOT_RUN")

    def test_missing_or_duplicate_offer_and_partial_clients_refuse_accounting(self):
        for kind in ["missing", "duplicate", "client", "close"]:
            run = fixture()
            if kind == "missing":
                run["records"].pop()
            elif kind == "duplicate":
                run["records"][-1]["offer_id"] = 0
            elif kind == "client":
                run["clients_opened"] = 7
            else:
                run["close_errors"] = ["RuntimeError"]
            with self.subTest(kind=kind):
                self.assertFalse(self.summarize(run)["diagnostic"]["accounting_valid"])

    def test_warmup_is_excluded_and_collection_gaps_are_not_hidden(self):
        run = fixture()
        run["warmup_records"] = copy.deepcopy(run["records"])
        run["config"]["warmup_seconds"] = 1
        for row in run["warmup_records"]:
            row["phase"] = "warmup"
        result = self.summarize(run)
        self.assertEqual(result["diagnostic"]["offered"], 40)
        self.assertEqual(result["diagnostic"]["successful_rps"], 40)
        run["observation_errors"] = ["RuntimeError"]
        self.assertEqual(self.summarize(run)["diagnostic"]["verdict"], "FAIL")

    def test_short_synthetic_run_cannot_select_engine_or_native_acceptance(self):
        result = self.summarize(fixture())
        self.assertEqual(result["engine_decision"], "UNDECIDED")
        self.assertFalse(result["gtm_qualified"])
        self.assertTrue(result["not_run"])
        self.assertEqual(result["qualification"], "SYNTHETIC_SQL_DIAGNOSTIC")

    def test_real_or_remote_inputs_refuse_before_resource_creation(self):
        benchmark = self.surface("benchmark")
        for manifest in [{"dataset": "marlow-4oct"}, {"dataset": "synthetic", "remote": True}]:
            with self.subTest(manifest=manifest):
                with self.assertRaises(ValueError):
                    benchmark.admit(manifest)
        self.assertIsNone(benchmark.admit({"dataset": "synthetic"}))

    def test_results_bind_each_response_to_its_own_full_oracle(self):
        benchmark = self.surface("benchmark")
        from vector_baseline import corpus, oracle
        import tempfile
        identity = {"provider": "synthetic", "model": "fixture", "dimension": 2,
                    "metric": "dot", "generation": "synthetic-v1"}
        with tempfile.TemporaryDirectory() as tmp:
            c = corpus.write_corpus(Path(tmp) / "c", [[1, 0], [2, 0]], [{"id": "a"}, {"id": "b"}], identity)
            q = {"id": "q", "status": "READY", "mode": "dense", "tenant": "t1", "project": "p1",
                 "generation": "synthetic-v1", "vector": [1, 0], "lo": None, "hi": None,
                 "kind": None, "time_lo": None, "time_hi": None, "sparse": None}
            truth = oracle.rank(c, q)
            run = {"records": [{"offer_id": 0, "query_id": "q", "status": "OK", "response": {"ids": ["a", "b"]}},
                               {"offer_id": 1, "query_id": "q", "status": "OK", "response": {"ids": ["forbidden"]}}]}
            benchmark.attach_truth(run, {"q": truth})
            self.assertTrue(run["records"][0]["measurement"]["safe"])
            self.assertFalse(run["records"][1]["measurement"]["safe"])

    def test_warmup_failure_is_visible_and_prevents_diagnostic_pass(self):
        run = fixture()
        run["warmup_records"] = copy.deepcopy(run["records"])
        run["config"]["warmup_seconds"] = 1
        for row in run["warmup_records"]:
            row["phase"] = "warmup"
        run["warmup_records"][0]["status"] = "ERROR"
        result = self.summarize(run)
        self.assertEqual(result["diagnostic"]["warmup_errors"], 1)
        self.assertEqual(result["diagnostic"]["verdict"], "FAIL")


    def test_omitted_heldout_query_is_explicit_not_run(self):
        result = self.surface().summarize(fixture(), latency_budget_ms=100,
                                         bindings={"dataset": "synthetic", "heldout_query_ids": ["q", "omitted"]})
        self.assertEqual(result["diagnostic"]["recall_status"], "NOT_RUN")
        self.assertEqual(result["diagnostic"]["missing_heldout_queries"], ["omitted"])
        self.assertEqual(result["diagnostic"]["verdict"], "NOT_RUN")

    def test_interrupt_is_named_in_saved_fail_receipt_and_propagates(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        import tempfile
        import json
        benchmark = self.surface("benchmark")
        class Stack:
            cleanup_verified = False
            password = "synthetic-secret"
            lock = object()
            connection = None
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.cleanup_verified = True
                self.password = self.lock = None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "heldout.jsonl").write_text(json.dumps({"id": "q", "stratum": "scope", "mode": "dense", "status": "READY"}) + "\n")
            (root / "query-manifest.json").write_text(json.dumps({"corpus_manifest": "hash", "queries": {"heldout": "hash"}}))
            c = SimpleNamespace(path=root, manifest={"dataset": "synthetic"}, identity={}, records=[None])
            output = root / "result.json"
            with patch.object(benchmark.corpus, "load", return_value=c), \
                    patch.object(benchmark.corpus, "digest", return_value="hash"), \
                    patch.object(benchmark.oracle, "rank", return_value={}), \
                    patch.object(benchmark.postgres, "DisposablePostgres", Stack), \
                    patch.object(benchmark.postgres, "install", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    benchmark.execute(root, output, duration=1, warmup=0)
            receipt = json.loads(output.read_text())
            self.assertEqual(receipt["runner_error_class"], "KeyboardInterrupt")
            self.assertEqual(receipt["diagnostic"]["verdict"], "FAIL")
            self.assertTrue(receipt["cleanup_verified"])
            self.assertTrue(receipt["credential_discarded"])
            self.assertTrue(receipt["lock_released"])


    def test_mutation_custody_resolves_alias_and_rejects_foreign_source(self):
        import importlib.util
        import tempfile
        path = Path(__file__).with_name("mutate_b02.py")
        spec = importlib.util.spec_from_file_location("b02_mutation_fixture", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(hasattr(module, "source_custody"), "canonical path custody helper missing")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owned = root / "owned"
            owned.mkdir()
            source = owned / "load.py"
            source.write_text("fixture")
            alias = root / "alias"
            alias.symlink_to(owned, target_is_directory=True)
            rows = {"load.py": {"path": str(source.resolve()), "sha256": "hash"}}
            self.assertTrue(module.source_custody(rows, {"load.py": "hash"}, alias))
            self.assertFalse(module.source_custody(rows, {"load.py": "different"}, alias))
            self.assertFalse(module.source_custody(rows, {"load.py": "hash"}, root / "foreign"))


class S4bProductAccountingTests(unittest.IsolatedAsyncioTestCase):
    """Reuse S4a's actual C11/C04/S2/S3 fixture, not another product port."""
    async def asyncSetUp(self):
        from test_b01_product_path import S4ProductPathTests
        await S4ProductPathTests.asyncSetUp(self)

    async def authorize(self, subject):
        from test_b01_product_path import ProductPathTests
        return await ProductPathTests.authorize(self, subject)

    def make_client(self, **options):
        from test_b01_product_path import S4ProductPathTests
        return S4ProductPathTests.make_client(self, **options)

    def surface(self, name):
        from vector_baseline import benchmark
        value = getattr(benchmark, name, None)
        self.assertTrue(callable(value), "S4b offline/accounting seam missing: " + name)
        return value

    async def retained(self):
        client = self.make_client()
        await client.open()
        response = await client.request(self.spec["queries"][0])
        await client.aclose()
        value = fixture()
        for row in value["records"]:
            row["query_id"] = "q1"
            row["response"] = copy.deepcopy(response)
            row.pop("measurement")
        return value, client.frozen

    def diagnostic(self, value):
        from vector_baseline import report
        return report.summarize(value, latency_budget_ms=100,
                                bindings={"heldout_query_ids": ["q1"]})["diagnostic"]

    def poison(self, row):
        import hashlib, json
        body = json.loads(row["response"]["body"])
        body["results"][0]["id"] = "foreign"
        row["response"]["body"] = json.dumps(body)
        row["response"]["response_sha256"] = hashlib.sha256(row["response"]["body"].encode()).hexdigest()

    async def test_each_retained_response_is_compared_offline_and_refusal_is_charged_without_retiming(self):
        attach = self.surface("attach_product_truth")
        value, frozen = await self.retained()
        self.poison(value["records"][-1])
        before = copy.deepcopy(value)
        attach(value, frozen)
        bad = value["records"][-1]
        self.assertEqual((len(value["records"]), bad["status"]), (40, "ERROR"))
        self.assertEqual(bad["error_class"], "OfflineComparisonRefusal")
        self.assertNotIn("measurement", bad)
        self.assertEqual(bad["response_elapsed_seconds"], .001)
        self.assertEqual(bad["latency_seconds"], 10)
        for old, row in zip(before["records"], value["records"]):
            for key in ("offer_id", "client", "query_id", "phase", "scheduled_at", "scheduled_offset_seconds",
                        "dispatched_at", "dispatcher_lag_seconds", "started_at", "completed_at", "queue_seconds", "response"):
                self.assertEqual(row[key], old[key], key)
        good = value["records"][0]
        self.assertEqual(good["measurement"]["tie_recall"], 1.)
        self.assertEqual(good["measurement_binding"], {key: good["response"][key]
            for key in ("query_id", "input_sha256", "truth_sha256", "response_sha256")})
        result = self.diagnostic(value)
        self.assertEqual((result["offered"], result["successful_responses"], result["errors_or_missed"]), (40, 39, 1))
        self.assertEqual(result["verdict"], "FAIL")

    async def test_original_error_timeout_and_missed_rows_are_never_erased_or_recompared(self):
        attach = self.surface("attach_product_truth")
        from unittest.mock import patch
        value, frozen = await self.retained()
        for row, status in zip(value["records"], ("ERROR", "TIMEOUT", "MISSED")):
            row.update(status=status, error_class="OriginalFailure", latency_seconds=10)
        before = copy.deepcopy(value["records"][:3])
        with patch.object(frozen, "compare", wraps=frozen.compare) as compare:
            attach(value, frozen)
        self.assertEqual(value["records"][:3], before)
        self.assertEqual(compare.call_count, 37)
        self.assertEqual(len(value["records"]), 40)

    async def test_warmup_comparison_failure_and_earlier_error_both_survive(self):
        attach = self.surface("attach_product_truth")
        value, frozen = await self.retained()
        value["config"]["warmup_seconds"] = 1
        value["warmup_records"] = copy.deepcopy(value["records"])
        for row in value["warmup_records"]:
            row["phase"] = "warmup"
        self.poison(value["warmup_records"][0])
        value["warmup_records"][1].update(status="ERROR", error_class="OriginalFailure", latency_seconds=10)
        original = copy.deepcopy(value["warmup_records"][1])
        attach(value, frozen)
        self.assertEqual(value["warmup_records"][0]["status"], "ERROR")
        self.assertEqual(value["warmup_records"][1], original)
        self.assertEqual(self.diagnostic(value)["warmup_errors"], 2)
        self.assertEqual(self.diagnostic(value)["verdict"], "FAIL")

    async def test_legacy_diagnostic_response_cannot_supply_s4_recall_credit(self):
        attach = self.surface("attach_product_truth")
        value, frozen = await self.retained()
        for row in value["records"]:
            row["response"] = {"ids": ["r1", "r2"], "measurement": {"safe": True, "tie_recall": 1.}}
            row["measurement"] = {"safe": True, "tie_recall": 1., "strict_recall": 1.}
        attach(value, frozen)
        self.assertTrue(all(row["status"] == "ERROR" and "measurement" not in row for row in value["records"]))
        self.assertEqual(self.diagnostic(value)["successful_responses"], 0)

    async def test_each_response_query_input_truth_and_bytes_binding_is_enforced(self):
        attach = self.surface("attach_product_truth")
        value, frozen = await self.retained()
        for row, key in zip(value["records"], ("query_id", "input_sha256", "truth_sha256", "response_sha256")):
            row["response"][key] = "changed"
        attach(value, frozen)
        self.assertEqual([row["status"] for row in value["records"][:4]], ["ERROR"] * 4)
        self.assertEqual(self.diagnostic(value)["p95_ms"], 10000)
        self.assertEqual(self.diagnostic(value)["successful_rps"], 36)

    async def test_unsafe_comparison_mapping_cannot_be_promoted_to_success(self):
        attach = self.surface("attach_product_truth")
        from unittest.mock import patch
        value, frozen = await self.retained()
        with patch.object(frozen, "compare", return_value={"safe": False, "tie_recall": 1., "strict_recall": 1.}):
            attach(value, frozen)
        self.assertEqual(sum(r["status"] == "ERROR" for r in value["records"]), 40)
        self.assertFalse(any("measurement" in r for r in value["records"]))

    async def test_empty_under_k_and_tied_truth_keep_the_existing_oracle_meaning(self):
        attach = self.surface("attach_product_truth")
        import hashlib, json
        from vector_baseline import product_path
        self.spec["queries"][0]["limit"] = 10
        self.expected = hashlib.sha256(product_path.canonical(self.spec)).hexdigest()
        value, frozen = await self.retained()
        row = copy.deepcopy(value["records"][0])
        body = json.loads(row["response"]["body"])
        body.update(query="empty-basis", results=[], state="ready_empty")
        row["query_id"] = row["response"]["query_id"] = "empty"
        row["response"]["body"] = json.dumps(body)
        row["response"]["response_sha256"] = hashlib.sha256(row["response"]["body"].encode()).hexdigest()
        value["records"][1] = row
        attach(value, frozen)
        self.assertEqual(value["records"][0]["measurement"]["expected_count"], 3)
        self.assertEqual(value["records"][0]["measurement"]["tie_recall"], 1.)
        empty = value["records"][1]["measurement"]
        self.assertTrue(empty["safe"])
        self.assertIsNone(empty["tie_recall"])
        self.assertEqual(empty["expected_count"], 0)

    async def test_offline_join_never_repairs_missing_or_duplicate_offer_inventories(self):
        attach = self.surface("attach_product_truth")
        value, frozen = await self.retained()
        for kind in ("missing", "duplicate"):
            with self.subTest(kind=kind):
                broken = copy.deepcopy(value)
                if kind == "missing": broken["records"].pop()
                else: broken["records"][-1]["offer_id"] = 0
                attach(broken, frozen)
                self.assertEqual(self.diagnostic(broken)["verdict"], "INVALID_HARNESS")

    async def run_source(self, **options):
        run = self.surface("run_product")
        import hashlib
        from vector_baseline import product_path, load
        self.spec["splits"] = {"tuning": ["empty"], "held_out": ["q1"]}
        self.expected = hashlib.sha256(product_path.canonical(self.spec)).hexdigest()
        async def observe(): return {"scope": "SOURCE_FIXTURE_ONLY"}
        return await run(self.root, self.spec, self.expected,
            source_root=Path(__file__).resolve().parents[2], read_port=self.read,
            scope_for=lambda q: dict(type="http", path="/search", headers=[(b"authorization", b"Bearer synthetic")]),
            config=options.pop("config", load.RunConfig(duration_seconds=.05, warmup_seconds=.025)),
            latency_budget_ms=100, observer=observe, **options)

    async def test_source_driver_freezes_once_before_open_and_compares_only_after_all_clients_close(self):
        self.surface("run_product")
        from unittest.mock import patch
        from vector_baseline import product_path, oracle, corpus
        events, clients = [], []
        original_prepare, original_client = product_path.prepare_input, product_path.client
        original_compare, original_rank = product_path.PreparedInput.compare, oracle.rank
        def prepare(*args, **kwargs):
            events.append("prepare")
            return original_prepare(*args, **kwargs)
        def factory(*args, **kwargs):
            events.append("construct")
            c = original_client(*args, **kwargs); clients.append(c); return c
        def rank(*args, **kwargs):
            self.assertFalse(clients, "truth must precede even the first client")
            return original_rank(*args, **kwargs)
        def compare(frozen, qid, response):
            self.assertEqual(len(clients), 8)
            self.assertTrue(all(c._prepared is None for c in clients), "offline join follows cleanup")
            events.append("compare")
            return original_compare(frozen, qid, response)
        with patch.object(product_path, "prepare_input", prepare), patch.object(product_path, "client", factory), \
             patch.object(product_path.PreparedInput, "compare", compare), patch.object(oracle, "rank", rank):
            result = await self.run_source()
        self.assertEqual(events.count("prepare"), 1)
        self.assertEqual(events[:2], ["prepare", "construct"])
        self.assertEqual(events.count("compare"), 3)
        self.assertEqual(len({id(c.frozen) for c in clients}), 1)
        self.assertEqual(result["qualification"], "C11_SOURCE_FIXTURE_DIAGNOSTIC")
        self.assertFalse(result["api_qualified"])
        self.assertFalse(result["native_qualified"])
        self.assertFalse(result["gtm_qualified"])
        self.assertEqual(result["engine_decision"], "UNDECIDED")
        self.assertEqual(result["sustained_all_miss"], "NOT_RUN")
        self.assertEqual({(c["os_cache"], c["query_cache"], c["status"]) for c in result["cells"]},
                         {(os, query, "NOT_RUN") for os in ("warm", "cold") for query in ("prefilled", "empty")})
        preparation = result["run"]["preparation"]
        self.assertEqual(len(preparation), 8)
        self.assertTrue(all(p["status"] == "NOT_RUN" for p in preparation))
        self.assertTrue(all(r["measurement"]["safe"] for r in result["run"]["records"]))
        self.assertEqual([r["response"]["cache_hit"] for r in result["run"]["records"]], [True, True])
        self.assertEqual(self.provider_calls, 1)
        self.assertNotIn("measurement", result["run"]["records"][0]["response"])
        self.assertEqual(result["provider_embed_calls_are_http_calls"], False)

    async def test_source_driver_keeps_warmup_failures_and_refusal_bound_to_that_inventory(self):
        self.surface("run_product")
        self.revoked = True
        result = await self.run_source()
        self.assertEqual(result["diagnostic"]["warmup_errors"], 1)
        self.assertTrue(all(p["warmup_errors"] == 1 for p in result["run"]["preparation"]))
        self.assertEqual(result["diagnostic"]["errors_or_missed"], 2)
        self.assertEqual(result["diagnostic"]["verdict"], "FAIL")
        self.assertEqual(self.provider_calls, 0)
        self.assertNotIn("synthetic-private-auth", str(result))

    async def test_invalid_input_and_config_refuse_before_client_or_provider_effects(self):
        run = self.surface("run_product")
        from vector_baseline import load, product_path
        from unittest.mock import patch
        kwargs = dict(source_root=Path(__file__).resolve().parents[2], read_port=self.read, scope_for=lambda q: {})
        with patch.object(product_path, "client", side_effect=AssertionError("must not create client")):
            with self.assertRaises(product_path.Refusal):
                await run(self.root, self.spec, "0" * 64, **kwargs)
            with patch.object(product_path, "prepare_input", side_effect=AssertionError("invalid config precedes input")):
                with self.assertRaises(ValueError):
                    await run(self.root, self.spec, self.expected, config=load.RunConfig(duration_seconds=0), **kwargs)
                with self.assertRaises(ValueError):
                    await run(self.root, self.spec, self.expected, latency_budget_ms=float("nan"), **kwargs)
        self.assertEqual(self.provider_calls, 0)

    async def test_ready_boolean_cannot_promote_the_unavailable_s4a_preparation(self):
        self.surface("run_product")
        from vector_baseline import load, product_path
        from unittest.mock import patch
        closed = []
        actual = product_path.ProductClient.aclose
        async def close(client):
            await actual(client); closed.append(client)
        async def forged(*args): return {"status": "READY", "native_qualified": True}
        with patch.object(product_path.ProductClient, "prepare_measured", forged), \
             patch.object(product_path.ProductClient, "aclose", close):
            with self.assertRaises(product_path.Refusal):
                await self.run_source(config=load.RunConfig(duration_seconds=.025, warmup_seconds=0))
        self.assertEqual(len(closed), 8)
        self.assertEqual(self.provider_calls, 0)

    async def test_preparation_digest_binds_current_input_truth_config_and_exact_warmup(self):
        self.surface("run_product")
        from vector_baseline import load, product_path
        from unittest.mock import patch
        actual = product_path.ProductClient.prepare_measured
        for key in ("input_sha256", "truth_sha256", "preparation_sha256", "warmup_errors"):
            with self.subTest(key=key):
                async def changed(client, warmup, config):
                    value = await actual(client, warmup, config)
                    value[key] = "changed"
                    return value
                with patch.object(product_path.ProductClient, "prepare_measured", changed):
                    with self.assertRaises(product_path.Refusal):
                        await self.run_source(config=load.RunConfig(duration_seconds=.025, warmup_seconds=0))
        self.assertEqual(self.provider_calls, 0)
