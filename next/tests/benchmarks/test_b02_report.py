"""Hand-computed accounting and admission negatives, frozen before source."""
import copy
import importlib
from pathlib import Path
import unittest


def fixture():
    rows = [{"offer_id": i, "client": i % 8, "query_id": "q", "status": "OK",
             "phase": "measured", "scheduled_offset_seconds": i / 40,
             "scheduled_at": 100 + i / 40, "started_at": 100 + i / 40,
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
            c = SimpleNamespace(path=root, manifest={"dataset": "synthetic"}, identity={})
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
