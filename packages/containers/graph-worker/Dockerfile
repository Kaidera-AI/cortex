# cortex-graph-worker — L3 code graph (DuckDB + SQLite + sqlite-vec + better-code-review-graph)
# Internal only on :9001. cortex-api proxies /graph/* requests here.

# better-code-review-graph 3.21.0 declares Python ==3.13.*.  Keep the
# interpreter contract explicit in both stages; a 3.12 venv is unsatisfiable and
# used to make this image fail only when the resolver ran during a real build.
FROM python:3.13.15-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1 AS bcrg-builder

# Install only reviewed wheels into a relocatable, pip-free venv.  The copied
# interpreter is the worker's sole BCRG execution path in the runtime stage.
COPY requirements.bcrg.lock /build/requirements.bcrg.lock
RUN python -m venv --without-pip /opt/bcrg \
 && python -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
      --target /opt/bcrg/lib/python3.13/site-packages \
      -r /build/requirements.bcrg.lock

# BCRG's local backend defaults to this model but otherwise resolves the moving HF head
# on first use. Fetch one reviewed commit, verify the exact four-file allowlist and run
# inference in the builder; the final stage receives only the immutable cache + receipt.
COPY qwen3-model-pins.json /build/qwen3-model-pins.json
COPY fetch-qwen3-model.py /build/fetch-qwen3-model.py
RUN HF_HUB_DISABLE_TELEMETRY=1 \
    /opt/bcrg/bin/python /build/fetch-qwen3-model.py \
 && test -s /opt/kaidera-qwen3/kaidera-qwen3-model-receipt.json \
 && rm -rf /build


FROM python:3.13.15-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/kaidera \
    TMPDIR=/tmp \
    XDG_CACHE_HOME=/home/kaidera/.cache \
    BCRG_PYTHON=/opt/bcrg/bin/python \
    QWEN3_EMBED_CACHE_PATH=/opt/kaidera-qwen3/cache \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1

RUN groupadd --gid 10001 kaidera \
 && useradd --uid 10001 --gid 10001 --create-home \
      --home-dir /home/kaidera --shell /usr/sbin/nologin kaidera

