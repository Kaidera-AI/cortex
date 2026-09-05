from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, UploadFile


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


def upload(name: str, body: bytes, content_type: str) -> UploadFile:
    return UploadFile(
        io.BytesIO(body),
        filename=name,
        headers=Headers({"content-type": content_type}),
    )


@pytest.mark.asyncio
async def test_transcribe_route_authorises_and_streams_video_to_audio_worker(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_multimodal_audio_test")
    authorised: list[tuple[str, str, str]] = []
    proxied: dict[str, object] = {}

    async def allow_writer(project: str, agent: str, *, scope: str):
        authorised.append((project, agent, scope))

    async def forward(worker_url, **kwargs):
        proxied.update({"worker_url": worker_url, **kwargs})
        assert kwargs["filename"] == "demo.mp4"
        assert kwargs["upload"].content_type == "video/mp4"
        assert await kwargs["upload"].read() == b"video-bytes"
        result = {
            "model": "base",
            "text": "  durable transcript  ",
            "language": "en",
            "modality": "video",
            "audio_extracted": True,
        }
        return json.dumps(result).encode(), result

    monkeypatch.setattr(module, "require_registered_agent_writer", allow_writer)
    monkeypatch.setattr(module, "_forward_media_upload", forward)

    result = await module.transcribe_artifact(
        artifact=upload("demo.mp4", b"video-bytes", "video/mp4"),
        model="base",
        x_agent="kai",
        x_project="kaidera-os",
    )

    assert authorised == [("kaidera-os", "kai", "system-event")]
    assert proxied["worker_url"] == module.AUDIO_WORKER_URL
    assert proxied["path"] == "/transcribe"
    assert proxied["field"] == "audio"
    assert proxied["form"] == {"model": "base"}
    assert proxied["max_bytes"] == module.MEDIA_TRANSCRIBE_MAX_BYTES
    assert result == {
        "modality": "video",
        "content": "durable transcript",
        "model": "base",
        "language": "en",
        "audio_extracted": True,
        "extraction_method": "transcribed_video_audio",
    }


@pytest.mark.asyncio
async def test_describe_image_route_authorises_and_normalises_worker_result(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_multimodal_image_test")
    authorised: list[tuple[str, str, str]] = []

    async def allow_writer(project: str, agent: str, *, scope: str):
        authorised.append((project, agent, scope))

    async def forward(worker_url, **kwargs):
        assert worker_url == module.VISION_WORKER_URL
        assert kwargs["path"] == "/describe-image"
        assert kwargs["field"] == "image"
        assert kwargs["form"] == {
            "prompt": "Read the diagram",
            "model": "qwen3-vl:4b",
        }
        assert kwargs["filename"] == "diagram.png"
        assert kwargs["upload"].content_type == "image/png"
        assert await kwargs["upload"].read() == b"png-bytes"
        result = {"model": "qwen3-vl:4b", "description": "  API points to worker  "}
        return json.dumps(result).encode(), result

    monkeypatch.setattr(module, "require_registered_agent_writer", allow_writer)
    monkeypatch.setattr(module, "_forward_media_upload", forward)

    result = await module.describe_artifact_image(
        artifact=upload("diagram.png", b"png-bytes", "image/png"),
        prompt="Read the diagram",
        model="qwen3-vl:4b",
        x_agent="kai",
        x_project="kaidera-os",
    )

    assert authorised == [("kaidera-os", "kai", "system-event")]
    assert result == {
        "modality": "image",
        "content": "API points to worker",
        "model": "qwen3-vl:4b",
        "extraction_method": "vlm_enriched",
    }


@pytest.mark.asyncio
async def test_transcribe_route_rejects_empty_worker_output(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_multimodal_empty_test")

    async def allow_writer(_project: str, _agent: str, *, scope: str):
        assert scope == "system-event"

    async def forward(*_args, **_kwargs):
        result = {"model": "base", "text": ""}
        return json.dumps(result).encode(), result

    monkeypatch.setattr(module, "require_registered_agent_writer", allow_writer)
    monkeypatch.setattr(module, "_forward_media_upload", forward)

    with pytest.raises(HTTPException) as exc:
        await module.transcribe_artifact(
            artifact=upload("silence.wav", b"silence", "audio/wav"),
            model=None,
            x_agent="kai",
            x_project="kaidera-os",
        )

    assert exc.value.status_code == 502
    assert exc.value.detail == "audio worker returned an empty transcription"


class CountingUpload:
    def __init__(self, body: bytes, *, name: str, content_type: str):
        self._body = body
        self._position = 0
        self.filename = name
        self.content_type = content_type
        self.reads = 0

    async def read(self, size: int = -1) -> bytes:
        self.reads += 1
        if size < 0:
            chunk = self._body[self._position :]
            self._position = len(self._body)
            return chunk
        chunk = self._body[self._position : self._position + size]
        self._position += len(chunk)
        return chunk


@pytest.mark.asyncio
async def test_artifact_transcribe_returns_503_before_reading_upload(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_artifact_gate_test")
    source = CountingUpload(b"large-audio", name="voice.wav", content_type="audio/wav")

    async def allow_writer(_project: str, _agent: str, *, scope: str):
        assert scope == "system-event"

    async def gate(_worker_url: str, kind: str):
        assert kind == "audio"
        return JSONResponse(
            status_code=503,
            content={
                "detail": "audio worker unavailable: worker not deployed (profile core)",
                "reason_code": "media_worker_unavailable",
            },
        )

    async def no_proxy(*_args, **_kwargs):
        pytest.fail("a closed health gate must not proxy the upload")

    monkeypatch.setattr(module, "require_registered_agent_writer", allow_writer)
    monkeypatch.setattr(module, "_media_gate_or_503", gate)
    monkeypatch.setattr(module, "_proxy_media_upload", no_proxy)

    response = await module.transcribe_artifact(
        artifact=source,
        model=None,
        x_agent="kai",
        x_project="kaidera-os",
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    assert json.loads(response.body)["reason_code"] == "media_worker_unavailable"
    assert source.reads == 0


@pytest.mark.asyncio
async def test_artifact_describe_enforces_413_before_proxying(monkeypatch):
    module = load_module(API_MAIN_PATH, "cortex_api_artifact_cap_test")
    source = CountingUpload(b"oversized-image", name="chart.png", content_type="image/png")

    async def allow_writer(_project: str, _agent: str, *, scope: str):
        assert scope == "system-event"

    async def open_gate(_worker_url: str, _kind: str):
        return None

    async def no_proxy(*_args, **_kwargs):
        pytest.fail("an oversized upload must not reach the worker")

    monkeypatch.setattr(module, "require_registered_agent_writer", allow_writer)
    monkeypatch.setattr(module, "MEDIA_DESCRIBE_MAX_BYTES", 4)
    monkeypatch.setattr(module, "_media_gate_or_503", open_gate)
    monkeypatch.setattr(module, "_proxy_media_upload", no_proxy)

    with pytest.raises(HTTPException) as exc:
        await module.describe_artifact_image(
            artifact=source,
            prompt=None,
            model=None,
            x_agent="kai",
            x_project="kaidera-os",
        )

    assert exc.value.status_code == 413
