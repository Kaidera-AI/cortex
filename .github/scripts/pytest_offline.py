"""Run the offline contract in isolated pytest processes, refusing partial green."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "packages/api/tests"
TARGETS = (
    ("test_graph_stats_performance.py",),
    ("test_schema_migrations.py",),
    ("test_search_decisions_security_definer.py",),
    ("test_search_messages_security_definer.py",),
    ("test_openkai_provider_env.py",),
    ("test_graph_build_jobs.py",),
    ("test_service_auth_hardening.py",),
    ("test_db_tls.py",),
    (
        "test_agents_register.py::test_register_agent_handler_no_longer_writes_redis_roster_state",
        "test_agents_register.py::test_agent_runtime_state_migration_shape",
    ),
)


class CompleteGate:
    def __init__(self, report):
        self.report = report
        self.collected = []
        self.passed = set()
        self.failed = set()
        self.skipped = set()
        self.deselected = []

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_deselected(self, items):
        self.deselected.extend(item.nodeid for item in items)

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)
        elif report.skipped:
            self.skipped.add(report.nodeid)
        elif report.when == "call" and report.passed:
            self.passed.add(report.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        complete = bool(self.collected) and set(self.collected) == self.passed
        if self.failed or self.skipped or self.deselected or not complete:
            session.exitstatus = 1
        self.report.write_text(json.dumps({
            "pytest_exit": int(exitstatus), "gate_exit": int(session.exitstatus),
            "collected": self.collected, "passed": sorted(self.passed),
            "failed": sorted(self.failed), "skipped": sorted(self.skipped),
            "deselected": self.deselected, "complete": complete,
        }, indent=2) + "\n")


def worker(targets, report):
    import pytest
    gate = CompleteGate(report)
    code = pytest.main([
        "-p", "pytest_asyncio.plugin", "--strict-markers", "-q", "--tb=short",
        "-o", "cache_dir=" + str(report.parent / "cache"),
        "--basetemp=" + str(report.parent / (report.stem + "-tmp")),
        *[str(TESTS / target) for target in targets],
    ], plugins=[gate])
    return int(code)


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CORTEX_", "HARNESS_", "OPENKAI_", "PG"))}
    env.update({"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "",
                "PYTHONDONTWRITEBYTECODE": "1", "CORTEX_TEST_PROJECT": "",
                "CORTEX_API_URL": "http://127.0.0.1:1",
                "CORTEX_ADMIN_TOKEN": "offline-ci-unused"})
    receipts = []
    failed = False
    for index, targets in enumerate(TARGETS):
        report = output / f"suite-{index:02}.json"
        report.unlink(missing_ok=True)
        command = [sys.executable, str(Path(__file__).resolve()), "--worker",
                   "--output", str(report), "--", *targets]
        try:
            result = subprocess.run(command, cwd=ROOT, env=env, text=True,
                                    capture_output=True, timeout=180)
        except subprocess.TimeoutExpired:
            failed = True
            receipts.append({"targets": targets, "error": "TIMEOUT"})
            print(f"offline suite {index}: TIMEOUT", flush=True)
            continue
        (output / f"suite-{index:02}.txt").write_text(result.stdout + result.stderr)
        print(result.stdout + result.stderr, flush=True)
        if not report.is_file():
            failed = True
            receipts.append({"targets": targets, "error": "missing pytest receipt",
                             "process_exit": result.returncode})
            continue
        receipt = json.loads(report.read_text())
        receipt["targets"] = targets
        receipt["process_exit"] = result.returncode
        failed |= result.returncode != 0 or receipt["gate_exit"] != 0
        receipts.append(receipt)
    passed = sum(len(row.get("passed", [])) for row in receipts)
    skipped = sum(len(row.get("skipped", [])) for row in receipts)
    deselected = sum(len(row.get("deselected", [])) for row in receipts)
    failed |= passed != 145 or skipped != 0 or deselected != 0
    summary = {"expected_tests": 145, "passed": passed, "skipped": skipped,
               "deselected": deselected, "exit": int(failed), "suites": receipts}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"offline contract: {passed}/145 passed; {skipped} skipped; "
          f"{deselected} deselected; exit={int(failed)}", flush=True)
    return int(failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("targets", nargs="*")
    args = parser.parse_args()
    destination = args.output or Path(tempfile.mkdtemp(prefix="cortex-pytest-offline-"))
    raise SystemExit(worker(args.targets, destination) if args.worker else run(destination))
