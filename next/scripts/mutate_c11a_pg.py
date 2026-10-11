"""Real-PG C11a permission-bypass source-copy fault, with owned cleanup proof."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "next/src/cortex_core/api/c11a.py"
BEFORE = "if allowed is not True:"
AFTER = "if False:"
EXPECTED = "test_c11a_pg.C11aPGTests.test_real_pg_search_ready_and_revoked_grant_fails_closed"


def main():
    body = SOURCE.read_text()
    assert body.count(BEFORE) == 1
    source_digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="cox-c11a-pg-fault-") as temporary:
        root = Path(temporary) / "next"
        target = root / "src/cortex_core/api/c11a.py"
        target.parent.mkdir(parents=True)
        target.write_text(body.replace(BEFORE, AFTER, 1))
        contracts = root / "contracts"
        contracts.mkdir()
        for filename in ("route-matrix.md", "c11a-consumer-routes.json"):
            shutil.copy2(ROOT / "next/contracts" / filename, contracts / filename)
        env = os.environ.copy()
        env["C11A_TEST_OVERLAY"] = str(root / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run([sys.executable, str(ROOT / "next/tests/c11a/run_pg.py")],
                                capture_output=True, text=True, env=env)
        packets = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        run = next((row for row in packets if "exit_code" in row), None)
        cleanup = next((row for row in packets if row.get("cleanup") == "pass"), None)
        lines = [] if run is None else [json.loads(line.split("=", 1)[1])
                                      for line in run["stdout"].splitlines()
                                      if line.startswith("CORTEX_TEST_RESULT=")]
        report = lines[0] if len(lines) == 1 else None
        killed = (result.returncode == 1 and run is not None and run["exit_code"] == 1
                  and cleanup is not None and report is not None and report["tests_run"] == 1
                  and not report["errors"] and EXPECTED in {row["id"] for row in report["failures"]}
                  and all(row["phase"] == "test" and row["is_assertion"] is True
                          for row in report["failures"]))
        assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == source_digest
        print(json.dumps({"status": "killed" if killed else "inconclusive",
                          "source_sha256": source_digest,
                          "mutated_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                          "expected_test": EXPECTED, "exit_code": result.returncode,
                          "runner_packets": packets, "receipt": report,
                          "stdout": result.stdout, "stderr": result.stderr}, sort_keys=True))
        if not killed:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
