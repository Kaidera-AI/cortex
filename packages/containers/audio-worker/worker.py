"""cortex-audio-worker — internal audio and video transcription API."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool


CACHE_DIR = os.environ.get("WHISPER_CACHE_DIR", "/var/lib/cortex/models/whisper")
DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "base").strip()
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}

if not DEFAULT_MODEL:
    raise RuntimeError("WHISPER_MODEL must name exactly one model")


def _inference_concurrency() -> int:
    raw = os.environ.get("WHISPER_MAX_INFERENCE_CONCURRENCY", "1")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            "WHISPER_MAX_INFERENCE_CONCURRENCY must be an integer from 1 to 4"
        ) from exc
    if not 1 <= value <= 4:
        raise RuntimeError(
            "WHISPER_MAX_INFERENCE_CONCURRENCY must be an integer from 1 to 4"
        )
    return value


MAX_INFERENCE_CONCURRENCY = _inference_concurrency()

app = FastAPI(title="cortex-audio-worker", version="0.1.0")
_model_cache: dict[str, object] = {}
_model_cache_guard = threading.Lock()
_model_load_lock = threading.Lock()
_inference_slots = asyncio.Semaphore(MAX_INFERENCE_CONCURRENCY)


def _get_model(name: str):
    if name != DEFAULT_MODEL:
        raise ValueError("audio worker can load only its configured Whisper model")
    with _model_cache_guard:
        cached = _model_cache.get(name)
    if cached is not None:
        return cached

    # ``transcribe`` runs this function in Starlette's thread pool. Serialize
    # every cache miss, including requests for different model names: loading
    # multiple Whisper models at once can exceed the worker's memory budget.
    with _model_load_lock:
        with _model_cache_guard:
            cached = _model_cache.get(name)
        if cached is not None:
            return cached

        import whisper

        loaded = whisper.load_model(name, download_root=CACHE_DIR)
        with _model_cache_guard:
            _model_cache[name] = loaded
        return loaded


def _extract_audio_track(source_path: str, target_path: str) -> None:
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            source_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            target_path,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if (
        result.returncode != 0
        or not os.path.isfile(target_path)
        or os.path.getsize(target_path) == 0
    ):
        detail = (result.stderr or "no audio track produced")[-500:]
        raise HTTPException(422, f"ffmpeg could not extract an audio track: {detail}")


@app.get("/health")
async def health():
    with _model_cache_guard:
        loaded = sorted(_model_cache)
    return {
        "ok": True,
        "default_model": DEFAULT_MODEL,
        "cache_dir": CACHE_DIR,
        "loaded": loaded,
        "max_inference_concurrency": MAX_INFERENCE_CONCURRENCY,
        "video_extensions": sorted(VIDEO_EXTENSIONS),
    }


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    model: Optional[str] = Form(None),
):
    model_name = (model or DEFAULT_MODEL).strip()
    if model_name != DEFAULT_MODEL:
        raise HTTPException(
            422,
            f"audio worker is configured for Whisper model {DEFAULT_MODEL!r}",
        )
    suffix = Path(audio.filename or "").suffix.lower()
    modality = "video" if suffix in VIDEO_EXTENSIONS else "audio"
    source_path = ""
    transcription_path = ""
    cleanup_paths: list[str] = []

    try:
        with tempfile.NamedTemporaryFile(suffix=suffix or ".audio", delete=False) as handle:
            source_path = handle.name
            cleanup_paths.append(source_path)
            await audio.seek(0)
            await run_in_threadpool(shutil.copyfileobj, audio.file, handle)

        transcription_path = source_path
        if modality == "video":
            descriptor, transcription_path = tempfile.mkstemp(suffix=".wav")
            os.close(descriptor)
            cleanup_paths.append(transcription_path)
            await run_in_threadpool(
                _extract_audio_track,
                source_path,
                transcription_path,
            )

        # Model initialization and inference share one capacity gate. This
        # prevents an uncached load from overlapping active inference inside
        # the worker's 2 GiB container limit.
        async with _inference_slots:
            whisper_model = await run_in_threadpool(_get_model, model_name)
            result = await run_in_threadpool(whisper_model.transcribe, transcription_path)
        return {
            "model": model_name,
            "text": result.get("text", ""),
            "language": result.get("language"),
            "modality": modality,
            "audio_extracted": modality == "video",
        }
    finally:
        for path in cleanup_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
