"""Literal cache-return faults with the existing shared unittest BODY classifier."""

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "next/src/cortex_core/embeddings/query_cache.py"
PRIMARY = "next/tests/integration/test_query_cache_float32_roundtrip.py"
COMPANION = "next/tests/integration/test_query_cache_float32_roundtrip_pg.py"
FROZEN = {
    PRIMARY: "7bb0524dfe96d73a870386fc7a677d7c56baec5f51a9ec3d2e74d82feb174196",
    COMPANION: "007796fac63c55c19d62ead1e5aac5a64a1f008688be127af5e7b4407bfdfcfa",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def check_frozen():
    assert all(digest((ROOT / path).read_bytes()) == value
               for path, value in FROZEN.items()), "frozen test bytes changed"


def child(real_pg, case, report_path):
    sys.setrecursionlimit(10000)  # Preserve unequal-vector diagnostic formatting.
    sys.path[:0] = [str(ROOT / "next/src"), str(ROOT / "next/tests"),
                   str(ROOT / "next/tests/integration")]
    import test_receipts
    import cortex_core.embeddings.query_cache as cache
    name = ("test_query_cache_float32_roundtrip_pg" if real_pg
            else "test_query_cache_float32_roundtrip")
    module = importlib.import_module(name)
    klass = (module.TestNativeCacheFloat32RoundTrip if real_pg
             else module.CacheFloat32RoundTripTests)
    suite = (unittest.TestSuite([klass(case)]) if case
             else unittest.defaultTestLoader.loadTestsFromTestCase(klass))
    assert Path(cache.__file__).resolve() == SOURCE.resolve()
    check_frozen()
    result = unittest.TextTestRunner(verbosity=2,
                                   resultclass=test_receipts.AssertionResult).run(suite)
    value = {
        "verification_kind": "REAL_PG_SYNTHETIC_AUTHORITY" if real_pg else "UNIT_PORTS",
        "tested_head": subprocess.check_output(["git", "rev-parse", "HEAD"],
                                                cwd=ROOT, text=True).strip(),
        "tests_run": result.testsRun,
        "failures": result.assertions,
        "errors": [{"id": test.id(), "traceback": tb} for test, tb in result.errors],
        "skipped": list(result.skipped),
        "exit": 0 if result.wasSuccessful() else 1,
        "imported_source": {"path": cache.__file__,
                            "sha256": digest(Path(cache.__file__).read_bytes())},
        "frozen_tests": FROZEN,
    }
    report_path.write_text(json.dumps(value, indent=2) + "\n")
    print(test_receipts.MARKER + json.dumps(value), flush=True)
    return value["exit"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--real-pg", action="store_true")
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--case")
    args = parser.parse_args()
    if args.real_pg and os.environ.get("NEMO_CACHE_ROUNDTRIP_OWNED_FIXTURE") != "1":
        raise RuntimeError("explicit owned disposable-PG fixture required")
    if args.child:
        return child(args.real_pg, args.case, args.directory)
    directory = args.directory.resolve(strict=True)
    check_frozen()
    sys.path.insert(0, str(ROOT / "next/tests"))
    import test_receipts

    def run(label, case=None):
        report = directory / (label + ".json")
        command = [sys.executable, "-B", str(Path(__file__).resolve()),
                   str(report), "--child"]
        if args.real_pg:
            command.append("--real-pg")
        if case:
            command.extend(["--case", case])
        allowed = ["PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL",
                   "TMPDIR", "PYTHONPATH"]
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if args.real_pg:
            for key in ("SEARCH_TEST_DSN", "NEMO_CACHE_ROUNDTRIP_OWNED_FIXTURE"):
                env[key] = os.environ[key]
            observations = directory / (label + "-observations")
            observations.mkdir()
            env["NEMO_CACHE_ROUNDTRIP_OBSERVATIONS"] = str(observations)
        result = subprocess.run(command, cwd=ROOT, env=env, text=True,
                                capture_output=True, timeout=120)
        (directory / (label + ".stdout")).write_text(result.stdout)
        (directory / (label + ".stderr")).write_text(result.stderr)
        return result, test_receipts.report(result)

    def baseline(label):
        result, value = run(label)
        assert value is not None and result.returncode == value["exit"] == 0
        assert value["tests_run"] == (2 if args.real_pg else 3)
        assert not value["failures"] and not value["errors"] and not value["skipped"]

    raw = SOURCE.read_bytes()
    text = raw.decode()
    warm = ('                vector = tuple(\n'
            '                    json.loads(vector_literal(json.loads(row["vector"]), 768))\n'
            '                )')
    recipes = [
        ("raw-cold-return", "return CachedEmbedding(vector, False)",
         "return CachedEmbedding(tuple(float(v) for v in provider_vector), False)",
         "test_native_cold_return_matches_publication_and_search_float32" if args.real_pg
         else "test_cold_provider_vector_is_returned_as_canonical_float32"),
        ("raw-warm-decoding", warm,
         '                vector = tuple(json.loads(row["vector"]))\n'
         '                vector_literal(vector, 768)',
         "test_native_warm_return_matches_publication_and_search_float32" if args.real_pg
         else "test_warm_vector_text_is_returned_as_canonical_float32"),
    ]
    rows = []
    try:
        baseline("clean")
        for label, before, after, case in recipes:
            assert text.count(before) == 1, label
            mutant = text.replace(before, after, 1).encode()
            SOURCE.write_bytes(mutant)
            result, value = run(label, case)
            prefix = ("test_query_cache_float32_roundtrip_pg.TestNativeCacheFloat32RoundTrip."
                      if args.real_pg else
                      "test_query_cache_float32_roundtrip.CacheFloat32RoundTripTests.")
            expected = prefix + case
            valid = (value is not None and test_receipts.classify(result, [expected]) == "killed"
                     and result.returncode == value["exit"] == 1
                     and value["tests_run"] == 1 and not value["errors"] and not value["skipped"]
                     and len(value["failures"]) == 1
                     and value["failures"][0]["id"] == expected
                     and value["failures"][0]["phase"] == "test"
                     and value["failures"][0]["exception_type"] == "builtins.AssertionError"
                     and value["imported_source"]["sha256"] == digest(mutant))
            classification = ("KILLED_BODY" if valid else "SURVIVED"
                              if result.returncode == 0 else "INCONCLUSIVE")
            rows.append({"label": label, "result": classification,
                         "verification_kind": "REAL_PG_SYNTHETIC_AUTHORITY" if args.real_pg else "UNIT_PORTS",
                         "source_path": str(SOURCE), "original_sha256": digest(raw),
                         "mutant_sha256": digest(mutant), "literal_before": before,
                         "literal_after": after, "expected_body": expected,
                         "exit": result.returncode, "frozen_tests": FROZEN})
            (directory / "mutants-progress.json").write_text(json.dumps(rows, indent=2) + "\n")
            SOURCE.write_bytes(raw)
            check_frozen()
            print(label, classification, flush=True)
            if not valid:
                raise RuntimeError("fault did not yield its expected BODY assertion: " + label)
        baseline("restored")
        (directory / "mutants.json").write_text(json.dumps(rows, indent=2) + "\n")
    finally:
        SOURCE.write_bytes(raw)
        check_frozen()
        assert digest(SOURCE.read_bytes()) == digest(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
