"""C11a source-copy faults; only expected test-body assertions count."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "next/tests/c11a"
RUNNER = ROOT / "next/tests/test_receipts.py"
API = ROOT / "next/src/cortex_core/api/c11a.py"
DISPATCH = ROOT / "next/src/cortex_core/cli/dispatch.py"
PG_RUNNER = TESTS / "run_pg.py"
CASES = [
    ("core_down", API, "if core is not True:", "if False:",
     "test_gateway.GatewayTests.test_core_down_fails_closed_even_for_unbound_or_unknown_nonhealth"),
    ("unlanded", API, "if name in UNLANDED:", "if False:",
     "test_gateway.GatewayTests.test_unlanded_addons_are_unavailable_even_if_source_claims_ready"),
    ("model", API, 'or not value["model_id"]', "or False",
     "test_gateway.GatewayTests.test_pg_ready_requires_model_generation_freshness_and_recheck"),
    ("state", API, 'value.get("state") != "ready"', "False",
     "test_gateway.GatewayTests.test_pg_ready_requires_model_generation_freshness_and_recheck"),
    ("freshness", API, 'value["freshness"].get("complete") is not True', "False",
     "test_gateway.GatewayTests.test_pg_ready_requires_model_generation_freshness_and_recheck"),
    ("permission", API, "if allowed is not True:", "if False:",
     "test_gateway.GatewayTests.test_pg_ready_requires_model_generation_freshness_and_recheck"),
    ("commit", API, "value.committed is not True", "False",
     "test_gateway.GatewayTests.test_write_waits_for_committed_c05_receipt_and_idempotency_key"),
    ("request_key", API, "value.request_key != request_key", "False",
     "test_gateway.GatewayTests.test_write_waits_for_committed_c05_receipt_and_idempotency_key"),
    ("effect_binding", API, 'row["effect"] != effect', "False",
     "test_gateway.GatewayTests.test_manifest_route_binding_refuses_method_path_drift"),
    ("unbound_route", API, '_packet("capability_unavailable",\n                                  reason="adapter_unbound")',
     '_packet("not_found",\n                                  reason="adapter_unbound")',
     "test_gateway.GatewayTests.test_unbound_consumer_route_is_typed_unavailable_not_empty_success"),
    ("retired_sql", API, 'return await _respond(send, 410, _packet("retired_route"',
     'return await _respond(send, 200, _packet("retired_route"',
     "test_gateway.GatewayTests.test_unbound_consumer_route_is_typed_unavailable_not_empty_success"),
    ("explicit_endpoint", DISPATCH, 'and "--endpoint" not in argv', 'and True',
     "test_dispatch.DispatchTests.test_explicit_endpoint_wins_and_missing_default_refuses"),
    ("configured_default", DISPATCH,
     '["status", "--endpoint", environ["CORTEX_API_URL"], *argv[1:]]',
     '["status", "--endpoint", "http://127.0.0.1:1", *argv[1:]]',
     "test_dispatch.DispatchTests.test_status_uses_explicit_configured_loopback_default"),
    ("memory_gate", PG_RUNNER, 'int(match.group(1)) < 35', 'int(match.group(1)) < 0',
     "test_run_pg.RunnerTests.test_memory_below_35_percent_refuses_before_podman"),
    ("resource_limit", PG_RUNNER, '"--memory", "1g"', '"--memory", "2g"',
     "test_run_pg.RunnerTests.test_owned_stack_has_bounded_resources_and_cleanup_on_runner_failure"),
    ("cleanup", PG_RUNNER, 'command("podman", "rm", "-f", "-v", name, check=False)', 'pass',
     "test_run_pg.RunnerTests.test_owned_stack_has_bounded_resources_and_cleanup_on_runner_failure"),
]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run():
    originals = {source: source.read_bytes() for _, source, _, _, _ in CASES}
    before_hashes = {str(path.relative_to(ROOT)): sha(path) for path in originals}
    rows = []
    for name, source, before, after, expected in CASES:
        body = originals[source].decode()
        assert body.count(before) == 1, (name, body.count(before))
        with tempfile.TemporaryDirectory(prefix="cox-c11a-fault-") as temporary:
            scratch = Path(temporary)
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            if source == PG_RUNNER:
                mutated = scratch / "run_pg.py"
                environment["C11A_RUNNER_PATH"] = str(mutated)
                environment["PYTHONPATH"] = str(ROOT / "next/src")
            else:
                relative = source.relative_to(ROOT / "next/src")
                mutated = scratch / "next/src" / relative
                environment["PYTHONPATH"] = os.pathsep.join((str(scratch / "next/src"),
                                                               str(ROOT / "next/src")))
                if source == API:
                    contracts = scratch / "next/contracts"
                    contracts.mkdir(parents=True)
                    for filename in ("route-matrix.md", "c11a-consumer-routes.json"):
                        shutil.copy2(ROOT / "next/contracts" / filename, contracts / filename)
            mutated.parent.mkdir(parents=True, exist_ok=True)
            mutated.write_text(body.replace(before, after, 1))
            result = subprocess.run([sys.executable, str(RUNNER), str(TESTS)],
                                    capture_output=True, text=True, env=environment)
            lines = [json.loads(line.split("=", 1)[1]) for line in result.stdout.splitlines()
                     if line.startswith("CORTEX_TEST_RESULT=")]
            receipt = lines[0] if len(lines) == 1 else None
            failures = [] if receipt is None else receipt["failures"]
            killed = (result.returncode == 1 and receipt is not None
                      and receipt["tests_run"] == 12 and not receipt["errors"]
                      and expected in {row["id"].split(" (")[0] for row in failures}
                      and all(row["phase"] == "test" and row["is_assertion"] is True
                              and row["phase_attribution"] == "traceback+active-unittest-v2"
                              for row in failures))
            row = {"name": name, "source": str(source.relative_to(ROOT)),
                   "expected_test": expected, "status": "killed" if killed else "inconclusive",
                   "mutated_sha256": sha(mutated), "exit_code": result.returncode,
                   "receipt": receipt, "stdout": result.stdout, "stderr": result.stderr}
            rows.append(row)
            print(json.dumps({"name": name, "status": row["status"],
                              "failures": len(failures),
                              "errors": len(receipt["errors"]) if receipt else None}), flush=True)
    assert before_hashes == {str(path.relative_to(ROOT)): sha(path) for path in originals}
    print(json.dumps({"source_sha256": before_hashes, "mutants": len(rows),
                      "killed": sum(row["status"] == "killed" for row in rows),
                      "inconclusive": [row["name"] for row in rows if row["status"] != "killed"],
                      "rows": rows}), flush=True)
    if any(row["status"] != "killed" for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    run()
