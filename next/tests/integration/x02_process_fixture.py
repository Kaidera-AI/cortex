"""Real owned child processes and a synthetic independent PG consumer; TEST ONLY."""

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import signal
import sys
from urllib.parse import urlsplit
from uuid import UUID

import asyncpg
from cortex_core.conductor.supervisor import LeaseBusy, StaleLease, Supervisor


class FixtureError(RuntimeError):
    """A fixture/protocol failure is a test error, never a semantic mutant kill."""


async def eventually(predicate, seconds=5):
    deadline = asyncio.get_running_loop().time() + seconds
    while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
        try:
            async with asyncio.timeout(remaining):
                result = predicate()
                if hasattr(result, "__await__"):
                    result = await result
        except TimeoutError:
            return False
        if result and asyncio.get_running_loop().time() < deadline:
            return True
        await asyncio.sleep(0.01)
    return False


def fixture_connection(dsn):
    parsed = urlsplit(dsn)
    if (parsed.scheme != "postgresql" or parsed.hostname != "127.0.0.1"
            or parsed.username != "conductor_runtime" or parsed.path != "/search_test"
            or parsed.query or parsed.fragment or not parsed.port
            or parsed.port in {5432, 5499, 5500, 8501}):
        raise ValueError("Only the disposable synthetic runner connection is accepted")


def observation(value):
    print("X02_PROCESS_RECEIPT=" + json.dumps(value, sort_keys=True), flush=True)


def emit(value):
    print(json.dumps({"pid": os.getpid(), **value}, sort_keys=True), flush=True)


class Child:
    def __init__(self, process):
        self.process = process
        self.pid = process.pid
        self.receipt = None
        self.fence = None

    @classmethod
    async def spawn(cls, dsn, installation, *, lease_seconds=0.5, mode="manual", crash=False):
        fixture_connection(dsn)
        if (not isinstance(installation, UUID) or mode not in {"manual", "heartbeat"}
                or type(lease_seconds) not in {float, int} or not 0 < lease_seconds <= 2
                or type(crash) is not bool):
            raise ValueError("Invalid bounded child fixture arguments")
        root = Path(__file__).resolve().parents[2]
        env = {"PATH": os.environ.get("PATH", ""), "SEARCH_TEST_DSN": dsn,
               "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "tests"),
                                              str(root / "tests/integration")])}
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).resolve()), "--child", str(installation),
            "--lease", str(lease_seconds), "--mode", mode, *(["--crash"] if crash else []),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, limit=8192,
        )
        child = cls(process)
        try:
            child.receipt = await child.read()
            child.fence = child.receipt.get("fence")
            if child.receipt["kind"] != "ready":
                await asyncio.wait_for(process.wait(), 2)
            return child
        except BaseException:
            await child.force_reap()
            raise

    async def read(self):
        raw = await asyncio.wait_for(self.process.stdout.readline(), 5)
        try:
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get("pid") != self.pid
                    or value.get("kind") not in {"ready", "lease_busy", "deliberate_crash",
                                                 "intent", "stale", "stopped"}):
                raise ValueError()
        except (ValueError, TypeError):
            raise FixtureError("Child did not supply a valid fixture receipt") from None
        observation(value)
        return value

    async def request(self, kind, **values):
        if self.process.returncode is not None:
            raise FixtureError("Request addressed to an exited fixture child")
        self.process.stdin.write((json.dumps({"kind": kind, **values}) + "\n").encode())
        await self.process.stdin.drain()
        return await self.read()

    async def kill(self):
        if self.process.returncode is None:
            self.process.kill()
        await asyncio.wait_for(self.process.wait(), 2)
        observation({"kind": "killed", "pid": self.pid, "returncode": self.process.returncode})

    async def force_reap(self):
        if self.process.returncode is None:
            self.process.kill()
        await asyncio.wait_for(self.process.wait(), 2)

    async def close(self):
        try:
            if self.process.returncode is None:
                self.process.stdin.write(b'{"kind":"stop"}\n')
                await self.process.stdin.drain()
                await asyncio.wait_for(self.process.wait(), 3)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            await self.force_reap()
        finally:
            if self.process.stdin:
                self.process.stdin.close()
            await self.force_reap()
        observation({"kind": "reaped", "pid": self.pid, "returncode": self.process.returncode})


class Manager:
    """Test restart policy; process plumbing cannot restart or write Core."""

    def __init__(self, dsn, installation, *, lease_seconds=0.5, max_restarts=3, crash=False):
        if type(max_restarts) is not int or not 0 <= max_restarts <= 3:
            raise ValueError("Restart attempts must be bounded")
        self.dsn, self.installation = dsn, installation
        self.lease_seconds, self.max_restarts, self.crash = lease_seconds, max_restarts, crash
        self.state = "new"
        self.children, self.events = [], []
        self.child = self.task = None
        self.closing = self.requested_stop = False
        self.reported_failure = False

    async def spawn(self):
        child = await Child.spawn(self.dsn, self.installation, lease_seconds=self.lease_seconds,
                                  mode="heartbeat", crash=self.crash)
        self.child = child
        self.children.append(child)
        self.state = child.receipt["kind"]
        return child.receipt

    async def start(self):
        if self.state != "new":
            raise FixtureError("Manager cannot be started twice")
        receipt = await self.spawn()
        self.task = asyncio.create_task(self.watch())
        return receipt

    async def watch(self):
        restarts = 0
        while not self.closing:
            await self.child.process.wait()
            event = {"kind": "exit", "pid": self.child.pid,
                     "returncode": self.child.process.returncode}
            self.events.append(event)
            observation(event)
            if self.requested_stop:
                self.state = "stopped"
                return
            if restarts >= self.max_restarts:
                self.state = "exhausted"
                return
            restarts += 1
            self.state = "backoff"
            await asyncio.sleep(self.lease_seconds)
            if self.closing or self.requested_stop:
                self.state = "stopped"
                return
            await self.spawn()

    async def request_stop(self):
        self.requested_stop = True
        await self.child.close()
        await asyncio.wait_for(self.task, 5)

    async def close(self):
        self.closing = True
        results = []
        if self.task:
            self.task.cancel()
            results.extend(await asyncio.gather(self.task, return_exceptions=True))
        results.extend(await asyncio.gather(*(child.close() for child in self.children),
                                            return_exceptions=True))
        self.state = "closed"
        failures = [result for result in results if isinstance(result, BaseException)
                    and not isinstance(result, asyncio.CancelledError)]
        if failures and not self.reported_failure:
            self.reported_failure = True
            raise FixtureError("Background fixture failure observed after owned cleanup") from None


