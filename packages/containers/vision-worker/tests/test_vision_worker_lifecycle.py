from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.responses import JSONResponse


WORKER_DIR = Path(__file__).resolve().parents[1]
WORKER_PATH = WORKER_DIR / "worker.py"
ENTRYPOINT_PATH = WORKER_DIR / "entrypoint.sh"


def load_worker(name: str):
    spec = importlib.util.spec_from_file_location(name, WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeAsyncClient:
    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, _url: str):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def arm_health(worker, monkeypatch, result):
    monkeypatch.setattr(
        worker.httpx,
        "AsyncClient",
        lambda **_kwargs: FakeAsyncClient(result),
    )


@pytest.mark.asyncio
async def test_health_is_503_when_ollama_dies(monkeypatch):
    worker = load_worker("cortex_vision_worker_health_down_test")
    arm_health(worker, monkeypatch, httpx.ConnectError("connection refused"))

    response = await worker.health()

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    assert json.loads(response.body) == {
        "ok": False,
        "ollama_reachable": False,
        "error": "ConnectError: connection refused",
    }


@pytest.mark.asyncio
async def test_health_is_503_for_reachable_but_invalid_ollama(monkeypatch):
    worker = load_worker("cortex_vision_worker_health_invalid_test")
    arm_health(worker, monkeypatch, FakeResponse(200, ["not", "an", "object"]))

    response = await worker.health()

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["ok"] is False
    assert payload["ollama_reachable"] is True
    assert payload["error"] == "invalid Ollama tags response: payload is not an object"


@pytest.mark.asyncio
async def test_health_is_ok_only_with_valid_ollama_tags(monkeypatch):
    worker = load_worker("cortex_vision_worker_health_ok_test")
    arm_health(
        worker,
        monkeypatch,
        FakeResponse(200, {"models": [{"name": "qwen3-vl:4b"}]}),
    )

    assert await worker.health() == {
        "ok": True,
        "default_model": worker.DEFAULT_MODEL,
        "models_pulled": ["qwen3-vl:4b"],
    }


def make_python_executable(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)


def test_entrypoint_exits_and_terminates_adapter_when_ollama_dies(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ollama_started = tmp_path / "ollama-started"
    ollama_release = tmp_path / "ollama-release"
    uvicorn_started = tmp_path / "uvicorn-started"
    uvicorn_terminated = tmp_path / "uvicorn-terminated"

    make_python_executable(
        fake_bin / "ollama",
        """import os
from pathlib import Path
import time

Path(os.environ["FAKE_OLLAMA_STARTED"]).touch()
release = Path(os.environ["FAKE_OLLAMA_RELEASE"])
while not release.exists():
    time.sleep(0.01)
raise SystemExit(23)
""",
    )
    make_python_executable(
        fake_bin / "curl",
        """import os
from pathlib import Path

raise SystemExit(0 if Path(os.environ["FAKE_OLLAMA_STARTED"]).exists() else 22)
""",
    )
    make_python_executable(
        fake_bin / "uvicorn",
        """import os
from pathlib import Path
import signal
import time

terminated = Path(os.environ["FAKE_UVICORN_TERMINATED"])

def stop(_signum, _frame):
    terminated.touch()
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
Path(os.environ["FAKE_UVICORN_STARTED"]).touch()
while True:
    time.sleep(0.05)
""",
    )

    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_OLLAMA_STARTED": str(ollama_started),
        "FAKE_OLLAMA_RELEASE": str(ollama_release),
        "FAKE_UVICORN_STARTED": str(uvicorn_started),
        "FAKE_UVICORN_TERMINATED": str(uvicorn_terminated),
    }
    process = subprocess.Popen(
        ["bash", str(ENTRYPOINT_PATH)],
        cwd=WORKER_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not uvicorn_started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert uvicorn_started.exists(), "entrypoint did not start the adapter"

        ollama_release.touch()
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)

    assert process.returncode == 1
    assert "ollama ready" in stdout
    assert "ollama exited with status 23" in stderr
    assert uvicorn_terminated.exists()
