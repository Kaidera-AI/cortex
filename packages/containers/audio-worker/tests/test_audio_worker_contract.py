from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers, UploadFile


WORKER_PATH = Path(__file__).resolve().parents[1] / "worker.py"


def load_worker(name: str):
    spec = importlib.util.spec_from_file_location(name, WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def upload(name: str, body: bytes, content_type: str) -> UploadFile:
    return UploadFile(
        io.BytesIO(body),
        filename=name,
        headers=Headers({"content-type": content_type}),
    )


def test_extract_audio_track_uses_ffmpeg_mono_16khz_contract(tmp_path, monkeypatch):
    worker = load_worker("cortex_audio_worker_ffmpeg_test")
    source = tmp_path / "demo.mp4"
    target = tmp_path / "demo.wav"
    source.write_bytes(b"video")
    seen: list[str] = []

    def fake_run(args, **kwargs):
        seen.extend(args)
        assert kwargs == {"check": False, "capture_output": True, "text": True}
        target.write_bytes(b"wav")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    worker._extract_audio_track(str(source), str(target))

    assert seen == [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(target),
    ]
    assert target.read_bytes() == b"wav"


@pytest.mark.asyncio
async def test_video_transcription_extracts_audio_and_cleans_temporary_files(monkeypatch):
    worker = load_worker("cortex_audio_worker_video_test")
    paths: dict[str, str] = {}

    def fake_extract(source: str, target: str):
        paths["source"] = source
        paths["target"] = target
        assert Path(source).read_bytes() == b"video"
        Path(target).write_bytes(b"wav")

    class FakeModel:
        def transcribe(self, path: str):
            assert path == paths["target"]
            assert Path(path).read_bytes() == b"wav"
            return {"text": "video transcript", "language": "en"}

    monkeypatch.setattr(worker, "_extract_audio_track", fake_extract)
    monkeypatch.setattr(worker, "_get_model", lambda _name: FakeModel())

    result = await worker.transcribe(
        audio=upload("demo.webm", b"video", "video/webm"),
        model="base",
    )

    assert result == {
        "model": "base",
        "text": "video transcript",
        "language": "en",
        "modality": "video",
        "audio_extracted": True,
    }
    assert not Path(paths["source"]).exists()
    assert not Path(paths["target"]).exists()


@pytest.mark.asyncio
async def test_audio_transcription_does_not_invoke_ffmpeg_extraction(monkeypatch):
    worker = load_worker("cortex_audio_worker_audio_test")

    class FakeModel:
        def transcribe(self, path: str):
            assert Path(path).suffix == ".wav"
            return {"text": "audio transcript", "language": "en"}

    monkeypatch.setattr(
        worker,
        "_extract_audio_track",
        lambda *_args: pytest.fail("audio input must not run video extraction"),
    )
    monkeypatch.setattr(worker, "_get_model", lambda _name: FakeModel())

    result = await worker.transcribe(
        audio=upload("voice.wav", b"audio", "audio/wav"),
        model=None,
    )

    assert result["modality"] == "audio"
    assert result["audio_extracted"] is False
    assert result["text"] == "audio transcript"


def test_concurrent_first_requests_load_same_model_once(monkeypatch):
    worker = load_worker("cortex_audio_worker_model_lock_test")
    loaded_model = object()
    calls = 0
    calls_guard = threading.Lock()

    def load_model(name: str, *, download_root: str):
        nonlocal calls
        assert name == "base"
        assert download_root == worker.CACHE_DIR
        with calls_guard:
            calls += 1
        # Release the GIL long enough for every caller to reach the model-load
        # boundary; without the per-model lock, this deterministically races.
        time.sleep(0.05)
        return loaded_model

    monkeypatch.setitem(sys.modules, "whisper", SimpleNamespace(load_model=load_model))

    with ThreadPoolExecutor(max_workers=4) as pool:
        models = list(pool.map(worker._get_model, ["base"] * 4))

    assert calls == 1
    assert all(model is loaded_model for model in models)


def test_model_cache_rejects_every_nonconfigured_model():
    worker = load_worker("cortex_audio_worker_model_allowlist_test")

    with pytest.raises(ValueError, match="configured Whisper model"):
        worker._get_model("small")

    assert worker._model_cache == {}


@pytest.mark.asyncio
async def test_transcription_rejects_nonconfigured_model_before_writing_upload(
    monkeypatch,
):
    worker = load_worker("cortex_audio_worker_model_request_test")
    monkeypatch.setattr(
        worker.tempfile,
        "NamedTemporaryFile",
        lambda **_kwargs: pytest.fail("rejected model must not write the upload"),
    )

    with pytest.raises(worker.HTTPException) as exc_info:
        await worker.transcribe(
            audio=upload("voice.wav", b"audio", "audio/wav"),
            model="small",
        )

    assert exc_info.value.status_code == 422
    assert worker._model_cache == {}


@pytest.mark.asyncio
async def test_model_loading_and_inference_share_the_same_capacity_slot(monkeypatch):
    worker = load_worker("cortex_audio_worker_load_inference_slot_test")
    inference_started = threading.Event()
    release_inference = threading.Event()
    get_calls = 0
    load_overlapped_inference = False

    class FakeModel:
        def transcribe(self, _path: str):
            inference_started.set()
            assert release_inference.wait(1.0)
            return {"text": "bounded", "language": "en"}

    def fake_get_model(_name: str):
        nonlocal get_calls, load_overlapped_inference
        get_calls += 1
        if inference_started.is_set() and not release_inference.is_set():
            load_overlapped_inference = True
        return FakeModel()

    monkeypatch.setattr(worker, "_get_model", fake_get_model)
    monkeypatch.setattr(worker, "_inference_slots", asyncio.Semaphore(1))

    first = asyncio.create_task(
        worker.transcribe(
            audio=upload("first.wav", b"first", "audio/wav"),
            model=None,
        )
    )
    assert await asyncio.to_thread(inference_started.wait, 1.0)
    second = asyncio.create_task(
        worker.transcribe(
            audio=upload("second.wav", b"second", "audio/wav"),
            model=None,
        )
    )
    await asyncio.sleep(0.05)
    calls_while_first_inference_active = get_calls
    release_inference.set()
    results = await asyncio.gather(first, second)

    assert calls_while_first_inference_active == 1
    assert get_calls == 2
    assert load_overlapped_inference is False
    assert [result["text"] for result in results] == ["bounded", "bounded"]


@pytest.mark.asyncio
async def test_concurrent_transcriptions_respect_inference_bound(monkeypatch):
    worker = load_worker("cortex_audio_worker_inference_bound_test")
    active = 0
    peak = 0
    counter_guard = threading.Lock()

    class FakeModel:
        def transcribe(self, _path: str):
            nonlocal active, peak
            with counter_guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with counter_guard:
                active -= 1
            return {"text": "bounded", "language": "en"}

    monkeypatch.setattr(worker, "_get_model", lambda _name: FakeModel())
    monkeypatch.setattr(worker, "_inference_slots", asyncio.Semaphore(1))

    results = await asyncio.gather(
        worker.transcribe(
            audio=upload("first.wav", b"first", "audio/wav"),
            model="base",
        ),
        worker.transcribe(
            audio=upload("second.wav", b"second", "audio/wav"),
            model="base",
        ),
    )

    assert peak == 1
    assert [result["text"] for result in results] == ["bounded", "bounded"]


@pytest.mark.asyncio
async def test_health_reports_loaded_models_and_inference_bound():
    worker = load_worker("cortex_audio_worker_health_capacity_test")
    worker._model_cache["base"] = object()

    result = await worker.health()

    assert result["loaded"] == ["base"]
    assert result["max_inference_concurrency"] == worker.MAX_INFERENCE_CONCURRENCY
