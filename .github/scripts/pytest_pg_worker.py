"""In-pod source-bound test runner; no Podman or host engine is available here."""

from __future__ import annotations

import argparse
from collections import Counter
import asyncio
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import tempfile
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
        ok &= all(row["phase"] == "call" for row in self.reports if row["outcome"] == "failed")
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


def runtime_contract(output):
    assert os.getuid() == os.getgid() == 10001
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    assert int(status["CapEff"].strip(), 16) == 0
    assert status["NoNewPrivs"].strip() == "1"
    root_options = next(line.split()[3].split(",") for line in Path("/proc/mounts").read_text().splitlines() if line.split()[1] == "/")
    assert "ro" in root_options
    cg = Path("/sys/fs/cgroup")
    limits = {name: (cg / name).read_text().strip() for name in ["memory.max", "memory.swap.max", "cpu.max"]}
    assert limits["memory.max"] == str(736 * 1024 ** 2)
    assert limits["memory.swap.max"] == "0"
    quota, period = map(int, limits["cpu.max"].split())
    assert quota / period == 1.45
    (output / "runtime-contract.json").write_text(json.dumps({"uid": os.getuid(), "gid": os.getgid(),
        "cap_eff": status["CapEff"].strip(), "no_new_privs": 1, "root_options": root_options,
        "limits": limits, "python": sys.version}, indent=2) + "\n")


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
    runtime_contract(output)
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
    runtime_contract(output)
    report = output / "certificates.json"
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--pytest-worker",
                             "--report", str(report), "--expected-failures", "0", "--",
                             "test_cert_connect_native.py"], cwd=SOURCE, text=True,
                            capture_output=True, timeout=240)
    (output / "certificates.txt").write_text(result.stdout + result.stderr)
    data = json.loads(report.read_text()) if report.is_file() else {}
    return 0 if result.returncode == 0 and len(data.get("selected", [])) == 3 else 1


NATIVE_ROLES_SUITE = "next/release/ship/install/tests/test_roles_pg.py"
NATIVE_ROLE_NODES = [NATIVE_ROLES_SUITE + "::" + name for name in (
    "RolesInDatabase::test_1_pre_creates_exactly_the_approved_logins_with_exact_flags_and_scram",
    "RolesInDatabase::test_2_pre_leaves_the_two_groups_to_the_unchanged_migrations",
    "RolesInDatabase::test_3_post_grants_exactly_one_set_only_membership",
    "RolesInDatabase::test_4_post_refuses_an_unexpected_membership",
    "RolesInDatabase::test_5_phases_rerun_to_the_same_state",
    "RolesInDatabase::test_6_close_closes_only_the_finite_logins",
    "RolesInDatabase::test_7_a_closed_login_cannot_connect_and_a_service_login_still_can",
    "RolesInDatabase::test_8_close_reruns_cleanly",
    "SecretGuard::test_a_missing_or_short_secret_creates_nothing",
)]


def native_roles_result(packet, process_exit, source_sha256, cleanup):
    """Nine exact native methods; unit packets do not themselves execute PostgreSQL."""
    spec = importlib.util.spec_from_file_location("native_roles_phase_gate",
        Path(__file__).with_name("pytest_next_offline.py"))
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return (packet.get("suite") == NATIVE_ROLES_SUITE
        and packet.get("source_sha256") == source_sha256
        and Counter(packet.get("expected", [])) == Counter(NATIVE_ROLE_NODES)
        and Counter(packet.get("collected", [])) == Counter(NATIVE_ROLE_NODES)
        and cleanup == dict(ok=True, errors=[], residual=[])
        and gate.receipt_valid(packet, process_exit))


