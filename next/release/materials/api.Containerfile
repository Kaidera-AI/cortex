# R1 upstream dependency preparation only. No Cortex runtime or F descriptor.
ARG API_BASE
FROM ${API_BASE} AS install
COPY wheels/ /wheels/
COPY requirements.txt /requirements.txt
RUN python3 -m pip --isolated --disable-pip-version-check --no-cache-dir install \
    --no-index --find-links=/wheels --require-hashes --no-compile \
    --target=/material/python -r /requirements.txt

FROM ${API_BASE} AS material
COPY --from=install /material/python/ /opt/cortex/python/
# Full ldd closure of upstream libpq18.6; originals and licenses inventoried in R1.
COPY libpq/ /opt/cortex/libpq/
RUN printf '%s\n' /opt/cortex/libpq > /etc/ld.so.conf.d/cortex-libpq.conf \
    && ldconfig \
    && rm -f /var/cache/ldconfig/aux-cache
ENV PYTHONPATH=/opt/cortex/python LD_LIBRARY_PATH=/opt/cortex/libpq PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
