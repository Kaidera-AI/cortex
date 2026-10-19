"""Actual single-body mutations over temporary source copies; canonical admission."""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

NEXT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(NEXT / "tests"))
receipts = importlib.import_module("test_receipts")
classify, report = receipts.classify, receipts.report

MUTATIONS = [
    ("load.py", "completion-relative-schedule", "scheduled = origin + index / config.rps", "scheduled = clock.now()",
     "test_b02_load.LoadTests.test_fixed_schedule_and_eight_persistent_clients", 1),
    ("load.py", "queue-exclusive-latency", 'row["latency_seconds"] = row["completed_at"] - row["scheduled_at"]',
     'row["latency_seconds"] = row["completed_at"] - row["started_at"]',
     "test_b02_load.LoadTests.test_queue_wait_is_part_of_scheduled_latency", 1),
    ("load.py", "missed-arrivals-hidden", "if lag >= 1 / config.rps:", "if False:",
     "test_b02_load.LoadTests.test_scheduler_stall_records_missed_arrivals_without_retiming", 1),
    ("load.py", "fast-error-latency-omitted",
     'response_elapsed_seconds=max(0., clock.now() - row["scheduled_at"]),\n                               latency_seconds=max(config.deadline_seconds, clock.now() - row["scheduled_at"]))',
     'response_elapsed_seconds=max(0., clock.now() - row["scheduled_at"]),\n                               latency_seconds=max(0., clock.now() - row["scheduled_at"]))',
     "test_b02_load.LoadTests.test_fast_errors_get_deadline_or_later_latency_accounting", 1),
    ("load.py", "pending-open-untracked", 'clients.append(client)  # Track the pending connection before open\'s external effect.\n            await asyncio.wait_for(client.open(), config.deadline_seconds)',
     'await asyncio.wait_for(client.open(), config.deadline_seconds)\n            clients.append(client)',
     "test_b02_load.LoadTests.test_partial_open_side_effect_is_closed_before_error_propagates", 1),
    ("load.py", "cancelled-clients-not-closed", "for client in reversed(clients):", "for client in []:",
     "test_b02_load.LoadTests.test_caller_cancellation_closes_all_created_clients", 1),
    ("load.py", "observer-error-hidden", "observation_errors.append(type(error).__name__)", "pass",
     "test_b02_load.LoadTests.test_observation_failure_is_visible_and_cleaned", 1),
    ("load.py", "admission-after-effect", "    config.validate()", "    factory(0)\n    config.validate()",
     "test_b02_load.LoadTests.test_invalid_configuration_refuses_before_opening_clients", 1),
    ("report.py", "p95-off-by-one", "math.ceil(len(values) * percentile) - 1", "math.ceil(len(values) * percentile)",
     "test_b02_report.ReportTests.test_nearest_rank_p95_uses_exact_boundary", 1),
    ("report.py", "failure-latencies-excluded", 'latencies = [r["latency_seconds"] * 1000 for r in rows]',
     'latencies = [r["latency_seconds"] * 1000 for r in rows if r["status"] == "OK"]',
     "test_b02_report.ReportTests.test_failure_latencies_are_included_and_success_floor_is_independent", 1),
    ("report.py", "drain-denominator", "successful_rps = len(successes) / duration",
     'successful_rps = len(successes) / (run["measured_end"] - run["measured_start"])',
     "test_b02_report.ReportTests.test_throughput_uses_scheduled_interval_not_drain_duration", 1),
    ("report.py", "unsafe-hit-hidden", 'safe = all(isinstance(m, dict) and m.get("safe") is True for m in metrics)', "safe = True",
     "test_b02_report.ReportTests.test_high_mean_cannot_hide_a_forbidden_or_duplicate_hit", 1),
    ("report.py", "empty-recall-fiction", "mean = sum(means) / len(means) if means else None",
     "mean = sum(means) / len(means) if means else 1",
     "test_b02_report.ReportTests.test_empty_only_has_no_fictional_recall_pass", 1),
    ("report.py", "partial-inventory-admitted", '    latencies = [r["latency_seconds"] * 1000 for r in rows]',
     '    valid = True\n    latencies = [r["latency_seconds"] * 1000 for r in rows]',
     "test_b02_report.ReportTests.test_missing_or_duplicate_offer_and_partial_clients_refuse_accounting", 4),
    ("report.py", "warmup-failure-hidden", '              or any(r["status"] != "OK" for r in run["warmup_records"])\n', "",
     "test_b02_report.ReportTests.test_warmup_failure_is_visible_and_prevents_diagnostic_pass", 1),
    ("report.py", "premature-engine-selection", '"engine_decision": "UNDECIDED"', '"engine_decision": "POSTGRES_FIRST"',
     "test_b02_report.ReportTests.test_short_synthetic_run_cannot_select_engine_or_native_acceptance", 1),
    ("benchmark.py", "real-input-admitted", 'if manifest.get("dataset") != "synthetic" or manifest.get("remote", False) is not False:', "if False:",
     "test_b02_report.ReportTests.test_real_or_remote_inputs_refuse_before_resource_creation", 2),
    ("benchmark.py", "first-response-reused", 'row["response"]["ids"], "dense"', 'run["records"][0]["response"]["ids"], "dense"',
     "test_b02_report.ReportTests.test_results_bind_each_response_to_its_own_full_oracle", 1),
    ("benchmark.py", "unbounded-resource-observer",
     'stdout, _ = await asyncio.wait_for(process.communicate(), OBSERVATION_TIMEOUT_SECONDS)',
     'stdout, _ = await process.communicate()',
     "test_b02_load.LoadTests.test_owned_stats_timeout_kills_and_reaps_actual_child", 1),
    ("benchmark.py", "cancelled-stats-child-left-running", 'if process.returncode is None:', 'if False:',
     "test_b02_load.LoadTests.test_owned_stats_cancellation_kills_and_reaps_actual_child", 1),
    ("report.py", "omitted-heldout-query-hidden", 'mean is None or not coverage_complete', 'mean is None',
     "test_b02_report.ReportTests.test_omitted_heldout_query_is_explicit_not_run", 1),
    ("benchmark.py", "interruption-class-omitted", 'except BaseException as error:', 'except Exception as error:',
     "test_b02_report.ReportTests.test_interrupt_is_named_in_saved_fail_receipt_and_propagates", 1),
    ("benchmark.py", "blocking-resource-observer", 'target = await asyncio.to_thread(stack.owned_resource, "container")',
     'target = stack.owned_resource("container")',
     "test_b02_load.LoadTests.test_owned_resource_poll_does_not_block_the_arrival_loop", 1),
    ("load.py", "s4-preparation-omitted",
     'preparation = await before_measured(tuple(clients), copy.deepcopy(warmup), config)', 'preparation = None',
     "test_b02_offer_accounting.MeasuredPreparationTests.test_preparation_runs_after_drain_before_new_origin_with_all_clients_open", 1),
    ("load.py", "s4-warmup-custody-mutable",
     'before_measured(tuple(clients), copy.deepcopy(warmup), config)', 'before_measured(tuple(clients), warmup, config)',
     "test_b02_offer_accounting.MeasuredPreparationTests.test_preparation_receives_a_private_warmup_copy", 1),
    ("load.py", "s4-preparation-error-swallowed",
     '            preparation = await before_measured(tuple(clients), copy.deepcopy(warmup), config)',
     '            try:\n                preparation = await before_measured(tuple(clients), copy.deepcopy(warmup), config)\n            except Exception:\n                preparation = None',
     "test_b02_offer_accounting.MeasuredPreparationTests.test_preparation_failure_closes_clients_and_never_offers_measured_work", 1),
    ("benchmark.py", "s4-first-response-reused",
     'measurement = prepared.compare(row["query_id"], row["response"])',
     'measurement = prepared.compare(row["query_id"], run[phase][0]["response"])',
     "test_b02_report.S4bProductAccountingTests.test_each_retained_response_is_compared_offline_and_refusal_is_charged_without_retiming", 1),
    ("benchmark.py", "s4-warmup-oracle-omitted",
     'for phase in ("warmup_records", "records"):', 'for phase in ("records",):',
     "test_b02_report.S4bProductAccountingTests.test_warmup_comparison_failure_and_earlier_error_both_survive", 1),
    ("benchmark.py", "s4-earlier-errors-recompared",
     '            if row["status"] != "OK":\n                continue', '            if False:\n                continue',
     "test_b02_report.S4bProductAccountingTests.test_original_error_timeout_and_missed_rows_are_never_erased_or_recompared", 1),
    ("benchmark.py", "s4-unsafe-comparison-admitted",
     'if not isinstance(measurement, dict) or measurement.get("safe") is not True:', 'if False:',
     "test_b02_report.S4bProductAccountingTests.test_unsafe_comparison_mapping_cannot_be_promoted_to_success", 1),
    ("benchmark.py", "s4-refusal-penalty-omitted",
     'latency_seconds=max(run["config"]["deadline_seconds"], row["latency_seconds"]))',
     'latency_seconds=row["latency_seconds"])',
     "test_b02_report.S4bProductAccountingTests.test_each_response_query_input_truth_and_bytes_binding_is_enforced", 1),
    ("benchmark.py", "s4-comparison-rewrites-completion",
     '                row.update(status="ERROR", error_class="OfflineComparisonRefusal",',
     '                row["completed_at"] = row["scheduled_at"] + run["config"]["deadline_seconds"]\n                row.update(status="ERROR", error_class="OfflineComparisonRefusal",',
     "test_b02_report.S4bProductAccountingTests.test_each_retained_response_is_compared_offline_and_refusal_is_charged_without_retiming", 1),
    ("benchmark.py", "s4-measurement-binding-forged",
     'binding = {key: row["response"][key] for key in', 'binding = {key: prepared.input_sha256 for key in',
     "test_b02_report.S4bProductAccountingTests.test_each_retained_response_is_compared_offline_and_refusal_is_charged_without_retiming", 1),
    ("benchmark.py", "s4-truth-recomputed-per-client",
     'read_port=read_port, scope_for=scope_for, prepared=prepared)',
     'read_port=read_port, scope_for=scope_for, prepared=None)',
     "test_b02_report.S4bProductAccountingTests.test_source_driver_freezes_once_before_open_and_compares_only_after_all_clients_close", 1),
    ("benchmark.py", "s4-preparation-ready-admitted",
     'if (not isinstance(value, dict) or value != refusal\n                    or type(value.get("warmup_errors")) is not int):',
     'if False:',
     "test_b02_report.S4bProductAccountingTests.test_ready_boolean_cannot_promote_the_unavailable_s4a_preparation", 1),
    ("benchmark.py", "s4-preparation-binding-ignored",
     'if (not isinstance(value, dict) or value != refusal\n                    or type(value.get("warmup_errors")) is not int):',
     'if (not isinstance(value, dict) or any(value.get(k) != v for k,v in refusal.items() if k not in ("input_sha256", "truth_sha256", "preparation_sha256", "warmup_errors"))):',
     "test_b02_report.S4bProductAccountingTests.test_preparation_digest_binds_current_input_truth_config_and_exact_warmup", 5),
    ("benchmark.py", "s4-native-cells-promoted",
     'result["cells"] = [{"os_cache": os_cache, "query_cache": query_cache, "status": "NOT_RUN",',
     'result["cells"] = [{"os_cache": os_cache, "query_cache": query_cache, "status": "PASS",',
     "test_b02_report.S4bProductAccountingTests.test_source_driver_freezes_once_before_open_and_compares_only_after_all_clients_close", 1),
    ("benchmark.py", "s4-sustained-miss-promoted",
     'sustained_all_miss="NOT_RUN", provider_embed_calls_are_http_calls=False)',
     'sustained_all_miss="PASS", provider_embed_calls_are_http_calls=False)',
     "test_b02_report.S4bProductAccountingTests.test_source_driver_freezes_once_before_open_and_compares_only_after_all_clients_close", 1),
]

