"""Bounded, reproducible semantic mutations; fresh PG and cleanup per mutant."""

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/cortex_core/embeddings/pg_search.py"
SQL = ROOT / "schema/retrieval/001-pg-search.sql"
MUTATIONS = [
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


def run():
    evidence = Path(sys.argv[1])
    evidence.mkdir(parents=True, exist_ok=True)
    receipt = []
    for path, name, before, after in MUTATIONS:
        original = path.read_bytes()
        text = original.decode()
        if text.count(before) != 1:
            raise RuntimeError("Mutation target drift: " + name)
        try:
            path.write_text(text.replace(before, after))
            result = subprocess.run(
                [sys.executable, str(ROOT / "tests/integration/run_pg_search.py")],
                capture_output=True,
                text=True,
            )
            output = result.stdout + result.stderr
            (evidence / (name + ".log")).write_text(
                output + "\nexit=" + str(result.returncode) + "\n"
            )
            killed = (
                result.returncode != 0
                and "Ran 10 tests" in output
                and "cleanup: PASS" in output
                and "FAILED" in output
            )
            receipt.append(
                {
                    "mutation": name,
                    "file": str(path.relative_to(ROOT)),
                    "exit": result.returncode,
                    "killed": killed,
                }
            )
            print(name, "KILLED" if killed else "NOT PROVEN", flush=True)
            if not killed:
                raise RuntimeError("Mutation survived or environment failed: " + name)
        finally:
            path.write_bytes(original)
    (evidence / "mutations.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        str(len(receipt)) + "/" + str(len(MUTATIONS)) + " semantic mutants killed",
        flush=True,
    )


if __name__ == "__main__":
    run()
