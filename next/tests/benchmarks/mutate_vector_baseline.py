"""Bounded semantic probes; kill only the named test's assertion failure."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

NEXT = Path(__file__).resolve().parents[2]
MUTATIONS = [
    ("corpus.py", "normalization", "        v /= np.sqrt(np.sum(v * v, axis=1))[:, None]",
     "        v *= 1.0", "test_vector_baseline.CorpusTests.test_byte_reproducibility_and_chunk_independence"),
    ("corpus.py", "stored-cosine-admission", 'if identity["metric"] == "cosine" and np.any(np.linalg.norm(stored.astype(np.float64), axis=-1) == 0):',
     'if False:', "test_vector_baseline_failures.FailureControls.test_float32_underflow_refused"),
    ("oracle.py", "tenant-leak", '(records["tenant"] == q["tenant"].encode())',
     'np.ones(len(records), dtype=bool)', "test_vector_baseline.OracleTests.test_full_scan_scopes_deletes_and_all_metrics"),
    ("oracle.py", "tombstone-leak", '& ~records["deleted"]',
     '& np.ones(len(records), dtype=bool)', "test_vector_baseline.OracleTests.test_full_scan_scopes_deletes_and_all_metrics"),
    ("oracle.py", "rrf-offset", '1 / (2 + np.arange(len(selected), dtype=np.float64))',
     '1 / (60 + np.arange(len(selected), dtype=np.float64))', "test_vector_baseline.OracleTests.test_exact_branch_ties_precede_prefetch_and_full_fusion_separate"),
    ("postgres.py", "cleanup-skips-volume", 'if resource in self.owned:',
     'if resource in self.owned and resource != "volume":', "test_vector_baseline.PostgresContractTests.test_cleanup_attempts_all_owned_resources_after_failure"),
    ("postgres.py", "unfiltered-SQL", '"tenant = %s", "project = %s", "NOT deleted", "generation = %s"',
     '"tenant = %s", "project = %s", "true", "generation = %s"', "test_vector_baseline.PostgresContractTests.test_query_uses_parameters_scope_and_dense_operator"),
]


def main():
    destination = NEXT / "benchmarks/vector_baseline/receipts/mutations"
    destination.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    test_path = str(NEXT / "tests/benchmarks")
    baseline = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", test_path, "-v"],
                              env={**env, "PYTHONPATH": str(NEXT / "src")}, capture_output=True, text=True)
    if baseline.returncode:
        raise RuntimeError("mutation baseline is RED")
    results = []
    for filename, label, before, after, test in MUTATIONS:
        source = NEXT / "src/vector_baseline" / filename
        original = source.read_bytes()
        if original.decode().count(before) != 1:
            raise RuntimeError("mutation target drift: " + label)
        with tempfile.TemporaryDirectory(prefix="b01-mutant-") as tmp:
            root = Path(tmp) / "vector_baseline"
            root.mkdir()
            for path in (NEXT / "src/vector_baseline").glob("*.py"):
                data = path.read_bytes()
                if path == source:
                    data = data.decode().replace(before, after, 1).encode()
                (root / path.name).write_bytes(data)
            r = subprocess.run([sys.executable, "-m", "unittest", test, "-v"],
                               env={**env, "PYTHONPATH": str(Path(tmp)) + os.pathsep + test_path},
                               capture_output=True, text=True)
            output = r.stdout + r.stderr
            (destination / (label + ".stdout.txt")).write_text(r.stdout)
            (destination / (label + ".stderr.txt")).write_text(r.stderr)
            method = test.rsplit(".", 1)[1]
            killed = (r.returncode == 1 and f"FAIL: {method} (" in output
                      and "FAILED (failures=" in output and "ERROR:" not in output)
            results.append({"source": filename, "mutation": label, "expected_test": test,
                            "exit": r.returncode, "killed": killed,
                            "source_sha256": hashlib.sha256(original).hexdigest()})
            if not killed:
                raise RuntimeError("mutation survived or failed for unrelated reason: " + label)
    (destination / "receipt.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps({"mutants": len(results), "killed": sum(r["killed"] for r in results)}))


if __name__ == "__main__":
    main()
