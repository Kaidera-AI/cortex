"""Fail-capable offline verification of the bounded PR35 repair packet."""
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assertions(data):
    tree = ast.parse(data)
    return [ast.dump(n, include_attributes=False) for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr.startswith("assert")]


def suite(label, tests, *, failures=0, errors=0, skipped=0):
    text = (HERE / f"{label}.stderr.txt").read_text()
    exit_code = int((HERE / f"{label}.exit").read_text())
    assert re.search(rf"Ran {tests} tests? in ", text), label
    if failures or errors:
        assert exit_code == 1 and "FAILED (" in text, label
        assert len(re.findall(r"^FAIL: ", text, re.M)) == failures, label
        assert len(re.findall(r"^ERROR: ", text, re.M)) == errors, label
    else:
        assert exit_code == 0 and re.search(r"^OK(?: \(.*\))?$", text, re.M), label
    assert len(re.findall(r"\.\.\. skipped ", text)) == skipped, label


def main():
    evidence = json.loads((HERE / "evidence.json").read_text())
    for name, digest in evidence["source_sha256"].items():
        assert sha(ROOT / name) == digest, f"source drift: {name}"
    for name, digest in evidence["receipts_sha256"].items():
        assert sha(HERE / name) == digest, f"receipt drift: {name}"
    integrity = json.loads((HERE / "test-integrity.json").read_text())
    for name, digest in integrity["new_RED_test_files_exact"].items():
        assert sha(ROOT / "next/tests/benchmarks" / name) == digest, name
    for name, expected in evidence["old_assertion_ast_sha256"].items():
        data = (ROOT / "next/tests/benchmarks" / name).read_text()
        nodes = assertions(data)
        assert len(nodes) == expected["count"], name
        assert hashlib.sha256(json.dumps(nodes).encode()).hexdigest() == expected["sha256"], name
    suite("red-six", 6, failures=6)
    suite("lifecycle-red", 2, failures=2)
    suite("row-red", 2, failures=9)
    suite("red", 13, failures=14, errors=1, skipped=1)
    suite("collision-fault-seed-red", 1, failures=3)
    suite("restored-native", 41)
    suite("final-b01-native", 41)
    suite("final-contracts", 33)
    suite("final-shared", 20)
    suite("handoff-b01-native", 41)
    suite("handoff-contracts", 33)
    suite("handoff-shared", 20)
    for label in ("final-b01-native", "final-contracts", "final-shared",
                  "handoff-b01-native", "handoff-contracts", "handoff-shared"):
        meta = json.loads((HERE / f"{label}.json").read_text())
        expected = evidence["handoff_refresh"] if label.startswith("handoff-") else evidence
        assert meta["head"] == expected["tested_source_commit"], label
        assert meta["target_main"] == expected["target_main"], label
        command = meta["command"]
        assert (ROOT / command[command.index("-s") + 1]).is_dir(), label
        for stream in ("stdout", "stderr"):
            assert sha(HERE / f"{label}.{stream}.txt") == meta[f"{stream}_sha256"], label
    for label in ("final-b01-native", "handoff-b01-native"):
        probes = [json.loads(line.split("=", 1)[1]) for line in
                  (HERE / f"{label}.stdout.txt").read_text().splitlines()
                  if line.startswith("B01_CREATE_FAILURE_PROBE=")]
        assert {(p["kind"], p["error"]) for p in probes} == {
            (kind, error) for kind in ("volume", "secret", "container")
            for error in ("TimeoutExpired", "RuntimeError")}
        assert len(probes) == 6
        assert all(all(p[key] is True for key in
                       ("removed", "credential_discarded", "lock_released")) for p in probes)
    suite("mutations/baseline", 41, skipped=2)
    mutations = json.loads((HERE / "mutations/receipt.json").read_text())
    assert len(mutations) == 12 and len({m["mutation"] for m in mutations}) == 12
    for m in mutations:
        assert m["exit"] == 1 and m["killed"] is True, m["mutation"]
        assert sha(ROOT / "next/src/vector_baseline" / m["source"]) == m["source_sha256"]
        lines = (HERE / "mutations" / f'{m["mutation"]}.stdout.txt').read_text().splitlines()
        markers = [line.split("=", 1)[1] for line in lines if line.startswith("CORTEX_TEST_RESULT=")]
        assert len(markers) == 1, m["mutation"]
        result = json.loads(markers[0])
        assert result["tests_run"] == 1 and result["failures"] and not result["errors"], m["mutation"]
        for f in result["failures"]:
            assert f["id"].split(" (", 1)[0] == m["expected_test"], m["mutation"]
            assert f["phase"] == "test" and f["is_assertion"] is True, m["mutation"]
            assert f["exception_type"] == "builtins.AssertionError", m["mutation"]
    assert int((HERE / "mutations.exit").read_text()) == 0
    assert int((HERE / "lint.exit").read_text()) == 0
    assert int((HERE / "command-check-red.exit").read_text()) == 1
    assert "missing unittest discovery start directory" in (HERE / "command-check-red.stderr.txt").read_text()
    assert int((HERE / "command-check-green.exit").read_text()) == 0
    assert int((HERE / "recipe-order-red.exit").read_text()) == 1
    assert "before any mutation producer" in (HERE / "recipe-order-red.stderr.txt").read_text()
    assert int((HERE / "recipe-order-green.exit").read_text()) == 0
    recipe_red = json.loads((HERE / "recipe-order-red.json").read_text())
    recipe_green = json.loads((HERE / "recipe-order-green.json").read_text())
    assert sha(HERE / "verify-recipe-order.py") == recipe_red["check_sha256"] == recipe_green["check_sha256"]
    assert sha(ROOT / "next/benchmarks/vector_baseline/README.md") == recipe_green["readme_sha256"]
    subprocess.run([sys.executable, str(HERE / "verify-recipe-order.py")],
                   capture_output=True, text=True, check=True)
    print(json.dumps({"result": "PASS", "tested_source_commit": evidence["tested_source_commit"],
                      "target_main": evidence["target_main"], "native_tests": 41,
                      "handoff_refresh": evidence["handoff_refresh"],
                      "real_create_failure_cases": 6, "contract_tests": 33,
                      "shared_tests": 20, "actual_body_mutants_killed": 12,
                      "new_RED_tests_unchanged": True, "old_assertion_nodes_unchanged": 95}))


if __name__ == "__main__":
    main()
