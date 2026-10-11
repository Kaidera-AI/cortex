"""Literal B01 faults on temporary source copies, with shared BODY attribution."""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

NEXT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(NEXT / "tests"))
from test_receipts import classify, report

TEST = "test_b01_product_path.ProductPathTests."
MUTATIONS = [
    ("omit-cache-provider", 'cached = await cache.get(q["subject"], identity, q["text"])',
     'cached = __import__("cortex_core.embeddings.query_cache", fromlist=["CachedEmbedding"]).CachedEmbedding(tuple(q["vector"]), False)',
     "test_actual_calls_cold_warm_and_independent_tiny_truth"),
    ("input-binding-ignored", "if hashlib.sha256(canonical(spec)).hexdigest() != expected:", "if False:",
     "test_changed_input_mapping_refused_before_any_port"),
    ("model-binding-ignored", 'raise Refusal("identity_mismatch")', "pass",
     "test_wrong_identity_refused_before_ports"),
    ("split-overlap-ignored", 'raise Refusal("split_overlap")', "pass",
     "test_split_overlap_refused_before_ports"),
    ("current-core-scope-ignored", "(current.tenant_id, current.project_id) != expected_scope",
     "(current.tenant_id, current.project_id) != (current.tenant_id, current.project_id)",
     "test_current_scope_is_compared_with_frozen_mapping"),
    ("provider-vector-binding-inverted", 'np.asarray(cached.vector, dtype="<f4").tobytes() != np.asarray(q["vector"], dtype="<f4").tobytes()',
     'np.asarray(cached.vector, dtype="<f4").tobytes() == np.asarray(q["vector"], dtype="<f4").tobytes()',
     "test_unbound_provider_vector_has_no_recall_credit"),
    ("hit-kind-revision-ignored", 'return {"status": "RESULT_REFUSED", "reason": "hit_source_mismatch"}', "pass",
     "test_unknown_foreign_duplicate_kind_and_revision_hits_refuse"),
    ("duplicate-hit-inventory-ignored", 'if not measured["safe"]:', "if False:",
     "test_unknown_foreign_duplicate_kind_and_revision_hits_refuse"),
    ("response-identity-ignored", "if result.freshness.identity != identity.key:", "if False:",
     "test_wrong_freshness_identity_has_no_credit"),
    ("unavailable-counted-success", 'if row["status"] != "MEASURED":', "if False:",
     "test_unavailable_results_are_charged_as_load_errors"),
    ("offer-dropped-from-receipt", '"load": copy.deepcopy(run)',
     '"load": {**copy.deepcopy(run), "records": copy.deepcopy(run["records"])[:-1]}',
     "test_fixed_offers_and_receipt_preserve_errors_and_no_gtm_credit"),
    ("synthetic-gtm-credit", '"gtm_qualified": False', '"gtm_qualified": True',
     "test_fixed_offers_and_receipt_preserve_errors_and_no_gtm_credit"),
]

CHILD = r'''
import hashlib, importlib, json, pathlib, sys, unittest
from test_receipts import AssertionResult, MARKER
target = sys.argv[1]
suite = unittest.defaultTestLoader.loadTestsFromName(target)
result = unittest.TextTestRunner(verbosity=2,resultclass=AssertionResult).run(suite)
module = importlib.import_module("vector_baseline.product_path")
path = pathlib.Path(module.__file__).resolve()
print(MARKER + json.dumps({"tests_run":result.testsRun,"failures":result.assertions,
 "errors":[{"id":test.id(),"traceback":trace} for test,trace in result.errors],
 "skipped":len(result.skipped),"executed_source":{"path":str(path),
 "sha256":hashlib.sha256(path.read_bytes()).hexdigest()}}),flush=True)
sys.exit(0 if result.wasSuccessful() else 1)
'''


def sha(data):
    return hashlib.sha256(data).hexdigest()


