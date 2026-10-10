FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

ARG KOS_VERSION
ARG KOS_SOURCE_REVISION
ARG KOS_IMAGE_SOURCE
ARG KOS_EDITION
ARG CORTEX_RELEASE_ID
ARG CORTEX_RELEASE_LINEAGE
ARG CORTEX_RELEASE_SEQUENCE
ARG CORTEX_API_CONTRACT
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

COPY api/requirements.lock /build/requirements.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
      -r /build/requirements.lock \
 && rm -rf /build \
 && groupadd --system --gid 10001 kaidera \
 && useradd --system --uid 10001 --gid 10001 --no-create-home \
      --shell /usr/sbin/nologin kaidera

# Recompile after the install layer has stamped source mtimes. Keep timestamp caches.
RUN python -c "import compileall, py_compile, sysconfig; assert compileall.compile_dir(sysconfig.get_path('purelib'), force=True, quiet=1, optimize=0, invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)"

# Build from packages so the one canonical schema inventory is in context.
# Explicit API allowlist; tests, caches and recipes are never runtime payload.
COPY api/main.py \
     api/mcp_server.py \
     api/observability.py \
     api/openkai_provider_env.py \
     api/db_tls.py \
     api/service_auth.py \
     api/service_auth_http.py \
     api/service_auth_policy.py \
     api/cortex_auth_tokens.py \
     api/release_identity.py \
     api/service_auth_owner.py \
     api/service_auth_owner_client.py \
     api/service_auth_owner_lifespan.py \
     ./
COPY api/ingest/ ./ingest/
COPY api/models/ ./models/

COPY schema/migrations/ /app/migrations/

RUN test -s ./main.py \
 && test -s ./openkai_provider_env.py \
 && test -s ./service_auth.py \
 && test -s ./service_auth_owner.py \
 && test -s ./service_auth_owner_client.py \
 && test -s ./service_auth_owner_lifespan.py \
 && test -s ./ingest/__init__.py \
 && test -s ./models/__init__.py

# Legacy builds stay unqualified; a manual release must bake its public identity.
RUN if [ -n "$CORTEX_RELEASE_SEQUENCE" ]; then \
      python /app/release_identity.py "$CORTEX_RELEASE_ID" "$CORTEX_RELEASE_LINEAGE" \
        "$CORTEX_RELEASE_SEQUENCE" "$CORTEX_API_CONTRACT" "$KOS_SOURCE_REVISION"; \
    else \
      case "$KOS_VERSION" in *-manual.*) exit 1 ;; esac; \
    fi

EXPOSE 8501
USER 10001:10001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8501"]

COPY api/standalone/ ./standalone/
