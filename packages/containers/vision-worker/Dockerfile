# cortex-vision-worker — Ollama wrapper for VLM (Qwen3-VL-4B)
# Internal only on :9002.
#
# Phase 1 ships as a thin wrapper around the Ollama HTTP API. The Ollama
# binary itself is in this container; the model (qwen3-vl:4b ~8GB) is pulled
# lazily on first request to keep image size manageable. The pulled model
# persists in the cortex-models volume.

FROM ollama/ollama:0.33.2@sha256:020e4134285e2ef4d8fd801234176de3b4faadc992a3eb06c8e66a2f9d4c4ba2

# Install curl for healthcheck and the FastAPI wrapper deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --break-system-packages --no-cache-dir \
    fastapi==0.141.1 \
    starlette==1.6.0 \
    uvicorn[standard]==0.52.3 \
    httpx==0.27.2 \
    pydantic==2.11.4 \
    python-multipart==0.0.32

ENV OLLAMA_HOST=0.0.0.0:11434
ENV OLLAMA_MODELS=/var/lib/cortex/models/ollama
ENV CORTEX_VISION_MODEL=qwen3-vl:4b

WORKDIR /app
COPY worker.py entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

EXPOSE 9002

HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
    CMD curl -fsS http://localhost:9002/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