def dependency_roots():
    """Propagate only already imported, explicitly named public dependencies."""
    names = ("numpy", "httpx", "httpcore", "anyio", "certifi", "idna", "h11", "asyncpg")
    roots = []
    for name in names:
        path = str(Path(importlib.import_module(name).__file__).resolve().parent.parent)
        if path not in roots: roots.append(path)
    return roots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--real-pg", action="store_true", help="Use the admitted existing owned lifecycle")
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=False)
    if args.real_pg:
        return native(args.destination)
    source = NEXT / "src/vector_baseline/product_path.py"
    original = source.read_bytes()
    frozen = (NEXT / "tests/benchmarks/test_b01_product_path.py").read_bytes()
    rows = []
    with tempfile.TemporaryDirectory(prefix="b01-literal-source-") as directory:
        stage = Path(directory) / "src"
        shutil.copytree(NEXT / "src", stage)
        candidate = stage / "vector_baseline/product_path.py"
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR") if key in os.environ}
        env.update(PYTHONPATH=os.pathsep.join([str(stage), str(NEXT / "tests"),
                   str(NEXT / "tests/benchmarks"), *dependency_roots()]), PYTHONDONTWRITEBYTECODE="1")

        def execute(name, data, target):
            candidate.write_bytes(data)
            flags = ["-S"] if sys.flags.no_site else []
            process = subprocess.run([sys.executable, *flags, "-c", CHILD, target], env=env,
                                     capture_output=True, text=True, timeout=60)
            (args.destination / (name + ".stdout")).write_text(process.stdout)
            (args.destination / (name + ".stderr")).write_text(process.stderr)
            value = report(process)
            bound = bool(value and value.get("skipped") == 0
                and value["executed_source"]["sha256"] == sha(data)
                and Path(value["executed_source"]["path"]).resolve() == candidate.resolve())
            return process, value, bound

        baseline, value, bound = execute("baseline", original, TEST[:-1])
        if not bound or baseline.returncode != 0: raise RuntimeError("Baseline is not qualified")
        for name, before, after, method in MUTATIONS:
            text = original.decode()
            if text.count(before) != 1: raise RuntimeError("Literal source point is not unique: " + name)
            mutated = text.replace(before, after).encode()
            process, value, bound = execute(name, mutated, TEST + method)
            verdict = classify(process, [TEST + method]) if bound else "inconclusive"
            rows.append({"name": name, "target": TEST + method, "source_sha256": sha(mutated),
                "source_bound": bound, "exit": process.returncode, "verdict": verdict,
                "tests": value["tests_run"] if value else None,
                "body_failures": len(value["failures"]) if value else None,
                "errors": len(value["errors"]) if value else None})
        restored, value, bound = execute("restored", original, TEST[:-1])
        if not bound or restored.returncode != 0: raise RuntimeError("Restoration is not qualified")
    if source.read_bytes() != original or (NEXT / "tests/benchmarks/test_b01_product_path.py").read_bytes() != frozen:
        raise RuntimeError("Original source/test bytes changed")
    receipt = {"scope": "UNIT_PORTS_WITH_REAL_HOSTED_MOCK_TRANSPORT", "source_sha256": sha(original),
               "frozen_test_sha256": sha(frozen), "native": "NOT_RUN", "mutations": rows,
               "restoration": "exact source/test bytes; unchanged full suite GREEN"}
    (args.destination / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"tests": value["tests_run"], "mutations": len(rows),
          "killed": sum(r["verdict"] == "killed" for r in rows),
          "inconclusive": sum(r["verdict"] == "inconclusive" for r in rows)}, indent=2))
    return 0 if all(row["verdict"] == "killed" for row in rows) else 1


