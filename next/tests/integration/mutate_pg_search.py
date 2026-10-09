"""Bounded, reproducible semantic mutations; fresh PG and cleanup per mutant."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_receipts as receipts  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/cortex_core/embeddings/pg_search.py"
SQL = ROOT / "schema/retrieval/001-pg-search.sql"
MUTATIONS = [
    (
        SOURCE,
        "subnormal_norm_admitted",
        "if not math.isfinite(norm) or norm < 2.0**-126:",
        "if not math.isfinite(norm) or norm <= 0:",
    ),
    (SOURCE, "runtime_bypass", "if bypass:", "if False and bypass:"),
    (
        SOURCE,
        "mixed_identity",
        'if state["identity"] != key:',
        'if False and state["identity"] != key:',
    ),
    (
        SOURCE,
        "late_embedding",
        'if row is None or row["source_revision"] != revision:',
        "if row is None:",
    ),
    (
        SOURCE,
        "revision_rollback",
        "source_revision < EXCLUDED.source_revision",
        "source_revision >= EXCLUDED.source_revision",
    ),
    (
        SOURCE,
        "disabled_empty",
        'if state != "ready":',
        'if False and state != "ready":',
    ),
    (
        SOURCE,
        "freshness_lie",
        'return "lagging" if self.pending_records else "current"',
        'return "current"',
    ),
    (
        SQL,
        "no_hnsw",
        "CREATE INDEX search_vectors_hnsw ON retrieval.search_vectors\n    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);",
        "",
    ),
    (
        SQL,
        "rls_not_forced",
        "ALTER TABLE retrieval.search_vectors FORCE ROW LEVEL SECURITY;",
        "",
    ),
]
TARGETS = {
    "subnormal_norm_admitted": "test_pg_search_review.ReviewTests.test_mike_collinear_subnormal_cosine_probe",
    "runtime_bypass": "test_pg_search.SearchTests.test_superuser_runtime_is_rejected",
    "mixed_identity": "test_pg_search.SearchTests.test_generation_change_excludes_old_vectors",
    "late_embedding": "test_pg_search.SearchTests.test_freshness_and_stale_response_rejection",
    "revision_rollback": "test_pg_search_review.ReviewTests.test_revision_notice_advances_monotonically",
    "disabled_empty": "test_pg_search.SearchTests.test_unavailable_is_distinct_from_empty",
    "freshness_lie": "test_pg_search.SearchTests.test_freshness_and_stale_response_rejection",
    "no_hnsw": "test_pg_search.SearchTests.test_hnsw_plan_and_distance",
    "rls_not_forced": "test_pg_search.SearchTests.test_forced_rls_without_scope_and_wrong_tenant_write",
}


def clean_run(returncode, output):
    cleanup = re.findall(r"^cleanup: (PASS|FAIL)$", output, re.MULTILINE)
    return (
        bool(cleanup)
        and cleanup[-1] == "PASS"
        and receipts.classify(subprocess.CompletedProcess([], returncode, output, ""), set()) == "survived"
    )


def classify_kill(returncode, output, target):
    cleanup = re.findall(r"^cleanup: (PASS|FAIL)$", output, re.MULTILINE)
    if not cleanup or cleanup[-1] != "PASS":
        return "INCONCLUSIVE"
    result = subprocess.CompletedProcess([], returncode, output, "")
    status = receipts.classify(result, {target})
    if status == "killed" and len(receipts.report(result)["failures"]) != 1:
        return "INCONCLUSIVE"
    return status.upper()


def execute(evidence, name, target=None):
    env = dict(os.environ)
    env.pop("SEARCH_TEST_TARGET", None)
    if target:
        env["SEARCH_TEST_TARGET"] = target
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests/integration/run_pg_search.py")],
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
    receipt = []
    code, output = execute(evidence, "baseline")
    if not clean_run(code, output):
        (evidence / "baseline.json").write_text(
            json.dumps({"status": "INCONCLUSIVE", "exit": code}) + "\n"
        )
        raise RuntimeError("INCONCLUSIVE: clean baseline required before any mutation")
    for path, name, before, after in MUTATIONS:
        original = path.read_bytes()
        text = original.decode()
        if text.count(before) != 1:
            raise RuntimeError("Mutation target drift: " + name)
        try:
            path.write_text(text.replace(before, after))
            target = TARGETS[name]
            code, output = execute(evidence, name, target)
            status = classify_kill(code, output, target)
            receipt.append(
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
                json.dumps(receipt, indent=2) + "\n"
            )
            print(name, status, flush=True)
            if status != "KILLED":
                raise RuntimeError(status + ": " + name)
        finally:
            path.write_bytes(original)
    (evidence / "mutations.json").write_text(json.dumps(receipt, indent=2) + "\n")
    code, output = execute(evidence, "restored")
    if not clean_run(code, output):
        raise RuntimeError("INCONCLUSIVE: restored full suite failed")
    print(
        str(len(receipt)) + "/" + str(len(MUTATIONS)) + " semantic mutants killed",
        flush=True,
    )


if __name__ == "__main__":
    run()
