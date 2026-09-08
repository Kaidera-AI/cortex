FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

ARG KOS_VERSION
ARG KOS_SOURCE_REVISION
ARG KOS_IMAGE_SOURCE
ARG KOS_EDITION
LABEL org.opencontainers.image.source="$KOS_IMAGE_SOURCE" \
      org.opencontainers.image.version="$KOS_VERSION" \
      org.opencontainers.image.revision="$KOS_SOURCE_REVISION" \
      io.kaidera.kos.edition="$KOS_EDITION" \
      io.kaidera.kos.python.lock.sha256="7b4ef32e848a05d673054453345d63006209d33e5d295a84ec73ec95033da354"
RUN test -n "$KOS_VERSION" \
 && case "$KOS_EDITION:$KOS_IMAGE_SOURCE" in open-source:https://github.com/Kaidera-AI/kaidera-os|open-source:https://github.com/Kaidera-AI/cortex|commercial:https://github.com/Kaidera-AI/kaideraos|dev:https://github.com/Kaidera-AI/kaideraos) ;; *) exit 1 ;; esac \
 && test "${#KOS_SOURCE_REVISION}" -eq 40 \
 && case "$KOS_SOURCE_REVISION" in *[!0-9a-f]*) exit 1 ;; esac

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp/kaidera \
    OPENKAI_PROVIDER_PROJECTION_DIR=/run/openkai-provider \
    OPENKAI_PROVIDER_REGISTRY_FILE=/run/openkai-registry.json \
    OPENKAI_PROVIDER_STATUS_URL=http://host.containers.internal:8766/provider-config

COPY requirements.lock /build/requirements.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
      -r /build/requirements.lock \
 && rm -rf /build \
 && groupadd --system --gid 10001 kaidera \
 && useradd --system --uid 10001 --gid 10001 --no-create-home \
      --shell /usr/sbin/nologin kaidera

# Keep the Apple/Compose image to the same explicit importable API allowlist as the
# source-free appliance image. Tests, caches and the source Dockerfile are not runtime.
COPY main.py \
     mcp_server.py \
     observability.py \
     openkai_provider_env.py \
     db_tls.py \
     service_auth.py \
     service_auth_owner.py \
     service_auth_owner_client.py \
     service_auth_owner_lifespan.py \
     ./
COPY ingest/ ./ingest/
COPY models/ ./models/

RUN test -s ./main.py \
 && test -s ./openkai_provider_env.py \
 && test -s ./service_auth.py \
 && test -s ./service_auth_owner.py \
 && test -s ./service_auth_owner_client.py \
 && test -s ./service_auth_owner_lifespan.py \
 && test -s ./ingest/__init__.py \
 && test -s ./models/__init__.py

EXPOSE 8501
USER 10001:10001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8501"]

COPY standalone/ ./standalone/
