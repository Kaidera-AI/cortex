# R1 upstream dependency preparation only. Cortex runtime/final-F joins are R2.
ARG CORE_BASE
FROM ${CORE_BASE} AS compile
COPY debs/ /debs/
RUN apt-get install -y --no-install-recommends /debs/*.deb \
    && rm -rf /debs /var/lib/apt/lists/* /var/cache/apt/archives/*
COPY pgvector/ /build/pgvector/
ARG SOURCE_DATE_EPOCH
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH} LC_ALL=C TZ=UTC
RUN cd /build/pgvector \
    && PG_CFLAGS="-ffile-prefix-map=/build/pgvector=. -fdebug-prefix-map=/build/pgvector=." make -j2 OPTFLAGS="" \
    && make install DESTDIR=/material \
    && mkdir -p /material/usr/share/doc/pgvector \
    && cp LICENSE README.md /material/usr/share/doc/pgvector/

FROM ${CORE_BASE} AS material
COPY --from=compile /material/ /
USER postgres
