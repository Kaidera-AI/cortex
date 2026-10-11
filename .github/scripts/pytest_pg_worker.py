"""In-pod source-bound test runner; no Podman or host engine is available here."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request


SOURCE = Path("/source")
API = SOURCE / "packages/api"
TESTS = API / "tests"
ADMIN_DSN = "postgresql://postgres@127.0.0.1:55431/platform_agent_memory"
sys.path[:0] = [str(API), str(TESTS), str(SOURCE / ".github/scripts")]


class OutcomeGate:
    def __init__(self, report, expected_failures=0, expected_selected=None):
        self.report = report
        self.expected_failures = expected_failures
        self.expected_selected = expected_selected
        self.items = []
        self.passed = set()
        self.failed = set()
        self.skipped = set()
        self.deselected = []
        self.reports = []

    def pytest_collection_finish(self, session):
        self.items = [item.nodeid for item in session.items]

    def pytest_deselected(self, items):
        self.deselected.extend(item.nodeid for item in items)

    def pytest_runtest_logreport(self, report):
        self.reports.append({"nodeid": report.nodeid, "phase": report.when,
                             "outcome": report.outcome, "properties": report.user_properties,
                             "longrepr": str(report.longrepr) if report.failed or report.skipped else ""})
        if report.failed:
            self.failed.add(report.nodeid)
        elif report.skipped:
            self.skipped.add(report.nodeid)
        elif report.when == "call" and report.passed:
            self.passed.add(report.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        body_failures = {row["nodeid"] for row in self.reports
                         if row["phase"] == "call" and row["outcome"] == "failed"}
        complete = set(self.items) == self.passed | self.failed and bool(self.items)
        ok = complete and not self.skipped and not self.deselected
        ok &= len(self.failed) == self.expected_failures and self.failed == body_failures
        ok &= int(exitstatus) == (1 if self.expected_failures else 0)
        ok &= self.expected_selected is None or len(self.items) == self.expected_selected
        session.exitstatus = 0 if ok else 1
        self.report.write_text(json.dumps({
            "selected": self.items, "passed": sorted(self.passed), "failed": sorted(self.failed),
            "skipped": sorted(self.skipped), "deselected": self.deselected,
            "pytest_exit": int(exitstatus), "gate_exit": int(session.exitstatus),
            "expected_body_failures": self.expected_failures, "reports": self.reports,
        }, indent=2) + "\n")


def pytest_worker(args):
    import pytest
    gate = OutcomeGate(args.report, args.expected_failures, args.expected_selected)
    return int(pytest.main([
        "-p", "pytest_asyncio.plugin", *(["-p", "pytest_pg_bypass"] if args.bypass else []),
        "--strict-markers", "-q", "--tb=short", "-o", "cache_dir=/tmp/pytest-pg-cache",
        "--basetemp=/tmp/" + args.report.stem,
        *[str(TESTS / target) for target in args.targets],
    ], plugins=[gate]))


async def database_snapshot(dsn):
    import asyncpg
    spec = importlib.util.spec_from_file_location("guard_snapshot", TESTS / "test_owned_rls_environment.py")
    contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    conn = await asyncpg.connect(dsn)
    try:
        return await contract._database_snapshot(conn)
    finally:
        await conn.close()


async def bootstrap(output):
    import asyncpg
    sys.path.insert(0, "/app/standalone")
    import migrate
    assert hashlib.sha256(Path(migrate.cortex.__file__).read_bytes()).digest() == hashlib.sha256((API / "main.py").read_bytes()).digest()
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        receipt = await migrate.migrate_connection(conn, migration_dir=SOURCE / "packages/schema/migrations")
        (output / "migration.json").write_text(json.dumps(receipt, indent=2) + "\n")
    finally:
        await conn.close()


async def provision_marker_and_api_project(project):
    import asyncpg
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute("CREATE SCHEMA cortex_test_fixture")
        await conn.execute("CREATE TABLE cortex_test_fixture.owned_rls(run_id text PRIMARY KEY,database_name text NOT NULL)")
        await conn.execute("INSERT INTO cortex_test_fixture.owned_rls VALUES($1,current_database())",
                           os.environ["CORTEX_OWNED_RLS_RUN_ID"])
        await conn.execute("INSERT INTO cortex_projects(project_key,display_name,repo_root,repo_type) "
                           "VALUES($1,'CI API fixture',$2,'repo')", project, "/owned-ci/" + project)
        await conn.execute("INSERT INTO roles(project,name,description,is_builtin,default_capabilities) "
                           "VALUES($1,'service','CI test writer',true,'{}'::jsonb)", project)
        await conn.execute("INSERT INTO agents(name,project,role,capabilities,status) "
                           "VALUES('kai',$1,'service','{\"writer_scope\":\"work\",\"keep_visible\":true}'::jsonb,'available')", project)
        await conn.execute("CREATE DATABASE cortex_ci_migration_005")
    finally:
        await conn.close()


def general(output):
    output.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    api_project = os.environ["CORTEX_CI_API_PROJECT"]
    rls_project = os.environ["CORTEX_CI_RLS_PROJECT"]
    batches = []
    server = None

    def batch(name, targets, expected, *, project=api_project, dsn=ADMIN_DSN,
              bypass=False, failures=0):
        local = dict(environment, CORTEX_TEST_PROJECT=project, CORTEX_TEST_PG_DSN=dsn,
                     CORTEX_OWNED_RLS_DSN=ADMIN_DSN,
                     CORTEX_UNMARKED_RLS_TEST_DSN=ADMIN_DSN,
                     CORTEX_MARKED_RLS_TEST_DSN=ADMIN_DSN)
        report = output / (name + ".json")
        argv = [sys.executable, str(Path(__file__).resolve()), "--pytest-worker",
                "--report", str(report), "--expected-failures", str(failures),
                "--expected-selected", str(expected)]
        if bypass:
            argv.append("--bypass")
        argv.extend(["--", *targets])
        result = subprocess.run(argv, cwd=SOURCE, env=local, text=True, capture_output=True, timeout=180)
        (output / (name + ".txt")).write_text(result.stdout + result.stderr)
        if not report.is_file():
            raise RuntimeError(name + ": missing pytest receipt")
        receipt = json.loads(report.read_text())
        ok = result.returncode == 0 and receipt["gate_exit"] == 0 and len(receipt["selected"]) == expected
        batches.append({"name": name, "report": report.name, "expected": expected,
                        "control": bypass, "ok": ok, "process_exit": result.returncode})
        print(name, "selected", len(receipt["selected"]), "passed", len(receipt["passed"]),
              "failed", len(receipt["failed"]), "gate", result.returncode, flush=True)
        return ok

    try:
        asyncio.run(bootstrap(output))
        before = asyncio.run(database_snapshot(ADMIN_DSN))
        (output / "before-unmarked.json").write_text(json.dumps(before, indent=2) + "\n")
        if not batch("g1-unmarked", ["test_owned_rls_environment.py::test_unmarked_owned_database_refusal_has_zero_writes"], 1):
            raise RuntimeError("unmarked zero-write guard failed")
        assert asyncio.run(database_snapshot(ADMIN_DSN)) == before
        asyncio.run(provision_marker_and_api_project(api_project))
        marked_before = asyncio.run(database_snapshot(ADMIN_DSN))
        if not batch("g1-wrong-run", ["test_owned_rls_environment.py::test_mismatched_marker_run_id_has_zero_writes"], 1):
            raise RuntimeError("wrong-run zero-write guard failed")
        assert asyncio.run(database_snapshot(ADMIN_DSN)) == marked_before
        log = (output / "api.txt").open("w")
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8501"],
                                  cwd=API, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                  start_new_session=True)
        ready = False
        for _ in range(100):
            if server.poll() is not None:
                break
            try:
                with urllib.request.urlopen("http://127.0.0.1:8501/health", timeout=.5) as response:
                    health = json.load(response)
                    ready = health.get("status") == "healthy"
                    if ready:
                        (output / "health.json").write_text(json.dumps(health, indent=2) + "\n")
                        break
            except Exception:
                time.sleep(.2)
        if not ready:
            raise RuntimeError("actual owned API did not become healthy")
        for name, count in [("test_handoff_lifecycle.py", 6), ("test_mcp_live.py", 10), ("test_sessions_ingest.py", 10)]:
            batch(name.removesuffix(".py"), [name], count)
        batch("agents-live", ["test_agents_register.py::" + name for name in [
            "test_app_user_cannot_run_roles_ddl_but_api_registers_agent",
            "test_project_api_registers_arbitrary_project_agent_role",
            "test_register_agent_persists_runtime_state_outside_capabilities",
            "test_runtime_state_is_project_scoped_for_same_agent_name"]], 4)
        os.killpg(server.pid, signal.SIGTERM)
        server.wait(timeout=10)
        server = None
        log.close()
        batch("migration005", ["test_migration_005.py"], 3,
              dsn="postgresql://postgres@127.0.0.1:55431/cortex_ci_migration_005")
        batch("exact-invalidation", ["test_exact_id_invalidation_native.py"], 4)
        baseline = asyncio.run(database_snapshot(ADMIN_DSN))
        for name, count in [("test_phase_c_rls.py", 4), ("test_phase_c_rls_audit_extension.py", 5)]:
            batch(name.removesuffix(".py"), [name], count, project=rls_project)
            after = asyncio.run(database_snapshot(ADMIN_DSN))
            assert after == baseline, "RLS teardown changed rows/grants/roles/schema ACL"
        (output / "rls-before.json").write_text(json.dumps(baseline, indent=2) + "\n")
        (output / "rls-after.json").write_text(json.dumps(after, indent=2) + "\n")
        batch("bypass-control", [
            "test_phase_c_rls.py::test_rls_isolates_under_non_superuser_role",
            "test_phase_c_rls_audit_extension.py::test_project_key_isolation_under_non_superuser",
            "test_phase_c_rls_audit_extension.py::test_project_id_isolation_via_subquery"],
            3, project=rls_project, bypass=True, failures=3)
        assert asyncio.run(database_snapshot(ADMIN_DSN)) == baseline
    finally:
        if server and server.poll() is None:
            os.killpg(server.pid, signal.SIGTERM)
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)
                server.wait(timeout=3)
        (output / "batches.json").write_text(json.dumps(batches, indent=2) + "\n")
    return 0 if all(batch["ok"] for batch in batches) else 1


def certificate(output):
    output.mkdir(parents=True, exist_ok=True)
    report = output / "certificates.json"
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--pytest-worker",
                             "--report", str(report), "--expected-failures", "0", "--",
                             "test_cert_connect_native.py"], cwd=SOURCE, text=True,
                            capture_output=True, timeout=240)
    (output / "certificates.txt").write_text(result.stdout + result.stderr)
    data = json.loads(report.read_text()) if report.is_file() else {}
    return 0 if result.returncode == 0 and len(data.get("selected", [])) == 3 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest-worker", action="store_true")
    parser.add_argument("--mode", choices=["general", "certificate"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--expected-failures", type=int, default=0)
    parser.add_argument("--expected-selected", type=int)
    parser.add_argument("--bypass", action="store_true")
    parser.add_argument("targets", nargs="*")
    args = parser.parse_args()
    raise SystemExit(pytest_worker(args) if args.pytest_worker else
                     general(args.output) if args.mode == "general" else certificate(args.output))
