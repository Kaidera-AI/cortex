"""Contract tests for the audio/vision media worker proxy routes.

The audio/vision workers are opt-in compose-profile services, so the API must:
- answer GET /media/status with the exact contract shape and never raise;
- fail closed with 503 + reason_code before consuming an upload when the
  worker is unreachable or unhealthy;
- pass a healthy worker's JSON through unchanged on 2xx;
- map worker failures and timeouts to typed 502/503 — never a 500.

Transport is mocked at ``module.httpx.AsyncClient`` following the repo's
existing pattern (test_local_search_worker.py).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.responses import JSONResponse

API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    module_root = str(path.parent)
    added_to_path = module_root not in sys.path
    if added_to_path:
        sys.path.insert(0, module_root)
    try:
        spec.loader.exec_module(module)
    finally:
        if added_to_path:
            sys.path.remove(module_root)
    return module


class FakeHealthResponse:
    def __init__(self, payload=None, *, status_code: int = 200, body: bytes | None = None):
        self.status_code = status_code
        self._body = body if body is not None else json.dumps(payload).encode("utf-8")

    def json(self):
        return json.loads(self._body)


class FakeStreamResponse:
    def __init__(self, status_code: int = 200, body: bytes = b""):
        self.status_code = status_code
        self._body = body

    async def aiter_bytes(self):
        yield self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeStreamContext:
    """Async context manager standing in for client.stream(...)."""

    def __init__(self, response_or_exc):
        self._result = response_or_exc

    async def __aenter__(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    async def __aexit__(self, *_args):
        return False


class FakeAsyncClient:
    def __init__(self, health_by_url, upload_result, calls, **kwargs):
        self.health_by_url = health_by_url
        self.upload_result = upload_result
        self.calls = calls
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, "kwargs": kwargs})
        for base, result in self.health_by_url.items():
            if url.startswith(base):
                if isinstance(result, Exception):
                    raise result
                return result
        raise httpx.ConnectError("no route to media worker")

    def stream(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        return FakeStreamContext(self.upload_result)


class FakeUpload:
    def __init__(self, data: bytes, filename="clip.wav", content_type="audio/wav"):
        self.data = data
        self.filename = filename
        self.content_type = content_type
        self.reads = 0
        self._pos = 0

    async def read(self, size: int = -1) -> bytes:
        self.reads += 1
        if size is None or size < 0:
            chunk = self.data[self._pos:]
            self._pos = len(self.data)
        else:
            chunk = self.data[self._pos:self._pos + size]
            self._pos += len(chunk)
        return chunk


def arm_transport(module, monkeypatch, *, health=None, upload=None):
    """Replace module.httpx.AsyncClient with the scripted fake."""
    calls: list[dict] = []
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: FakeAsyncClient(health or {}, upload, calls, **kwargs),
    )
    return calls


def allow_writer(module, monkeypatch, recorded: list):
    async def fake_require(project, agent, *, scope="work"):
        recorded.append((project, agent, scope))

    monkeypatch.setattr(module, "require_registered_agent_writer", fake_require)


def healthy_payload():
    return FakeHealthResponse({"ok": True, "default_model": "base"})


@pytest.mark.asyncio
async def test_media_status_contract_shape_when_workers_down(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_media_status_down_test")
    arm_transport(module, monkeypatch)  # every health probe gets ConnectError

    result = await module.media_status(x_project="")

    assert set(result.keys()) == {"audio", "vision"}
    for name in ("audio", "vision"):
        entry = result[name]
        assert set(entry.keys()) == {"reachable", "ok", "detail"}
        assert entry["reachable"] is False
        assert entry["ok"] is False
        assert entry["detail"] == "worker not deployed (profile core)"


@pytest.mark.asyncio
async def test_media_status_reports_reachable_but_not_ok(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_media_status_not_ok_test")
    arm_transport(
        module,
        monkeypatch,
        health={
            module.AUDIO_WORKER_URL: healthy_payload(),
            module.VISION_WORKER_URL: FakeHealthResponse({"ok": False}),
        },
    )

    result = await module.media_status(x_project="")

    assert result["audio"] == {"reachable": True, "ok": True, "detail": "healthy"}
    assert result["vision"]["reachable"] is True
    assert result["vision"]["ok"] is False
    assert result["vision"]["detail"] == "health not ok"


@pytest.mark.asyncio
async def test_media_status_treats_timeout_and_bad_status_as_degraded(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_media_status_timeout_test")
    arm_transport(
        module,
        monkeypatch,
        health={
            module.AUDIO_WORKER_URL: httpx.ReadTimeout("health probe timed out"),
            module.VISION_WORKER_URL: FakeHealthResponse({"error": "boom"}, status_code=500),
        },
    )

    result = await module.media_status(x_project="")

    assert result["audio"] == {
        "reachable": False,
        "ok": False,
        "detail": "worker not deployed (profile core)",
    }
    assert result["vision"]["reachable"] is True
    assert result["vision"]["ok"] is False
    assert result["vision"]["detail"] == "health returned 500"


@pytest.mark.asyncio
async def test_media_status_still_validates_a_supplied_project(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_media_status_scope_test")
    arm_transport(module, monkeypatch)

    with pytest.raises(module.HTTPException) as exc_info:
        await module.media_status(x_project="NOT A PROJECT")

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_transcribe_health_gate_fails_closed_before_touching_upload(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_media_gate_closed_test")
    calls = arm_transport(module, monkeypatch)  # health unreachable
    writer_calls: list = []
    allow_writer(module, monkeypatch, writer_calls)
    upload = FakeUpload(b"RIFF-audio-bytes")

    response = await module.media_transcribe(
        audio=upload, model=None, x_agent="kai", x_project="kaidera-os"
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["reason_code"] == "media_worker_unavailable"
    assert "audio worker unavailable" in body["detail"]
    assert writer_calls, "the registered-writer guard must run before the gate"
    assert upload.reads == 0, "the upload must not be consumed when the gate fails"
    assert not any(call["method"] == "POST" for call in calls)


@pytest.mark.asyncio
async def test_transcribe_passes_worker_body_through_unchanged(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_media_transcribe_ok_test")
    worker_body = json.dumps(
        {"model": "medium", "text": "Hello from Whisper.", "language": "en"}
    ).encode("utf-8")
    calls = arm_transport(
        module,
        monkeypatch,
        health={module.AUDIO_WORKER_URL: healthy_payload()},
        upload=FakeStreamResponse(200, worker_body),
    )
    writer_calls: list = []
    allow_writer(module, monkeypatch, writer_calls)
    upload = FakeUpload(b"RIFF-audio-bytes", filename="note.wav", content_type="audio/wav")

    response = await module.media_transcribe(
        audio=upload, model="medium", x_agent="kai", x_project="kaidera-os"
    )

    assert response.status_code == 200
    assert response.media_type == "application/json"
    assert response.body == worker_body
    post = next(call for call in calls if call["method"] == "POST")
    assert post["url"] == f"{module.AUDIO_WORKER_URL}/transcribe"
    field, payload = next(iter(post["kwargs"]["files"].items()))
    assert field == "audio"
    assert payload == ("note.wav", b"RIFF-audio-bytes", "audio/wav")
    assert post["kwargs"]["data"] == {"model": "medium"}


@pytest.mark.asyncio
async def test_transcribe_maps_worker_error_status_to_502(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_media_transcribe_502_test")
    arm_transport(
        module,
        monkeypatch,
        health={module.AUDIO_WORKER_URL: healthy_payload()},
        upload=FakeStreamResponse(500, b"worker exploded"),
    )
    allow_writer(module, monkeypatch, [])

    with pytest.raises(module.HTTPException) as exc_info:
        await module.media_transcribe(
            audio=FakeUpload(b"RIFF"), model=None, x_agent="kai", x_project="kaidera-os"
        )

    assert exc_info.value.status_code == 502
    assert "status 500" in exc_info.value.detail


@pytest.mark.asyncio
async def test_media_timeouts_surface_as_typed_errors_never_500(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_media_timeout_test")

    # A timed-out health probe is "unreachable": the gate returns a typed 503.
    calls = arm_transport(
        module,
        monkeypatch,
        health={module.AUDIO_WORKER_URL: httpx.ReadTimeout("probe timeout")},
        upload=FakeStreamResponse(200, b"{}"),
    )
    allow_writer(module, monkeypatch, [])
    gated = await module.media_transcribe(
        audio=FakeUpload(b"RIFF"), model=None, x_agent="kai", x_project="kaidera-os"
    )
    assert isinstance(gated, JSONResponse)
    assert gated.status_code == 503
    assert gated.status_code != 500

    # A mid-flight upload timeout maps to a typed 502, never a 500.
    arm_transport(
        module,
        monkeypatch,
        health={module.AUDIO_WORKER_URL: healthy_payload()},
        upload=httpx.ReadTimeout("worker went quiet"),
    )
    with pytest.raises(module.HTTPException) as exc_info:
        await module.media_transcribe(
            audio=FakeUpload(b"RIFF"), model=None, x_agent="kai", x_project="kaidera-os"
        )
    assert exc_info.value.status_code == 502
    assert exc_info.value.status_code != 500


@pytest.mark.asyncio
async def test_describe_image_gate_and_passthrough(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_media_describe_test")
    allow_writer(module, monkeypatch, [])

    # Gate closed: vision worker down -> 503, upload untouched.
    calls = arm_transport(module, monkeypatch)
    upload = FakeUpload(b"\x89PNG-bytes", filename="chart.png", content_type="image/png")
    gated = await module.media_describe_image(
        image=upload, prompt=None, model=None, x_agent="kai", x_project="kaidera-os"
    )
    assert isinstance(gated, JSONResponse)
    assert gated.status_code == 503
    assert json.loads(gated.body)["reason_code"] == "media_worker_unavailable"
    assert upload.reads == 0

    # Gate open: worker JSON passes through verbatim, form fields forwarded.
    worker_body = json.dumps(
        {"model": "qwen3-vl:4b", "description": "A bar chart with three bars."}
    ).encode("utf-8")
    calls = arm_transport(
        module,
        monkeypatch,
        health={module.VISION_WORKER_URL: FakeHealthResponse({"ok": True})},
        upload=FakeStreamResponse(200, worker_body),
    )
    response = await module.media_describe_image(
        image=FakeUpload(b"\x89PNG-bytes", filename="chart.png", content_type="image/png"),
        prompt="Focus on the axes.",
        model="qwen3-vl:4b",
        x_agent="kai",
        x_project="kaidera-os",
    )
    assert response.status_code == 200
    assert response.body == worker_body
    post = next(call for call in calls if call["method"] == "POST")
    assert post["url"] == f"{module.VISION_WORKER_URL}/describe-image"
    field, payload = next(iter(post["kwargs"]["files"].items()))
    assert field == "image"
    assert payload == ("chart.png", b"\x89PNG-bytes", "image/png")
    assert post["kwargs"]["data"] == {"prompt": "Focus on the axes.", "model": "qwen3-vl:4b"}


@pytest.mark.asyncio
async def test_transcribe_enforces_upload_byte_cap_before_proxying(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_media_upload_cap_test")
    monkeypatch.setattr(module, "MEDIA_TRANSCRIBE_MAX_BYTES", 16)
    calls = arm_transport(
        module,
        monkeypatch,
        health={module.AUDIO_WORKER_URL: healthy_payload()},
        upload=FakeStreamResponse(200, b"{}"),
    )
    allow_writer(module, monkeypatch, [])

    with pytest.raises(module.HTTPException) as exc_info:
        await module.media_transcribe(
            audio=FakeUpload(b"x" * 64), model=None, x_agent="kai", x_project="kaidera-os"
        )

    assert exc_info.value.status_code == 413
    assert not any(call["method"] == "POST" for call in calls)


@pytest.mark.asyncio
async def test_artifact_transcribe_enforces_worker_response_cap(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_artifact_response_cap_test")
    monkeypatch.setattr(module, "MEDIA_WORKER_RESPONSE_MAX_BYTES", 8)
    arm_transport(
        module,
        monkeypatch,
        health={module.AUDIO_WORKER_URL: healthy_payload()},
        upload=FakeStreamResponse(200, b'{"text":"too large"}'),
    )
    allow_writer(module, monkeypatch, [])

    with pytest.raises(module.HTTPException) as exc_info:
        await module.transcribe_artifact(
            artifact=FakeUpload(b"RIFF"),
            model=None,
            x_agent="kai",
            x_project="kaidera-os",
        )

    assert exc_info.value.status_code == 502
    assert "response exceeded" in exc_info.value.detail
