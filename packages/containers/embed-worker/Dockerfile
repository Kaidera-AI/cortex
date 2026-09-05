# cortex-local-search-worker — pinned local embeddings and reranking.
# Internal only on :9005. cortex-api is the sole caller.

FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS model-fetch

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/kaidera \
    TMPDIR=/tmp \
    XDG_CACHE_HOME=/home/kaidera/.cache

RUN groupadd --gid 10001 kaidera \
 && useradd --uid 10001 --gid 10001 --create-home \
      --home-dir /home/kaidera --shell /usr/sbin/nologin kaidera

COPY requirements.lock /build/requirements.lock
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes \
      -r /build/requirements.lock \
    && python -c "import torch; assert torch.__version__ == '2.13.0+cpu'; assert torch.version.cuda is None" \
    && rm -rf /build


# Fetch only the exact safe artifacts from immutable model commits. This stage
# has network access; the runtime stage is offline and receives no downloader.
COPY model-pins.json /build/model-pins.json
COPY fetch-models.py /build/fetch-models.py
RUN python /build/fetch-models.py \
 && test ! -e /opt/kaidera-models/embedding/pytorch_model.bin \
 && test ! -e /opt/kaidera-models/rerank/pytorch_model.bin


FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/kaidera \
    TMPDIR=/tmp \
    XDG_CACHE_HOME=/home/kaidera/.cache

RUN groupadd --gid 10001 kaidera \
 && useradd --uid 10001 --gid 10001 --create-home \
      --home-dir /home/kaidera --shell /usr/sbin/nologin kaidera

COPY requirements.lock /build/requirements.lock
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes \
      -r /build/requirements.lock \
    && python -c "import torch; assert torch.__version__ == '2.13.0+cpu'; assert torch.version.cuda is None" \
    && rm -rf /build

# These caches are writable runtime scratch only.  Both selected models live in
# the immutable image under /opt and every model library is forced offline.
ENV HF_HOME=/var/lib/cortex/models/hf \
    SENTENCE_TRANSFORMERS_HOME=/var/lib/cortex/models/sentence-transformers \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    CORTEX_DEFAULT_MODEL=sentence-transformers/all-mpnet-base-v2 \
    CORTEX_DEFAULT_MODEL_REVISION=e8c3b32edf5434bc2275fc9bab85f82640a19130 \
    CORTEX_DEFAULT_RERANK_MODEL=cross-encoder/ms-marco-MiniLM-L6-v2 \
    CORTEX_DEFAULT_RERANK_MODEL_REVISION=233902d25c440f23af6f7d6e94d2946bac0bee0a

WORKDIR /app
COPY worker.py /app/
COPY worker-entrypoint /usr/local/bin/worker-entrypoint
COPY --from=model-fetch /opt/kaidera-models /opt/kaidera-models

RUN install -d -o 10001 -g 10001 -m 0750 \
      /home/kaidera/.cache \
      /var/lib/cortex/models/hf \
      /var/lib/cortex/models/sentence-transformers \
 && find /opt/kaidera-models -type d -exec chmod 0555 {} + \
 && find /opt/kaidera-models -type f -exec chmod 0444 {} + \
 && chmod 0755 /usr/local/bin/worker-entrypoint

ARG KOS_VERSION
ARG KOS_SOURCE_REVISION
ARG KOS_IMAGE_SOURCE
ARG KOS_EDITION
RUN /usr/local/bin/worker-entrypoint \
      --validate-image-metadata "$KOS_VERSION" "$KOS_SOURCE_REVISION" \
 && case "$KOS_EDITION:$KOS_IMAGE_SOURCE" in open-source:https://github.com/Kaidera-AI/kaidera-os|open-source:https://github.com/Kaidera-AI/cortex|commercial:https://github.com/Kaidera-AI/kaideraos|dev:https://github.com/Kaidera-AI/kaideraos) ;; *) exit 1 ;; esac

LABEL org.opencontainers.image.title="Kaidera OS Cortex local-search worker" \
      org.opencontainers.image.description="Internal keyless local embedding and rerank worker for Kaidera OS" \
      org.opencontainers.image.vendor="Kaidera" \
      org.opencontainers.image.source="${KOS_IMAGE_SOURCE}" \
      org.opencontainers.image.version="${KOS_VERSION}" \
      org.opencontainers.image.revision="${KOS_SOURCE_REVISION}" \
      io.kaidera.kos.edition="${KOS_EDITION}" \
      io.kaidera.kos.model="sentence-transformers/all-mpnet-base-v2" \
      io.kaidera.kos.model.source="https://huggingface.co/sentence-transformers/all-mpnet-base-v2" \
      io.kaidera.kos.model.revision="e8c3b32edf5434bc2275fc9bab85f82640a19130" \
      io.kaidera.kos.model.license="Apache-2.0" \
      io.kaidera.kos.rerank.model="cross-encoder/ms-marco-MiniLM-L6-v2" \
      io.kaidera.kos.rerank.model.source="https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2" \
      io.kaidera.kos.rerank.model.revision="233902d25c440f23af6f7d6e94d2946bac0bee0a" \
      io.kaidera.kos.rerank.model.license="Apache-2.0" \
      io.kaidera.kos.python.lock.sha256="03cb23ec5fc4ab946fca5fcc5ab5d1c8f3ce9b8efd6a249381e23e7925f20923" \
      io.kaidera.kos.worker="embed"

USER 10001:10001

# Prove the dependency stack and immutable artifact inventory as the final user.
RUN python -c \
      "from pathlib import Path; import sentence_transformers, torch; assert torch.__version__ == '2.13.0+cpu'; assert torch.version.cuda is None; root=Path('/opt/kaidera-models'); assert all((root/p).is_file() for p in ('embedding/model.safetensors','rerank/model.safetensors','model-pins.json','kaidera-model-receipt.json'))"
RUN ! command -v curl && ! command -v uv && ! command -v cc && ! command -v gcc

EXPOSE 9005

ENTRYPOINT ["/usr/local/bin/worker-entrypoint"]
CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "9005"]
