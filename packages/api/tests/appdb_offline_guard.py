"""Explicit pytest plugin for offline app-DB boundary/registration consumers.

Load with -p appdb_offline_guard, --noconftest and plugin autoload disabled.
This process-local guard must be installed before the real API module imports.
It is not a production configuration or a fake implementation of the API.
"""

import os
import socket
import sys


def denied(*_args, **_kwargs):
    raise AssertionError("offline consumer test forbids external I/O")


async def denied_async(*_args, **_kwargs):
    denied()


def pytest_configure(config):
    if not config.getoption("noconftest"):
        raise RuntimeError("offline consumer tests require --noconftest")
    for key in list(os.environ):
        if key.startswith(("CORTEX_", "HARNESS_", "OPENKAI_", "PG")):
            del os.environ[key]

    import asyncpg
    import httpx

    asyncpg.connect = denied_async
    asyncpg.create_pool = denied
    httpx.Client.send = denied
    httpx.AsyncClient.send = denied_async
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = denied

    def audit(event, args):
        if event == "open" and isinstance(args[0], (str, bytes)):
            name = os.fsdecode(args[0]).rsplit("/", 1)[-1]
            if name == ".env" or name.startswith(".env.") or name in (
                "runtime.env", "provider.env",
            ):
                raise AssertionError("offline consumer test forbids private configuration reads")
        if event in ("subprocess.Popen", "os.system"):
            denied()

    sys.addaudithook(audit)
