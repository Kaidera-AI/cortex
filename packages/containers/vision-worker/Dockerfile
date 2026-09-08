# cortex-vision-worker — Ollama wrapper for VLM (Qwen3-VL-4B)
# Internal only on :9002.
#
# Phase 1 ships as a thin wrapper around the Ollama HTTP API. The Ollama
# binary itself is in this container; the model (qwen3-vl:4b ~8GB) is pulled
# lazily on first request to keep image size manageable. The pulled model
# persists in the cortex-models volume.

FROM ollama/ollama:0.33.3@sha256:32931b46719f673c05fdbaa81ccb26da18ea4a1c57590a754874ab28ba269eb2 AS ollama
FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

# Preserve upstream Ollama runtime/accelerator libraries, but use the current
# CPython runtime rather than Ubuntu's older distribution-owned Python.
COPY --from=ollama /usr/bin/ollama /usr/bin/ollama
COPY --from=ollama /usr/lib/ollama /usr/lib/ollama
ENV LD_LIBRARY_PATH=/usr/local/nvidia/lib:/usr/local/nvidia/lib64 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility
COPY debian-snapshot.sources /build/debian-snapshot.sources
COPY install-os.sh /build/install-os.sh
RUN sh /build/install-os.sh

COPY requirements.lock /build/requirements.lock
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r /build/requirements.lock \
 && rm -rf /build
LABEL io.kaidera.kos.python.lock.sha256="9462525701b27db71853db5883e3d8f7151e5f3a4aeeaf337d95c950f3c21adc" \
      io.kaidera.kos.apt.snapshot="20260905T000000Z" \
      io.kaidera.kos.apt.sources.sha256="92bb8050b6c14816344007be399de0cba4b0f173ba700c08937d3540b88fbecf"

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
