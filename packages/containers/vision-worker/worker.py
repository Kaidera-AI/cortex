"""cortex-vision-worker — Ollama wrapper for image/diagram VLM enrichment."""

from __future__ import annotations

import base64
import os
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

OLLAMA_BASE = "http://localhost:11434"
DEFAULT_MODEL = os.environ.get("CORTEX_VISION_MODEL", "qwen3-vl:4b")

app = FastAPI(title="cortex-vision-worker", version="0.1.0")
_pulled: set[str] = set()


async def _ensure_model(client: httpx.AsyncClient, model: str):
    if model in _pulled:
        return
    # Check if already present
    r = await client.get(f"{OLLAMA_BASE}/api/tags")
    r.raise_for_status()
    tags = {t["name"] for t in r.json().get("models", [])}
    if model in tags:
        _pulled.add(model)
        return
    # Pull (streaming) — first call is slow; subsequent calls cached in volume
    async with client.stream("POST", f"{OLLAMA_BASE}/api/pull", json={"name": model}, timeout=None) as resp:
        resp.raise_for_status()
        async for _ in resp.aiter_lines():
            pass
    _pulled.add(model)


@app.get("/health")
async def health():
    async with httpx.AsyncClient(timeout=3) as client:
        try:
            r = await client.get(f"{OLLAMA_BASE}/api/tags")
        except Exception as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "ollama_reachable": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
    if r.status_code != 200:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "ollama_reachable": True,
                "error": f"Ollama tags returned status {r.status_code}",
            },
        )
    try:
        payload = r.json()
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        models = payload.get("models", [])
        if not isinstance(models, list):
            raise ValueError("models is not a list")
        tags = [
            item["name"]
            for item in models
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
    except (TypeError, ValueError) as exc:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "ollama_reachable": True,
                "error": f"invalid Ollama tags response: {exc}",
            },
        )
    return {"ok": True, "default_model": DEFAULT_MODEL, "models_pulled": tags}


@app.post("/describe-image")
async def describe_image(
    image: UploadFile = File(...),
    prompt: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
):
    """Describe an image. Pulls the model on first call if missing."""
    model_name = model or DEFAULT_MODEL
    body = await image.read()
    if not body:
        raise HTTPException(400, "empty image")
    b64 = base64.b64encode(body).decode("ascii")
    user_prompt = prompt or "Describe this image in detail. If it is a diagram, capture its structure and labels."

    async with httpx.AsyncClient(timeout=300) as client:
        await _ensure_model(client, model_name)
        r = await client.post(
            f"{OLLAMA_BASE}/api/generate",
            json={"model": model_name, "prompt": user_prompt, "images": [b64], "stream": False},
        )
        r.raise_for_status()
        data = r.json()
    return {"model": model_name, "description": data.get("response", "").strip()}