def owned_native_postmasters(scratch, pg_bin, proc=Path('/proc')):
    """Identify only this uid's exact PostgreSQL binary and owned -D argument."""
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != os.geteuid():
                continue
            argv = (entry / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')
            if (Path(argv[0]).resolve() == (pg_bin / 'postgres').resolve()
                    and '-D' in argv and Path(argv[argv.index('-D') + 1]).resolve().is_relative_to(scratch.resolve())):
                found.append(int(entry.name))
        except (OSError, UnicodeError, IndexError):
            continue  # Another uid or a process that exited while being inspected.
    return sorted(found)


def installer_roles(output, pg_bin, site):
    """Owned host-native cluster, outside offline RUN and all installed-candidate credit."""
    root = Path(__file__).resolve().parents[2]
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    scratch = Path(tempfile.mkdtemp(prefix="cortex-installer-roles-"))
    home = scratch / "home"; home.mkdir()
    temporary = scratch / "temporary"; temporary.mkdir()
    source_sha256 = hashlib.sha256((root / NATIVE_ROLES_SUITE).read_bytes()).hexdigest()
    cleanup = dict(ok=True, errors=[], residual=[])
    packet, code, error = {}, 125, None
    process = None
    # Credentials, inherited DB endpoints and caller plugin settings never reach the child.
    environment = dict(PATH=str(pg_bin) + os.pathsep + os.defpath, HOME=str(home),
        TMPDIR=str(temporary), LANG="C.UTF-8", PYTHONPATH=str(site), CORTEX_PG_BIN=str(pg_bin),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
    try:
        if sys.platform != 'linux' or not Path('/proc').is_dir():
            raise RuntimeError('native installer-role cleanup requires Linux procfs')
        if os.geteuid() == 0:
            raise RuntimeError("native installer-role job must run as a non-root user")
        if not site.is_dir() or not pg_bin.is_dir() or any(
                not os.access(pg_bin / tool, os.X_OK) for tool in ("initdb", "pg_ctl", "psql")):
            raise RuntimeError("explicit PostgreSQL tool directory and test site are required")
        versions = {tool: subprocess.check_output([str(pg_bin / tool), "--version"],
                    env=environment, text=True, timeout=10).strip()
                    for tool in ("initdb", "pg_ctl", "psql")}
        (output / "tools.json").write_text(json.dumps(dict(versions=versions, uid=os.geteuid(),
            pg_bin=str(pg_bin), site=str(site), python=sys.version), indent=2) + "\n")
        command = [sys.executable, "-B", str(root / ".github/scripts/pytest_next_worker.py"),
                   "--suite", NATIVE_ROLES_SUITE, "--output", str(output / "phases.json"),
                   "--temporary", str(scratch / "worker")]
        (output / "command.json").write_text(json.dumps(command, indent=2) + "\n")
        process = subprocess.Popen(command, cwd=root, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=180)
            code = process.returncode
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=10)
            code, error = 124, "native-role worker exceeded 180 seconds"
        (output / "worker.stdout.txt").write_text(stdout)
        (output / "worker.stderr.txt").write_text(stderr)
        if (output / "phases.json").is_file():
            packet = json.loads((output / "phases.json").read_text())
        if hashlib.sha256((root / NATIVE_ROLES_SUITE).read_bytes()).hexdigest() != source_sha256:
            raise RuntimeError("native-role source drifted during execution")
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=10)
        # A timed-out fixture can leave a daemon. Only stop clusters under this owned scratch.
        for marker in scratch.rglob("postmaster.pid"):
            data = marker.parent.resolve()
            if not data.is_relative_to(scratch.resolve()):
                cleanup["errors"].append("cluster escaped owned scratch")
                continue
            try:
                stopped = subprocess.run([str(pg_bin / "pg_ctl"), "-D", str(data),
                    "-m", "immediate", "-w", "stop"], env=environment, text=True,
                    capture_output=True, timeout=30)
                with (output / "cleanup.txt").open("a") as log:
                    log.write(stopped.stdout + stopped.stderr)
                if stopped.returncode:
                    cleanup["errors"].append("pg_ctl stop returned " + str(stopped.returncode))
            except Exception as exc:
                cleanup["errors"].append(type(exc).__name__ + ": " + str(exc))
        cleanup["residual"] = [str(path.relative_to(scratch)) for path in scratch.rglob("postmaster.pid")]
        # A fixture may remove its data directory even when pg_ctl stop failed.
        # procfs still exposes the exact binary and private -D argument.
        if sys.platform == 'linux' and Path('/proc').is_dir():
            leaked = owned_native_postmasters(scratch, pg_bin)
            if leaked:
                cleanup['errors'].append('fixture leaked owned postmasters: ' + str(leaked))
            for pid in leaked:
                if pid in owned_native_postmasters(scratch, pg_bin):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            deadline = time.monotonic() + 10
            while owned_native_postmasters(scratch, pg_bin) and time.monotonic() < deadline:
                time.sleep(.1)
            cleanup['residual'].extend('pid:' + str(pid) for pid in owned_native_postmasters(scratch, pg_bin))
        cleanup["ok"] = not cleanup["errors"] and not cleanup["residual"]
        if cleanup["ok"]:
            shutil.rmtree(scratch)
    ok = error is None and native_roles_result(packet, code, source_sha256, cleanup)
    result = dict(status="PASS" if ok else "FAIL", qualification="HOST_NATIVE_POSTGRESQL_ONLY",
        installed_candidate=False, suite=NATIVE_ROLES_SUITE, source_sha256=source_sha256,
        expected=NATIVE_ROLE_NODES, process_exit=code, error=error, cleanup=cleanup,
        scratch=str(scratch), scratch_retained=not cleanup["ok"])
    (output / "admission.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest-worker", action="store_true")
    parser.add_argument("--mode", choices=["general", "certificate", "installer-roles"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pg-bin", type=Path)
    parser.add_argument("--site", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--expected-failures", type=int, default=0)
    parser.add_argument("--expected-selected", type=int)
    parser.add_argument("--bypass", action="store_true")
    parser.add_argument("targets", nargs="*")
    args = parser.parse_args()
    if args.mode == "installer-roles":
        if args.pg_bin is None or args.site is None or args.output is None:
            parser.error("installer-roles requires --pg-bin, --site and fresh --output")
        raise SystemExit(installer_roles(args.output, args.pg_bin.resolve(), args.site.resolve()))
    raise SystemExit(pytest_worker(args) if args.pytest_worker else
                     general(args.output) if args.mode == "general" else certificate(args.output))
