"""P01 semantic mutants with real PG, complete suite and cleanup each run."""

import json
from pathlib import Path
import subprocess
import sys

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


def run():
    evidence = Path(sys.argv[1])
    evidence.mkdir(parents=True, exist_ok=True)
    receipts = []
    for path, name, before, after in MUTATIONS:
        original = path.read_bytes()
        text = original.decode()
        if text.count(before) != 1:
            raise RuntimeError("Mutation drift: " + name)
        try:
            path.write_text(text.replace(before, after))
            result = subprocess.run(
                [sys.executable, str(ROOT / "tests/integration/run_query_cache.py")],
                capture_output=True,
                text=True,
            )
            output = result.stdout + result.stderr
            (evidence / (name + ".log")).write_text(
                output + "\nexit=" + str(result.returncode) + "\n"
            )
            killed = (
                result.returncode != 0
                and "Ran 34 tests" in output
                and "cleanup: PASS" in output
                and "FAILED" in output
            )
            receipts.append(
                {
                    "mutation": name,
                    "file": str(path.relative_to(ROOT)),
                    "exit": result.returncode,
                    "killed": killed,
                }
            )
            print(name, "KILLED" if killed else "NOT PROVEN", flush=True)
            if not killed:
                raise RuntimeError("Mutation survived/environment failure: " + name)
        finally:
            path.write_bytes(original)
    (evidence / "mutations.json").write_text(json.dumps(receipts, indent=2) + "\n")
    print("8/8 semantic mutants killed", flush=True)


if __name__ == "__main__":
    run()
