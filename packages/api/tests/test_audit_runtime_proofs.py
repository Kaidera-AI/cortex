"""Four source-bound audit proofs; no live API, Ollama, or container access.

Only the search deadline case needs CORTEX_AUDIT_PG_DSN, which must identify a
disposable loopback PostgreSQL. The MCP probe uses an isolated pinned SDK 2 path.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import asyncpg
import pytest


ROOT = Path(__file__).resolve().parents[3]
API_ROOT = ROOT / "packages" / "api"
MCP_SERVER = API_ROOT / "mcp_server.py"
VISION_WORKER = ROOT / "packages" / "containers" / "vision-worker" / "worker.py"
SOAK = ROOT / "packages" / "cli" / "cortex-search-soak"


def load_python(path: Path, name: str):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def api():
    sys.path.insert(0, str(API_ROOT))
    try:
        yield load_python(API_ROOT / "main.py", "cortex_api_audit_runtime")
    finally:
        sys.path.remove(str(API_ROOT))


def validated_pg_target(raw: str) -> dict[str, object]:
    parsed = urlsplit(raw)
    if parsed.query or parsed.fragment:
        raise ValueError("audit DSN query parameters and fragments are forbidden")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("audit DSN port is invalid") from exc
    host = parsed.hostname
    user = unquote(parsed.username or "")
    database = unquote(parsed.path.removeprefix("/"))
    if parsed.scheme not in {"postgres", "postgresql"} or host not in {"127.0.0.1", "localhost"} or port in {None, 5500, 8501} or not user or not database or "/" in database:
        raise ValueError("audit DSN requires an explicit non-live loopback target")
    for key, expected in (("PGHOSTADDR", "127.0.0.1"), ("PGHOST", host), ("PGPORT", str(port))):
        inherited = os.environ.get(key)
        if inherited and inherited != expected:
            raise ValueError(f"audit environment {key} disagrees with target")
    for key in ("PGSERVICE", "PGSERVICEFILE"):
        if os.environ.get(key):
            raise ValueError(f"audit environment {key} may redirect target")
    target: dict[str, object] = {"host": "127.0.0.1", "port": port, "user": user, "database": database}
    if parsed.password is not None:
        target["password"] = unquote(parsed.password)
    return target


@pytest.mark.parametrize("override", ["?hostaddr=203.0.113.10", "?port=5500"])
def test_runtime_pg_guard_rejects_routing_overrides(override):
    with pytest.raises(ValueError):
        validated_pg_target("postgresql://postgres@127.0.0.1:37407/postgres" + override)


def test_runtime_pg_guard_rejects_inherited_hostaddr(monkeypatch):
    monkeypatch.setenv("PGHOSTADDR", "203.0.113.10")
    with pytest.raises(ValueError, match="PGHOSTADDR"):
        validated_pg_target("postgresql://postgres@127.0.0.1:37407/postgres")


@pytest.fixture
def scratch_pg():
    raw = os.environ.get("CORTEX_AUDIT_PG_DSN", "")
    if not raw:
        pytest.skip("CORTEX_AUDIT_PG_DSN must name an owned disposable loopback PostgreSQL")
    target = validated_pg_target(raw)
    name = "mike_runtime_" + uuid4().hex

    async def create():
        admin = await asyncpg.connect(**target)
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
        finally:
            await admin.close()

    async def drop():
        admin = await asyncpg.connect(**target)
        try:
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        finally:
            await admin.close()

    asyncio.run(create())
    try:
        yield {**target, "database": name}
    finally:
        asyncio.run(drop())


def test_api2_003_exact_id_db_wait_obeys_total_search_budget(api, scratch_pg, monkeypatch):
    async def case():
        native = await asyncpg.connect(**scratch_pg)
        blocker = await asyncpg.connect(**scratch_pg)
        transaction = None
        release_task = None
        try:
            await native.execute("CREATE TABLE decisions(id uuid PRIMARY KEY, project text, created_at timestamptz DEFAULT now(), summary text)")
            exact_id = "aaaaaaaa-0000-0000-0000-000000000001"
            await native.execute("INSERT INTO decisions(id,project,summary) VALUES($1,'audit','matching decision')", exact_id)
            transaction = blocker.transaction()
            await transaction.start()
            await blocker.execute("LOCK TABLE decisions IN ACCESS EXCLUSIVE MODE")

            async def release_lock():
                await asyncio.sleep(0.8)
                await transaction.rollback()

            release_task = asyncio.create_task(release_lock())

            class ExactIdConn:
                async def fetchrow(self, statement, *args):
                    if "FROM decisions" in statement:
                        return await native.fetchrow(statement, *args)
                    return None

                async def fetch(self, *_args):
                    return []

                async def execute(self, statement, *_args):
                    if statement.startswith("SET statement_timeout"):
                        return await native.execute(statement)
                    return "SET"

            async def no_provider(*_args):
                return api.SearchProviderOutcome("skipped")

            monkeypatch.setattr(api, "SEARCH_TOTAL_BUDGET_MS", 100)
            monkeypatch.setattr(api, "_embed_query_outcome", no_provider)
            start = time.monotonic()
            result = await api.execute_search(
                ExactIdConn(), "audit", "aaaaaaaa", search_type="decisions", rerank=False, graph=False
            )
            elapsed = time.monotonic() - start
            assert elapsed < 0.4, f"exact-ID database wait outlived 100 ms total budget: {elapsed:.3f}s"
            assert result["results"] or any(s.startswith("exact-id:") for s in result["degraded"])
        finally:
            if release_task is not None and not release_task.done():
                release_task.cancel()
                with suppress(asyncio.CancelledError):
                    await release_task
                await transaction.rollback()
            elif release_task is not None:
                await release_task
            await native.close()
            await blocker.close()

    asyncio.run(case())


def test_vision_001_failed_pull_stream_is_not_cached():
    worker = load_python(VISION_WORKER, "cortex_vision_audit_runtime")
    model = "audit-failed-pull-only"
    worker._pulled.discard(model)

    class Tags:
        def raise_for_status(self):
            return None

        def json(self):
            return {"models": []}

    class PullStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield '{"error":"model manifest unavailable"}'

    class Client:
        pulls = 0

        async def get(self, *_args):
            return Tags()

        def stream(self, *_args, **_kwargs):
            self.pulls += 1
            return PullStream()

    async def case():
        client = Client()
        for _ in range(2):
            with suppress(Exception):
                await worker._ensure_model(client, model)
            assert model not in worker._pulled, "failed model pull was cached"
        assert client.pulls == 2, "failed pull was not retried"

    try:
        asyncio.run(case())
    finally:
        worker._pulled.discard(model)


def test_cli3_soak_interruption_cannot_pass(monkeypatch):
    soak = load_python(SOAK, "cortex_search_soak_audit_runtime")
    monkeypatch.setattr(sys, "argv", [
        "cortex-search-soak", "--project", "audit", "--duration", "600", "--concurrency", "1",
        "--abort-workers", "1", "--url", "http://127.0.0.1:1",
        "--pg-container", "synthetic-pg", "--api-container", "synthetic-api",
    ])
    monkeypatch.setattr(soak, "resolve_container_engine", lambda *_args: "podman")
    monkeypatch.setattr(soak, "admin_stats", lambda *_args: {})
    monkeypatch.setattr(soak, "container_logs", lambda *_args: "")
    monkeypatch.setattr(soak, "normal_worker", lambda *_args: None)
    monkeypatch.setattr(soak, "abort_worker", lambda *_args: None)
    calls = [0]

    def scans(_stats):
        calls[0] += 1
        return {"idx_decisions_summary_trgm": calls[0]}

    monkeypatch.setattr(soak, "trgm_scans", scans)

    class CleanStats:
        def __init__(self):
            self.lock = threading.Lock()
            self.latencies = [0.1]
            self.codes = {200: 1}
            self.shed = 0
            self.hard_5xx = 0
            self.errors = {}
            self.aborts = 1
            self.degraded = {}

    monkeypatch.setattr(soak, "Stats", CleanStats)
    ticks = iter([0.0, 600.0])
    monkeypatch.setattr(soak.time, "monotonic", lambda: next(ticks, 600.0))
    assert soak.main() == 0, "completed-duration clean control failed"

    monkeypatch.setattr(soak.time, "monotonic", lambda: 0.0)

    def interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(soak.time, "sleep", interrupt)
    assert soak.main() != 0, "interrupted sub-600-second soak reported PASS"


def test_api_runtime_001_mcp_stdio_frames_survive_watchdog():
    sdk_root = os.environ.get("CORTEX_AUDIT_MCP2_DEPS", "")
    if not sdk_root or not (Path(sdk_root) / "mcp" / "server" / "mcpserver").is_dir():
        pytest.skip("isolated pinned mcp[cli]==2.1.1 is required for this stdio proof")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": sdk_root,
        "PYTHONDONTWRITEBYTECODE": "1",
        "CORTEX_PROJECT": "audit",
        "CORTEX_API_URL": "http://127.0.0.1:1",
    }
    control_code = (
        "import asyncio,importlib.util,sys\n"
        "spec=importlib.util.spec_from_file_location('cortex_mcp_control',sys.argv[1])\n"
        "module=importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "async def idle(): await asyncio.Event().wait()\n"
        "module._stdin_watchdog=idle\n"
        "module.main()\n"
    )

    def probe(disable_watchdog: bool):
        command = (
            [sys.executable, "-c", control_code, str(MCP_SERVER)]
            if disable_watchdog else [sys.executable, str(MCP_SERVER)]
        )
        proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True, env=env,
        )
        pending = b""

        def send(message):
            proc.stdin.write((json.dumps(message) + "\n").encode())
            proc.stdin.flush()

        def receive(request_id):
            nonlocal pending
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if line:
                        response = json.loads(line)
                        if response.get("id") == request_id:
                            return response
                ready, _, _ = select.select([proc.stdout], [], [], 0.2)
                if ready:
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    pending += chunk
                if proc.poll() is not None:
                    break
            return None

        response_ids = []
        try:
            send({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "audit-stdio", "version": "0"}},
            })
            initialized = receive(1)
            if initialized and initialized.get("result", {}).get("serverInfo", {}).get("name") == "cortex":
                response_ids.append(1)
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                for request_id in (2, 3):
                    send({"jsonrpc": "2.0", "id": request_id, "method": "tools/list"})
                    listed = receive(request_id)
                    if listed and isinstance(listed.get("result", {}).get("tools"), list):
                        response_ids.append(request_id)
            return response_ids
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()

    assert probe(True) == [1, 2, 3], "pinned SDK 2 control failed without the watchdog"
    actual = [probe(False) for _ in range(3)]
    assert all(ids == [1, 2, 3] for ids in actual), f"MCP stdio frames lost with watchdog active: {actual}"
