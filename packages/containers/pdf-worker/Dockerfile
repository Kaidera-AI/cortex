# cortex-pdf-worker — bounded PDF parsing (Poppler + future MinerU)
# Internal only on :9004.
#
# Note: MinerU adds ~3GB and many GPU/heavy ML deps. Phase 1 ships with
# poppler/pdftotext only (the lightweight path). magic-pdf/MinerU is a
# Phase 1.5 add-on once the lightweight path is verified.

FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/kaidera \
    TMPDIR=/tmp \
    XDG_CACHE_HOME=/home/kaidera/.cache

RUN groupadd --gid 10001 kaidera \
 && useradd --uid 10001 --gid 10001 --create-home \
      --home-dir /home/kaidera --shell /usr/sbin/nologin kaidera

COPY debian-snapshot.sources /build/debian-snapshot.sources
RUN set -eux; \
    rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.list \
      /etc/apt/sources.list.d/*.sources; \
    cp /build/debian-snapshot.sources /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    set -- /var/lib/apt/lists/*_dists_trixie_InRelease; \
    [ "$#" -eq 1 ]; \
    printf '%s  %s\n' \
      '98b25b5cd185c59d34aa6e4c3e9b5b8f01bbe9d104fe2dcfbcd30dc0a14a59ed' \
      "$1" | sha256sum -c -; \
    set -- /var/lib/apt/lists/*_dists_trixie-security_InRelease; \
    [ "$#" -eq 1 ]; \
    printf '%s  %s\n' '19a20e3818e39ae271b114aa048a20d961b3f4604c013bed46c2e6c991d08dbb' "$1" | sha256sum -c -; \
    set -- /var/lib/apt/lists/*_dists_trixie-updates_InRelease; \
    [ "$#" -eq 1 ]; \
    printf '%s  %s\n' 'f02d96f99db560df6328eeb584f034ca1d9fc4f25eb644144b042a0193165def' "$1" | sha256sum -c -; \
    apt-get install -y --download-only --no-install-recommends \
      'poppler-utils=25.03.0-5+deb13u4'; \
    architecture="$(dpkg --print-architecture)"; \
    case "$architecture" in \
      amd64) package_sha256='7b5bb3607e7fa078e002420d4e5129d7b83c5684ca6f88d229a40af3e76a7b02' ;; \
      arm64) package_sha256='f7d122283d16ccbe06a922c09bacfc0b0af46b09580ae0511fb2815aed770c6a' ;; \
      *) exit 1 ;; \
    esac; \
    set -- /var/cache/apt/archives/poppler-utils_*_"$architecture".deb; \
    [ "$#" -eq 1 ]; \
    printf '%s  %s\n' "$package_sha256" "$1" | sha256sum -c -; \
    apt-get install -y --no-install-recommends \
      'poppler-utils=25.03.0-5+deb13u4'; \
    [ "$(dpkg-query -W -f='${Version}' poppler-utils)" = '25.03.0-5+deb13u4' ]; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

COPY requirements.lock /build/requirements.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
      -r /build/requirements.lock \
 && rm -rf /build

WORKDIR /app
COPY worker.py /app/
COPY worker-entrypoint /usr/local/bin/worker-entrypoint

RUN install -d -o 10001 -g 10001 -m 0750 \
      /home/kaidera/.cache \
      /var/lib/cortex/vendor \
 && chmod 0755 /usr/local/bin/worker-entrypoint

ARG KOS_VERSION
ARG KOS_SOURCE_REVISION
ARG KOS_IMAGE_SOURCE
ARG KOS_EDITION
RUN /usr/local/bin/worker-entrypoint \
      --validate-image-metadata "$KOS_VERSION" "$KOS_SOURCE_REVISION" \
 && case "$KOS_EDITION:$KOS_IMAGE_SOURCE" in open-source:https://github.com/Kaidera-AI/kaidera-os|open-source:https://github.com/Kaidera-AI/cortex|commercial:https://github.com/Kaidera-AI/kaideraos|dev:https://github.com/Kaidera-AI/kaideraos) ;; *) exit 1 ;; esac

LABEL org.opencontainers.image.title="Kaidera OS Cortex PDF worker" \
      org.opencontainers.image.description="Internal PDF extraction worker for Kaidera OS" \
      org.opencontainers.image.vendor="Kaidera" \
      org.opencontainers.image.source="${KOS_IMAGE_SOURCE}" \
      org.opencontainers.image.version="${KOS_VERSION}" \
      org.opencontainers.image.revision="${KOS_SOURCE_REVISION}" \
      io.kaidera.kos.edition="${KOS_EDITION}" \
      io.kaidera.kos.apt.snapshot="20260905T000000Z" \
      io.kaidera.kos.apt.security-inrelease.sha256="19a20e3818e39ae271b114aa048a20d961b3f4604c013bed46c2e6c991d08dbb" \
      io.kaidera.kos.apt.updates-inrelease.sha256="f02d96f99db560df6328eeb584f034ca1d9fc4f25eb644144b042a0193165def" \
      io.kaidera.kos.apt.sources.sha256="92bb8050b6c14816344007be399de0cba4b0f173ba700c08937d3540b88fbecf" \
      io.kaidera.kos.apt.inrelease.sha256="98b25b5cd185c59d34aa6e4c3e9b5b8f01bbe9d104fe2dcfbcd30dc0a14a59ed" \
      io.kaidera.kos.apt.package="poppler-utils=25.03.0-5+deb13u4" \
      io.kaidera.kos.apt.package.amd64.sha256="7b5bb3607e7fa078e002420d4e5129d7b83c5684ca6f88d229a40af3e76a7b02" \
      io.kaidera.kos.apt.package.arm64.sha256="f7d122283d16ccbe06a922c09bacfc0b0af46b09580ae0511fb2815aed770c6a" \
      io.kaidera.kos.python.lock.sha256="a69c669996801ce1241890dc777ec66d137465b4f031e0a3213bc7eee7cd2ffa" \
      io.kaidera.kos.worker="pdf"

USER 10001:10001

# Exercise both bounded Poppler dependencies as the final user.
RUN pdftotext -v >/dev/null \
 && pdfinfo -v >/dev/null
RUN ! command -v curl && ! command -v uv && ! command -v cc && ! command -v gcc

EXPOSE 9004

ENTRYPOINT ["/usr/local/bin/worker-entrypoint"]
CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "9004"]
