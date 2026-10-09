"""P01 named assertion mutants, admitted by a clean full baseline and cleanup."""

import json
import os
import subprocess
import sys
from pathlib import Path

from mutate_pg_search import classify_kill, clean_run

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "src/cortex_core/embeddings/query_cache.py"
HOSTED = ROOT / "src/cortex_core/modules/providers/hosted.py"
SQL = ROOT / "schema/retrieval/002-query-cache.sql"
CONTEXT = ROOT / "src/cortex_core/embeddings/pg_search.py"
MUTATIONS = [
    (
        CONTEXT,
        "stale_admission_snapshot",
        "conn.transaction(isolation=isolation)",
        'conn.transaction(isolation="repeatable_read")',
    ),
    (
        CACHE,
        "ttl_ignored",
        'row is not None and row["fresh"] and row["vector"] is not None',
        'row is not None and row["vector"] is not None',
    ),
    (
        CACHE,
        "permission_cache_collision",
        "            scope.permission_generation,",
        '            "constant-permissions",',
    ),
    (
        CACHE,
        "stale_fence_publish",
        "AND query_digest=$5 AND owner=$6 AND fence=$7 AND lease_until > clock_timestamp()",
        "AND query_digest=$5 AND $6::uuid IS NOT NULL AND $7::bigint > 0 AND lease_until > clock_timestamp()",
    ),
    (
        HOSTED,
        "different_provider_endpoint",
        "https://openrouter.ai/api/v1/embeddings",
        "https://wrong-provider.invalid/embeddings",
    ),
    (
        HOSTED,
        "invalid_vector_accepted",
        "                vector_literal(vector, identity.dimensions)",
        "                pass",
    ),
    (
        HOSTED,
        "unbounded_credentials",
        "async with asyncio.timeout(self.timeout_seconds):",
        "async with asyncio.timeout(1000):",
    ),
    (
        SQL,
        "cache_rls_not_forced",
        "ALTER TABLE retrieval.query_embeddings FORCE ROW LEVEL SECURITY;",
        "",
    ),
]
TARGETS = {
    "stale_admission_snapshot": "test_query_cache.CacheTests.test_capacity_concurrent_distinct_queries",
    "ttl_ignored": "test_query_cache.CacheTests.test_ttl_and_failure_not_cached",
    "permission_cache_collision": "test_query_cache.CacheTests.test_cache_key_tenant_project_permission_identity",
    "stale_fence_publish": "test_query_cache.CacheTests.test_fence_rejects_old_response_during_successor_lease",
    "different_provider_endpoint": "test_query_cache.ProviderTests.test_existing_hosted_endpoint_and_snapshot",
    "invalid_vector_accepted": "test_query_cache.ProviderTests.test_provider_refuses_missing_key_wrong_provider_and_invalid_output",
    "unbounded_credentials": "test_query_cache.ProviderTests.test_response_bound_and_total_timeout",
    "cache_rls_not_forced": "test_query_cache.CacheTests.test_cache_rls_without_scope",
}


def execute(evidence, name, target=None):
    env = dict(os.environ)
    env.pop("SEARCH_TEST_TARGET", None)
    if target:
        env["SEARCH_TEST_TARGET"] = target
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests/integration/run_query_cache.py")],
        capture_output=True,
        text=True,
        env=env,
    )
    output = result.stdout + result.stderr
    (evidence / (name + ".log")).write_text(
        output + "\nexit=" + str(result.returncode) + "\n"
    )
    return result.returncode, output


def run():
    evidence = Path(sys.argv[1])
    evidence.mkdir(parents=True, exist_ok=True)
    receipts = []
    code, output = execute(evidence, "baseline")
    if not clean_run(code, output):
        (evidence / "baseline.json").write_text(
            json.dumps({"status": "INCONCLUSIVE", "exit": code}) + "\n"
        )
        raise RuntimeError("INCONCLUSIVE: clean full baseline required before edits")
    for path, name, before, after in MUTATIONS:
        original = path.read_bytes()
        text = original.decode()
        if text.count(before) != 1:
            raise RuntimeError("Mutation drift: " + name)
        try:
            path.write_text(text.replace(before, after))
            target = TARGETS[name]
            code, output = execute(evidence, name, target)
            status = classify_kill(code, output, target)
            receipts.append(
                {
                    "mutation": name,
                    "file": str(path.relative_to(ROOT)),
                    "exit": code,
                    "target": target,
                    "status": status,
                    "killed": status == "KILLED",
                }
            )
            (evidence / "mutations.json").write_text(
                json.dumps(receipts, indent=2) + "\n"
            )
            print(name, status, flush=True)
            if status != "KILLED":
                raise RuntimeError(status + ": " + name)
        finally:
            path.write_bytes(original)
    (evidence / "mutations.json").write_text(json.dumps(receipts, indent=2) + "\n")
    code, output = execute(evidence, "restored")
    if not clean_run(code, output):
        raise RuntimeError("INCONCLUSIVE: restored full suite failed")
    print("8/8 semantic mutants killed", flush=True)


if __name__ == "__main__":
    run()
