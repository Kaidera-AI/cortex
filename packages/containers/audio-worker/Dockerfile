# cortex-audio-worker — openai-whisper transcription
# Internal only on :9003.

FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Whisper 20240930 imports pkg_resources from setup.py. Setuptools 82 removed it;
# pin the last compatible major and disable an unconstrained isolated build env.
RUN pip install --no-cache-dir \
    setuptools==81.0.0 \
    wheel==0.45.1

# PyPI's Linux arm64 torch wheel pulls the CUDA 13 runtime into a CPU-only worker.
# Use the pinned upstream CPU index so local macOS/Linux builds stay operationally bounded.
RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.13.0

RUN pip install --no-cache-dir --no-build-isolation \
    fastapi==0.141.1 \
    starlette==1.6.0 \
    uvicorn[standard]==0.52.3 \
    openai-whisper==20240930 \
    pydantic==2.11.4 \
    python-multipart==0.0.32

ENV WHISPER_MODEL=base \
    WHISPER_CACHE_DIR=/var/lib/cortex/models/whisper \
    WHISPER_MAX_INFERENCE_CONCURRENCY=1

WORKDIR /app
COPY worker.py /app/

EXPOSE 9003

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD curl -fsS http://localhost:9003/health || exit 1

CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "9003"]