def native(destination):
    """Gate before any engine access. Product fixture and inherited proof share
    the existing owned lifecycle; no custom allocator or native acceptance claim.
    """
    manifest = NEXT / "tests/module_consumer/test_manifest.py"
    tracked = subprocess.run(["git", "cat-file", "-e", "HEAD:next/tests/module_consumer/test_manifest.py"],
                             cwd=NEXT, capture_output=True)
    if not manifest.is_file() or tracked.returncode:
        row = {"status": "BLOCKED", "reason": "inherited_manifest_not_integrated",
               "native_resources_created": 0, "tests_run": 0, "native_qualified": False}
        (destination / "native-receipt.json").write_text(json.dumps(row, indent=2) + "\n")
        print(json.dumps(row, indent=2))
        return 2
    # All imports/engine reads follow the integrated-head gate. No external DSN
    # is accepted; this is the already reviewed owned lifecycle and image pin.
    from vector_baseline import postgres, qdrant
    import psycopg
    from psycopg import sql
    import core_db_fixture
    import test_b01_product_path
    from test_receipts import AssertionResult
    import unittest
    from urllib.parse import quote

    profile = {name: importlib.import_module(name).__version__ for name in ("numpy", "httpx", "asyncpg", "psycopg")}
    required = {"numpy": "2.3.4", "httpx": "0.28.1", "asyncpg": "0.31.0", "psycopg": "3.2.9"}
    if not (3, 12) <= sys.version_info[:2] < (3, 14) or profile != required:
        raise RuntimeError("Declared existing test profile required; no installation fallback")
    admission = qdrant.preflight()
    if admission["free_percent"] < 35:
        raise RuntimeError("Fresh host35percent admission refused")
    info = json.loads(postgres.podman(["info", "--format", "json"]))
    host = info.get("host", {})
    total, free = host.get("memTotal"), host.get("memFree")
    if (type(total) is not int or type(free) is not int or total <= 0
            or not 0 <= free <= total or free * 100 < 35 * total):
        raise RuntimeError("Fresh engine35percent memory admission refused")
    postgres.podman(["image", "exists", postgres.IMAGE])

    def cached_runner(arguments, *, input=None):
        if arguments and arguments[0] == "create":
            arguments = ["create", "--pull=never", *arguments[1:]]
        return postgres.podman(arguments, input=input)

    stack = postgres.DisposablePostgres(runner=cached_runner)
    names = ("TEST_DATABASE_URL", "B01_OWNED_SEARCH_DSN")
    previous = {name: os.environ.get(name) for name in names}
    receipt = {"status": "RUNNING", "scope": "SOURCE_REAL_PG_COMPONENTS", "profile": profile,
               "host_admission": admission, "engine_free_percent": free * 100 // total,
               "native_qualified": False, "gtm_qualified": False}
    result = None
    try:
        with stack:
            # Existing fixture roles, in our new private DB only. The owner fixture
            # subsequently grants its original request membership and privileges.
            for role in (core_db_fixture.API, "search_runtime"):
                if stack.connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                    raise RuntimeError("Unexpected preexisting fixture role")
                stack.connection.execute(sql.SQL("""CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER
                    NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}""").format(
                    sql.Identifier(role), sql.Literal(stack.password)))
            connection = stack.connection.info
            base = f"postgresql://postgres@127.0.0.1:{connection.port}/postgres?password={quote(stack.password, safe='')}"
            os.environ["TEST_DATABASE_URL"] = base
            os.environ["B01_OWNED_SEARCH_DSN"] = base.replace("postgres@", "search_runtime@")
            sys.path.insert(0, str(NEXT / "tests/module_consumer"))
            inherited = unittest.defaultTestLoader.loadTestsFromName("test_manifest.Manifest")
            inherited_result = unittest.TextTestRunner(verbosity=2, resultclass=AssertionResult).run(inherited)
            receipt["inherited_manifest"] = {"tests": inherited_result.testsRun,
                "body_failures": inherited_result.assertions,
                "errors": len(inherited_result.errors), "skips": len(inherited_result.skipped),
                "source_sha256": sha(manifest.read_bytes())}
            if not inherited_result.wasSuccessful() or inherited_result.skipped:
                raise RuntimeError("Exact-head inherited manifest gate refused")
            result = unittest.TextTestRunner(verbosity=2, resultclass=AssertionResult).run(test_b01_product_path.native_suite())
            receipt["components"] = {"tests": result.testsRun, "body_failures": result.assertions,
                "errors": len(result.errors), "skips": len(result.skipped)}
            receipt["binding"] = stack.binding
            receipt["status"] = "SOURCE_FIXTURE_GREEN" if result.wasSuccessful() and not result.skipped else "RED"
    except BaseException as error:
        receipt.update(status="BLOCKED" if result is None else "RED", failure_class=type(error).__name__)
        raise
    finally:
        for name, value in previous.items():
            if value is None: os.environ.pop(name, None)
            else: os.environ[name] = value
        receipt["cleanup"] = {"complete": stack.cleanup_verified,
            "credential_discarded": stack.password is None, "lock_released": stack.lock is None,
            "remaining_pending": sorted(stack.pending), "remaining_owned": sorted(stack.owned)}
        (destination / "native-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return 0 if result and result.wasSuccessful() and not result.skipped and stack.cleanup_verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