CHILD = r'''
import hashlib, importlib, json, os, pathlib, sys, unittest
from test_receipts import AssertionResult, MARKER
names = sys.argv[1:] or ["test_b02_load", "test_b02_report", "test_b02_offer_accounting"]
suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n) for n in names)
result = unittest.TextTestRunner(verbosity=2, resultclass=AssertionResult).run(suite)
sources = {}
package = importlib.import_module("vector_baseline")
for source in sorted(pathlib.Path(next(iter(package.__path__))).glob("*.py")):
    name = source.stem
    module = package if name == "__init__" else importlib.import_module("vector_baseline." + name)
    path = pathlib.Path(module.__file__).resolve()
    sources[name + ".py"] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
print(MARKER + json.dumps({"tests_run":result.testsRun, "failures":result.assertions,
                          "errors":[{"id":t.id(),"traceback":s} for t,s in result.errors],
                          "executed_sources":sources}), flush=True)
sys.exit(0 if result.wasSuccessful() else 1)
'''


def sha(data):
    return hashlib.sha256(data).hexdigest()


def source_custody(observed, expected, root):
    return (set(observed) == set(expected)
            and all(row["sha256"] == expected[name]
                    and Path(row["path"]).resolve().is_relative_to(Path(root).resolve())
                    for name, row in observed.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=False)
    source = NEXT / "src/vector_baseline"
    originals = {path.name: path.read_bytes() for path in source.glob("*.py")}
    tests = {str(path.relative_to(NEXT)): sha(path.read_bytes())
             for path in (NEXT / "tests/benchmarks").glob("test_b02_*.py")}
    recipe_hash = sha(Path(__file__).read_bytes())
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    results = []

    def execute(label, root, target=None, expected_source=None):
        path = os.pathsep.join([str(root), str(NEXT / "tests/benchmarks"), str(NEXT / "tests"), str(NEXT / "src")])
        child = subprocess.run([sys.executable, "-c", CHILD, *([target] if target else [])],
                               env={**env, "PYTHONPATH": path}, capture_output=True, text=True)
        for stream in ["stdout", "stderr"]:
            (args.destination / (label + "." + stream + ".txt")).write_text(getattr(child, stream))
        (args.destination / (label + ".exit")).write_text(str(child.returncode) + "\n")
        parsed = report(child)
        if parsed is None:
            raise RuntimeError("missing structured result: " + label)
        if not source_custody(parsed["executed_sources"], expected_source, root):
            raise RuntimeError("executed-source custody mismatch: " + label)
        return child, parsed

    original_hashes = {name: sha(raw) for name, raw in originals.items()}
    child, parsed = execute("baseline", NEXT / "src", expected_source=original_hashes)
    if child.returncode or parsed["failures"] or parsed["errors"] or parsed["tests_run"] != 60:
        raise RuntimeError("B02 mutation baseline not clean")
    for filename, label, before, after, target, bodies in MUTATIONS:
        if originals[filename].decode().count(before) != 1:
            raise RuntimeError("mutation anchor drift: " + label)
        with tempfile.TemporaryDirectory(prefix="b02-mutant-", dir=args.destination) as temporary:
            root = Path(temporary)
            package = root / "vector_baseline"
            package.mkdir()
            expected = {}
            for name, raw in originals.items():
                data = raw.decode().replace(before, after, 1).encode() if name == filename else raw
                (package / name).write_bytes(data)
                expected[name] = sha(data)
            child, parsed = execute(label, root, target, expected)
            failures = parsed["failures"]
            killed = (classify(child, {target}) == "killed" and parsed["tests_run"] == 1
                      and len(failures) == bodies and not parsed["errors"]
                      and {row["id"].split(" (")[0] for row in failures} == {target})
            row = {"label": label, "file": filename, "expected_test": target, "expected_body_failures": bodies,
                   "before": before, "after": after, "source_sha256": original_hashes[filename],
                   "mutant_sha256": expected[filename], "recipe_file_sha256": recipe_hash,
                   "exit": child.returncode, "killed": killed, "body_failures": len(failures),
                   "errors": len(parsed["errors"]), "tests_run": parsed["tests_run"],
                   "stdout_sha256": sha(child.stdout.encode()), "stderr_sha256": sha(child.stderr.encode()),
                   "executed_sources": parsed["executed_sources"]}
            results.append(row)
            (args.destination / "in-progress.json").write_text(json.dumps(results, indent=2) + "\n")
            if not killed:
                raise RuntimeError("mutation not an intended body kill: " + label)
        if {path.name: sha(path.read_bytes()) for path in source.glob("*.py")} != original_hashes:
            raise RuntimeError("original source drift")
    child, parsed = execute("restored", NEXT / "src", expected_source=original_hashes)
    if child.returncode or parsed["failures"] or parsed["errors"] or parsed["tests_run"] != 60:
        raise RuntimeError("restored source not clean")
    (args.destination / "mutations.json").write_text(json.dumps({"mutations": results, "tests": tests,
                                                                 "recipe_file_sha256": recipe_hash,
                                                                 "baseline_restored": 60,
                                                                 "source_hashes": original_hashes}, indent=2) + "\n")
    print(json.dumps({"mutants": len(results), "actual_body_kills": len(results), "baseline_restored": 60}))


if __name__ == "__main__":
    main()
