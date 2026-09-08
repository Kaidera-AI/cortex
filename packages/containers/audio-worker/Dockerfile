# cortex-audio-worker — openai-whisper transcription
# Internal only on :9003.

FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

COPY debian-snapshot.sources /build/debian-snapshot.sources
COPY install-os.sh /build/install-os.sh
RUN sh /build/install-os.sh

# Latest Whisper is source-only. Hash-lock both runtime and build frontend/backend;
# disable the implicit, otherwise unconstrained isolated build environment.
COPY requirements.build.lock requirements.lock /build/
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r /build/requirements.build.lock \
 && pip install --no-cache-dir --no-build-isolation --only-binary=:all: \
      --no-binary=openai-whisper --require-hashes -r /build/requirements.lock \
 && python -c "import torch, whisper; assert torch.__version__ == '2.14.0+cpu'; assert torch.version.cuda is None" \
 && rm -rf /build

LABEL io.kaidera.kos.python.lock.sha256="36001deed95c085e9562bdd0c9b0a2ed67b6f754e97fd9682efda715185d6d73" \
      io.kaidera.kos.apt.snapshot="20260905T000000Z" \
      io.kaidera.kos.apt.sources.sha256="92bb8050b6c14816344007be399de0cba4b0f173ba700c08937d3540b88fbecf"

ENV WHISPER_MODEL=base \
    WHISPER_CACHE_DIR=/var/lib/cortex/models/whisper \
    WHISPER_MAX_INFERENCE_CONCURRENCY=1

WORKDIR /app
COPY worker.py /app/

EXPOSE 9003

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD curl -fsS http://localhost:9003/health || exit 1

CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "9003"]