class Consumer:
    """Independent synthetic data protocol; no lease or Conductor observation."""

    def __init__(self, pool, *, pause_at=None):
        if pause_at not in {None, "normal", "shadow", "pre_checkpoint"}:
            raise ValueError("Unknown consumer fixture phase")
        self.pool, self.pause_at = pool, pause_at
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()

    async def barrier(self, phase):
        if self.pause_at == phase:
            self.entered.set()
            await asyncio.wait_for(self.resume.wait(), 5)

    async def advance(self):
        async with self.pool.acquire(timeout=2) as conn:
            async with conn.transaction():
                checkpoint = await conn.fetchval("SELECT seq FROM public.x02_checkpoint WHERE id=1 FOR UPDATE")
                rows = await conn.fetch("SELECT seq,record_id,digest FROM public.x02_feed WHERE seq>$1 ORDER BY seq LIMIT 16", checkpoint)
                await self.barrier("normal")
                await self.barrier("shadow")
                for row in rows:
                    await conn.execute("INSERT INTO public.x02_sink VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
                                       row["seq"], row["record_id"], row["digest"])
                await self.barrier("pre_checkpoint")
                if rows:
                    await conn.execute("UPDATE public.x02_checkpoint SET seq=$1 WHERE id=1", rows[-1]["seq"])
                observation({"kind": "consumer_commit", "rows": len(rows),
                             "checkpoint": rows[-1]["seq"] if rows else checkpoint})
                return len(rows)


async def intent(supervisor, delay):
    entered = False

    async def action(conn, fence):
        nonlocal entered
        await conn.execute("INSERT INTO public.test_controls VALUES($1)", fence)
        entered = True
        await asyncio.sleep(delay)

    try:
        await supervisor.guarded(action)
        return {"kind": "intent", "entered": entered, "fence": supervisor.fence}
    except StaleLease:
        health = await supervisor.health()
        return {"kind": "stale", "entered": entered,
                "lease_expired": health["state"] == "supervisor_down"}


async def commands(supervisor, stop, heartbeat):
    reader = asyncio.StreamReader(limit=8192)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    stopped = asyncio.create_task(stop.wait())
    pending = None
    try:
        while not stop.is_set():
            pending = asyncio.create_task(reader.readline())
            choices = {pending, stopped} | ({heartbeat} if heartbeat else set())
            done, _ = await asyncio.wait(choices, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat and heartbeat in done:
                await heartbeat  # An unexpected heartbeat failure must remain a failure.
                break
            if stopped in done:
                break
            raw = await pending
            if not raw:
                break
            value = json.loads(raw)
            if value == {"kind": "stop"}:
                break
            if not isinstance(value, dict) or set(value) - {"kind", "delay"} or value.get("kind") != "intent":
                raise FixtureError("Invalid fixture command")
            delay = value.get("delay", 0)
            if type(delay) not in {int, float} or not math.isfinite(delay) or not 0 <= delay <= 2:
                raise FixtureError("Invalid fixture delay")
            emit(await intent(supervisor, delay))
    finally:
        stop.set()
        for task in [stopped, pending]:
            if task:
                task.cancel()
        await asyncio.gather(*[task for task in [stopped, pending] if task], return_exceptions=True)
        transport.close()


async def worker(installation, lease_seconds, mode, crash):
    dsn = os.environ["SEARCH_TEST_DSN"]
    fixture_connection(dsn)
    if crash:
        emit({"kind": "deliberate_crash"})
        return 70
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    supervisor = Supervisor(pool, installation, lease_seconds=lease_seconds)
    stop, heartbeat = asyncio.Event(), None
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    try:
        if mode == "heartbeat":
            heartbeat = asyncio.create_task(supervisor.run(stop))
            if not await eventually(lambda: supervisor.fence is not None or heartbeat.done(), 3):
                raise FixtureError("Fixture heartbeat did not start")
            if heartbeat.done():
                await heartbeat
        else:
            await supervisor.acquire()
        emit({"kind": "ready", "fence": supervisor.fence})
        await commands(supervisor, stop, heartbeat)
        return 0
    except LeaseBusy:
        emit({"kind": "lease_busy"})
        return 75
    finally:
        stop.set()
        if heartbeat:
            await asyncio.gather(heartbeat, return_exceptions=True)
        else:
            try:
                await supervisor.release()
            except StaleLease:
                pass
        await pool.close()
        loop.remove_signal_handler(signal.SIGTERM)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=UUID, required=True)
    parser.add_argument("--lease", type=float, required=True)
    parser.add_argument("--mode", choices=["manual", "heartbeat"], required=True)
    parser.add_argument("--crash", action="store_true")
    args = parser.parse_args()
    try:
        code = asyncio.run(worker(args.child, args.lease, args.mode, args.crash))
    except Exception:
        emit({"kind": "fixture_error"})
        code = 80
    raise SystemExit(code)