COPY debian-snapshot.sources /build/debian-snapshot.sources
RUN set -eux; \
    rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.list \
      /etc/apt/sources.list.d/*.sources; \
    cp /build/debian-snapshot.sources /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    set -- /var/lib/apt/lists/*_dists_bookworm_InRelease; \
    [ "$#" -eq 1 ]; \
    printf '%s  %s\n' \
      '77737fa4b34f2693e982cc9ee35736816c35a7778fc2d326cc1bbf5b301fe1aa' \
      "$1" | sha256sum -c -; \
    apt-get install -y --download-only --no-install-recommends \
      'git=1:2.39.5-0+deb12u3'; \
    architecture="$(dpkg --print-architecture)"; \
    case "$architecture" in \
      amd64) package_sha256='637a85ddd6247fab13bdd0592f2f39aff04ce4dbf0655d3ab553ac359a38ce6f' ;; \
      arm64) package_sha256='435176549a9f5c8bbadaa126b64a036b38ea34ad9f3eb128dc40b8407a5b5620' ;; \
      *) exit 1 ;; \
    esac; \
    set -- /var/cache/apt/archives/git_*_"$architecture".deb; \
    [ "$#" -eq 1 ]; \
    printf '%s  %s\n' "$package_sha256" "$1" | sha256sum -c -; \
    apt-get install -y --no-install-recommends 'git=1:2.39.5-0+deb12u3'; \
    [ "$(dpkg-query -W -f='${Version}' git)" = '1:2.39.5-0+deb12u3' ]; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Runtime calls this exact copied interpreter: no package installer, compiler,
# resolver cache or network package-resolution path exists in the final stage.
COPY --from=bcrg-builder /opt/bcrg /opt/bcrg
COPY --from=bcrg-builder /opt/kaidera-qwen3 /opt/kaidera-qwen3

# Worker runtime: FastAPI + DuckDB + sqlite-vec, wheel-only and hash-complete.
COPY requirements.runtime.lock /build/requirements.runtime.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
      -r /build/requirements.runtime.lock \
 && rm -rf /build

WORKDIR /app
COPY worker.py /app/
COPY worker-entrypoint /usr/local/bin/worker-entrypoint
COPY verify-qwen3-model.py /usr/local/libexec/kaidera/verify-qwen3-model.py

ENV CORTEX_GRAPHS_DIR=/var/lib/cortex/graphs
ENV PROJECTS_DIR=/projects

# The native stack mounts these exact paths as named volumes. Creating the mountpoints
# with the runtime identity seeds fresh volumes correctly; the entrypoint separately
# checks the effective mounted paths so appliance /state mounts cannot hide a bad owner.
RUN install -d -o 10001 -g 10001 -m 0750 \
      /home/kaidera/.cache \
      /var/lib/cortex/graphs \
      /var/lib/cortex/models \
 && find /opt/kaidera-qwen3 -type d -exec chmod 0555 {} + \
 && find /opt/kaidera-qwen3 -type f -exec chmod 0444 {} + \
 && chmod 0555 /usr/local/libexec/kaidera/verify-qwen3-model.py \
 && chmod 0755 /usr/local/bin/worker-entrypoint

ARG KOS_VERSION
ARG KOS_SOURCE_REVISION
ARG KOS_IMAGE_SOURCE
ARG KOS_EDITION
RUN /usr/local/bin/worker-entrypoint \
      --validate-image-metadata "$KOS_VERSION" "$KOS_SOURCE_REVISION" \
 && case "$KOS_EDITION:$KOS_IMAGE_SOURCE" in open-source:https://github.com/Kaidera-AI/kaidera-os|open-source:https://github.com/Kaidera-AI/cortex|commercial:https://github.com/Kaidera-AI/kaideraos|dev:https://github.com/Kaidera-AI/kaideraos) ;; *) exit 1 ;; esac

LABEL org.opencontainers.image.title="Kaidera OS Cortex graph worker" \
      org.opencontainers.image.description="Internal code-graph worker for Kaidera OS" \
      org.opencontainers.image.vendor="Kaidera" \
      org.opencontainers.image.source="${KOS_IMAGE_SOURCE}" \
      org.opencontainers.image.version="${KOS_VERSION}" \
      org.opencontainers.image.revision="${KOS_SOURCE_REVISION}" \
      io.kaidera.kos.edition="${KOS_EDITION}" \
      io.kaidera.kos.apt.snapshot="20260821T082113Z" \
      io.kaidera.kos.apt.sources.sha256="6577a69eb4910f1685aa0f5b96b81dfd65a1ce4eca132773cda7fe0ec75b5cda" \
      io.kaidera.kos.apt.inrelease.sha256="77737fa4b34f2693e982cc9ee35736816c35a7778fc2d326cc1bbf5b301fe1aa" \
      io.kaidera.kos.apt.package="git=1:2.39.5-0+deb12u3" \
      io.kaidera.kos.apt.package.amd64.sha256="637a85ddd6247fab13bdd0592f2f39aff04ce4dbf0655d3ab553ac359a38ce6f" \
      io.kaidera.kos.apt.package.arm64.sha256="435176549a9f5c8bbadaa126b64a036b38ea34ad9f3eb128dc40b8407a5b5620" \
      io.kaidera.kos.model="n24q02m/Qwen3-Embedding-0.6B-ONNX" \
      io.kaidera.kos.model.source="https://huggingface.co/n24q02m/Qwen3-Embedding-0.6B-ONNX" \
      io.kaidera.kos.model.revision="dc873d64d6143f27ad68dadbd1f0d9a4371b994e" \
      io.kaidera.kos.model.license="Apache-2.0" \
      io.kaidera.kos.model.pins.sha256="84777151610296106a65fa90d725e40d6517efb09819b3aee340be775c6bbcea" \
      io.kaidera.kos.python.bcrg-lock.sha256="efa8348ead260fc8bea1f81fba229d9e654dd513f858bdb7e97823f5544c36b5" \
      io.kaidera.kos.python.runtime-lock.sha256="5e61178fa0f47853b5393f84f263ae02ad7aed520693e05ce4b1a19c79d4dcea" \
      io.kaidera.kos.worker="graph"

USER 10001:10001

# Prove the receipt, exact cache and local-only inference path as the final user. Runtime
# entrypoint repeats the receipt/hash check before Uvicorn can expose a healthy endpoint.
RUN "$BCRG_PYTHON" -I -c "import better_code_review_graph"
RUN /usr/local/bin/python -I /usr/local/libexec/kaidera/verify-qwen3-model.py \
 && "$BCRG_PYTHON" -I -c "import os; import better_code_review_graph; from qwen3_embed import TextEmbedding; model=TextEmbedding(model_name='n24q02m/Qwen3-Embedding-0.6B-ONNX', cache_dir=os.environ['QWEN3_EMBED_CACHE_PATH'], local_files_only=True); vector=next(model.embed(['Kaidera offline runtime probe'], dim=768)); assert tuple(vector.shape) == (768,)"
RUN ! command -v curl && ! command -v uv && ! command -v cc && ! command -v gcc

EXPOSE 9001

ENTRYPOINT ["/usr/local/bin/worker-entrypoint"]
CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "9001"]
