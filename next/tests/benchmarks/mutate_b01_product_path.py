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

S4_TEST = 'test_b01_product_path.S4ProductPathTests.'
S4_MUTATIONS = [
    ('s4-timed-oracle', "principal = await self._read.principal(scope)",
     "oracle.rank(self.frozen._corpus,q,modes=('dense',))\n            principal = await self._read.principal(scope)",
     S4_TEST+'test_s4_no_truth_or_corpus_scan_inside_request'),
    ('s4-bypass-c11', 'body = await self._read.search(principal,scope,None)',
     "body = {'query':q['text'],'results':[]}",
     S4_TEST+'test_s4_actual_c11_cache_qdrant_response_and_offline_truth'),
    ('s4-bypass-cache-provider', 'cached = await cache.get(*args)',
     "cached = __import__('cortex_core.embeddings.query_cache',fromlist=['CachedEmbedding']).CachedEmbedding(tuple(owner._query['vector']),False)",
     S4_TEST+'test_s4_actual_c11_cache_qdrant_response_and_offline_truth'),
    ('s4-provider-vector-unbound', "raise Refusal('provider_vector_unbound')", 'pass',
     S4_TEST+'test_s4_unbound_provider_vector_has_no_c11_result_credit'),
    ('s4-offer-substitution', "if canonical(query) != canonical(q): raise Refusal('offer_binding_changed')", 'if False: pass',
     S4_TEST+'test_s4_changed_offer_cannot_substitute_frozen_query'),
    ('s4-serializer-bypassed', 'await c11a._respond(send,200,body)',
     "messages.extend([{'status':200},{'type':'http.response.body','body':canonical(body)}])",
     S4_TEST+'test_s4_serialization_completes_before_return'),
    ('s4-serialization-deadline-ignored', "check_deadline(scope['_cortex_deadline'])", 'pass',
     S4_TEST+'test_s4_serialization_over_deadline_is_unfulfilled'),
    ('s4-response-binding-ignored', "raise Refusal('response_binding_changed')", 'pass',
     S4_TEST+'test_s4_response_input_truth_and_bytes_are_bound'),
    ('s4-hit-source-ignored', "raise Refusal('hit_source_mismatch')", 'pass',
     S4_TEST+'test_s4_offline_unknown_foreign_duplicate_kind_revision_and_identity_refuse'),
    ('s4-duplicate-inventory-ignored', "if not measured['safe']: raise Refusal('unsafe_hit_inventory')", 'if False: pass',
     S4_TEST+'test_s4_offline_unknown_foreign_duplicate_kind_revision_and_identity_refuse'),
    ('s4-native-refusal-promoted', "return {'status':'NOT_RUN','reason':'owned_measured_state_and_provider_budget_unbound',",
     "return {'status':'PASS','reason':'owned_measured_state_and_provider_budget_unbound',",
     S4_TEST+'test_s4_prepare_refuses_unbound_native_state_and_miss_budget'),
    ('s4-warmup-errors-erased', "'warmup_errors':sum(r.get('status') != 'OK' for r in warmup_records)", "'warmup_errors':0",
     S4_TEST+'test_s4_preparation_is_bound_to_each_config_and_warmup_inventory'),
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

NATIVE_TARGETS = {
    "omit-cache-provider": "test_native_actual_cache_provider_search_and_core_scope",
    "input-binding-ignored": "test_native_unsupported_inputs_and_frozen_binding_no_effect",
    "current-core-scope-ignored": "test_native_current_core_scope_must_match_frozen_mapping",
    "provider-vector-binding-inverted": "test_native_provider_vector_must_match_frozen_query",
    "hit-kind-revision-ignored": "test_native_projected_source_must_match_frozen_corpus",
    "unavailable-counted-success": "test_native_core_down_load_is_charged_as_error",
    "offer-dropped-from-receipt": "test_native_fixed_offered_load_receipt_and_cleaned_connections",
    "synthetic-gtm-credit": "test_native_fixed_offered_load_receipt_and_cleaned_connections",
}

NATIVE_CHILD = r'''
import hashlib, importlib, json, pathlib, sys, unittest
from test_receipts import AssertionResult, MARKER
import test_b01_product_path
cases = [case for case in test_b01_product_path.native_suite()
         if case._testMethodName == sys.argv[1]]
if len(cases) != 1: raise RuntimeError("Native assertion selection is not unique")
expected = cases[0].id()
result = unittest.TextTestRunner(verbosity=2,resultclass=AssertionResult).run(unittest.TestSuite(cases))
module = importlib.import_module("vector_baseline.product_path")
path = pathlib.Path(module.__file__).resolve()
print(MARKER + json.dumps({"tests_run":result.testsRun,"failures":result.assertions,
 "errors":[{"id":test.id(),"traceback":trace} for test,trace in result.errors],
 "skipped":len(result.skipped),"expected_test":expected,"executed_source":{"path":str(path),
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


def native_faults(destination):
    """The caller owns/admitted PG; these children never allocate a resource.

    Fresh processes consume one temporary source fault and the frozen native
    assertion, avoiding cached modules and edits to the published source tree.
    """
    source = NEXT / "src/vector_baseline/product_path.py"
    original = source.read_bytes()
    frozen = (NEXT / "tests/benchmarks/test_b01_product_path.py").read_bytes()
    recipes = [row for row in MUTATIONS if row[0] in NATIVE_TARGETS]
    if len(recipes) != len(NATIVE_TARGETS): raise RuntimeError("Native recipes are not complete")
    rows = []
    with tempfile.TemporaryDirectory(prefix="b01-native-literal-source-") as directory:
        stage = Path(directory) / "src"
        shutil.copytree(NEXT / "src", stage)
        staged_inputs = {}
        for folder in ("schema", "contracts"):
            target = Path(directory) / folder
            shutil.copytree(NEXT / folder, target)
            for path in (NEXT / folder).rglob("*"):
                if path.is_file():
                    relative = path.relative_to(NEXT)
                    digest = sha(path.read_bytes())
                    if sha((Path(directory) / relative).read_bytes()) != digest:
                        raise RuntimeError("Native source input closure mismatch")
                    staged_inputs[str(relative)] = digest
        if "schema/manifest.json" not in staged_inputs:
            raise RuntimeError("Native schema manifest input is absent")
        candidate = stage / "vector_baseline/product_path.py"
        temporary = Path(directory) / "child-temporary"
        temporary.mkdir()
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR") if key in os.environ}
        env.update({key: os.environ[key] for key in ("TEST_DATABASE_URL", "B01_OWNED_SEARCH_DSN")})
        psycopg_root = str(Path(importlib.import_module("psycopg").__file__).resolve().parent.parent)
        env.update(PYTHONPATH=os.pathsep.join([str(stage), str(NEXT / "tests"),
            str(NEXT / "tests/benchmarks"), *dependency_roots(), psycopg_root]),
            TMPDIR=str(temporary), PYTHONDONTWRITEBYTECODE="1")

        def execute(label, data, method):
            candidate.write_bytes(data)
            flags = ["-S"] if sys.flags.no_site else []
            command = [sys.executable, *flags, "-c", NATIVE_CHILD, method]
            try:
                process = subprocess.run(command, env=env, capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired as error:
                def decoded(value): return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
                process = subprocess.CompletedProcess(command, 124, decoded(error.stdout), decoded(error.stderr))
            (destination / (label + ".stdout")).write_text(process.stdout)
            (destination / (label + ".stderr")).write_text(process.stderr)
            value = report(process)
            bound = bool(value and value.get("skipped") == 0 and value["tests_run"] == 1
                and value["executed_source"]["sha256"] == sha(data)
                and Path(value["executed_source"]["path"]).resolve() == candidate.resolve())
            return process, value, bound

        method = NATIVE_TARGETS["omit-cache-provider"]
        baseline, baseline_receipt, bound = execute("baseline", original, method)
        if not bound or baseline.returncode != 0: raise RuntimeError("Native fault baseline refused")
        for name, before, after, _ in recipes:
            text = original.decode()
            if text.count(before) != 1: raise RuntimeError("Native literal source point is not unique")
            effective = text.replace(before, after).encode()
            process, value, bound = execute(name, effective, NATIVE_TARGETS[name])
            expected = value.get("expected_test") if value else None
            verdict = classify(process, [expected]) if bound and expected else "inconclusive"
            rows.append({"name": name, "method": NATIVE_TARGETS[name], "expected_test": expected,
                "source_sha256": sha(effective), "source_bound": bound, "exit": process.returncode,
                "verdict": verdict, "receipt": value})
            (destination / "native-fault-progress.json").write_text(json.dumps(rows, indent=2) + "\n")
        restored, restored_receipt, bound = execute("restored", original, method)
        if not bound or restored.returncode != 0: raise RuntimeError("Native fault restoration refused")
    if source.read_bytes() != original or (NEXT / "tests/benchmarks/test_b01_product_path.py").read_bytes() != frozen:
        raise RuntimeError("Native source/test custody changed")
    receipt = {"scope": "REAL_PG_COMPONENTS_WITH_MOCKED_HOSTED_TRANSPORT", "source_sha256": sha(original),
        "frozen_test_sha256": sha(frozen), "baseline": baseline_receipt, "mutations": rows,
        "restored": restored_receipt, "all_killed": all(row["verdict"] == "killed" for row in rows),
        "staged_input_sha256": staged_inputs,
        "published_source_test_unchanged": True, "native_qualified": False, "gtm_qualified": False}
    (destination / "native-fault-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


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
        # Exact existing resource bytes are required by the real C11/S2 imports.
        for name in ("modules", "contracts"):
            shutil.copytree(NEXT / name, stage.parent / name)
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

        baseline, value, bound = execute("baseline", original, "test_b01_product_path")
        if not bound or baseline.returncode != 0: raise RuntimeError("Baseline is not qualified")
        faults = [(name,before,after,TEST+method) for name,before,after,method in MUTATIONS] + S4_MUTATIONS
        for name, before, after, target in faults:
            text = original.decode()
            if text.count(before) != 1: raise RuntimeError("Literal source point is not unique: " + name)
            mutated = text.replace(before, after).encode()
            process, value, bound = execute(name, mutated, target)
            verdict = classify(process, [target]) if bound else "inconclusive"
            rows.append({"name": name, "target": target, "source_sha256": sha(mutated),
                "source_bound": bound, "exit": process.returncode, "verdict": verdict,
                "tests": value["tests_run"] if value else None,
                "body_failures": len(value["failures"]) if value else None,
                "errors": len(value["errors"]) if value else None})
        restored, value, bound = execute("restored", original, "test_b01_product_path")
        if not bound or restored.returncode != 0: raise RuntimeError("Restoration is not qualified")
    if source.read_bytes() != original or (NEXT / "tests/benchmarks/test_b01_product_path.py").read_bytes() != frozen:
        raise RuntimeError("Original source/test bytes changed")
    receipt = {"scope": "UNIT_PORTS_WITH_REAL_HOSTED_MOCK_TRANSPORT", "source_sha256": sha(original),
               "frozen_test_sha256": sha(frozen),
               "shared_s3_fixture_sha256": sha((NEXT / "tests/receipt/test_c09_qdrant.py").read_bytes()),
               "shared_s3b_fixture_sha256": sha((NEXT / "tests/receipt/test_c09_qdrant_s3b.py").read_bytes()),
               "copied_resource_sha256": {str(p.relative_to(NEXT)): sha(p.read_bytes())
                   for name in ("modules", "contracts") for p in sorted((NEXT / name).rglob("*")) if p.is_file()},
               "native": "NOT_RUN", "mutations": rows,
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
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=NEXT):
        raise RuntimeError("Native receipt requires the clean exact committed candidate")
    tested_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=NEXT, text=True).strip()
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
    def engine_available_memory():
        connections = json.loads(postgres.podman(["system", "connection", "list", "--format", "json"]))
        defaults = [row for row in connections if row.get("Default")]
        if len(defaults) != 1:
            raise RuntimeError("Existing local machine context is not unique")
        current = defaults[0]
        if (current.get("Name") != "kos-e020-uat" or not current.get("IsMachine")
                or not current.get("URI", "").startswith("ssh://core@127.0.0.1:")):
            raise RuntimeError("Only the existing local rootless fixture machine is admitted")
        raw = postgres.podman(["machine", "ssh", current["Name"], "--", "cat", "/proc/meminfo"])
        fields = {line.split(":")[0]: int(line.split()[1]) for line in raw.splitlines()
                  if ":" in line and len(line.split()) > 1 and line.split()[1].isdigit()}
        return {"machine": current["Name"], "MemTotal_kB": fields.get("MemTotal"),
                "MemAvailable_kB": fields.get("MemAvailable"), "raw_meminfo": raw}

    admission = None
    engine = None
    try:
        admission = qdrant.preflight()
        engine = engine_available_memory()
        total, free = engine["MemTotal_kB"], engine["MemAvailable_kB"]
        if (admission["free_percent"] < 35 or type(total) is not int or type(free) is not int
                or total <= 0 or not 0 <= free <= total or free * 100 < 35 * total):
            raise RuntimeError("Fresh host and VM35percent admission refused")
    except RuntimeError:
        row = {"status": "BLOCKED", "reason": "memory_or_local_context_admission_refused",
               "tested_head": tested_head, "host_admission": admission, "engine_admission": engine,
               "native_resources_created": 0, "tests_run": 0, "native_qualified": False}
        (destination / "native-receipt.json").write_text(json.dumps(row, indent=2) + "\n")
        print(json.dumps(row, indent=2))
        return 2
    postgres.podman(["image", "exists", postgres.IMAGE])

    memory_admissions = []

    def admit_resource_memory(phase):
        allowed = (stack.name,) if "container" in stack.owned else ()
        observed = qdrant.preflight(allowed_names=allowed)
        engine = engine_available_memory()
        total, free = engine["MemTotal_kB"], engine["MemAvailable_kB"]
        valid = (observed["free_percent"] >= 35 and type(total) is int and type(free) is int
                 and total > 0 and 0 <= free <= total and free * 100 >= 35 * total)
        memory_admissions.append({"phase": phase, "host": observed,
            "vm_observation": engine, "passed": valid})
        if not valid: raise RuntimeError("Fresh create/start host and engine35percent admission refused")

    def cached_runner(arguments, *, input=None):
        if arguments and (arguments[0] in ("create", "start")
                or len(arguments) > 1 and arguments[1] == "create"):
            admit_resource_memory(":".join(arguments[:2]) if arguments[0] in ("volume", "secret") else arguments[0])
        if arguments and arguments[0] == "create":
            arguments = ["create", "--pull=never", *arguments[1:]]
        return postgres.podman(arguments, input=input)

    stack = postgres.DisposablePostgres(runner=cached_runner)
    names = ("TEST_DATABASE_URL", "B01_OWNED_SEARCH_DSN")
    previous = {name: os.environ.get(name) for name in names}
    receipt = {"status": "RUNNING", "scope": "SOURCE_REAL_PG_COMPONENTS", "profile": profile,
               "host_admission": admission, "engine_available_percent": free * 100 // total,
               "engine_admission": engine,
               "resource_memory_admissions": memory_admissions,
               "tested_head": tested_head, "native_qualified": False, "gtm_qualified": False,
               "fixture_sha256": sha((NEXT / "tests/benchmarks/test_b01_product_path.py").read_bytes()),
               "controller_sha256": sha(Path(__file__).read_bytes())}
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
            if result.wasSuccessful() and not result.skipped:
                faults = destination / "native-faults"
                faults.mkdir()
                receipt["native_faults"] = native_faults(faults)
                restored = unittest.TextTestRunner(verbosity=2, resultclass=AssertionResult).run(test_b01_product_path.native_suite())
                receipt["restored_components"] = {"tests": restored.testsRun, "body_failures": restored.assertions,
                    "errors": len(restored.errors), "skips": len(restored.skipped)}
            receipt["unit_complete"] = False  # Vera/Kai and final custody/CI still own credit.
            qualified = (result.wasSuccessful() and not result.skipped
                and receipt.get("native_faults", {}).get("all_killed")
                and restored.wasSuccessful() and not restored.skipped)
            receipt["status"] = "SOURCE_FIXTURE_GREEN" if qualified else "RED"
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
    return 0 if receipt["status"] == "SOURCE_FIXTURE_GREEN" and stack.cleanup_verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
