"""Process-wide Cortex API metrics.

Keeping collectors in a normal imported module makes registration idempotent
when tests or embedded runtimes load ``main.py`` under more than one module name.
"""

from __future__ import annotations

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
except ModuleNotFoundError:  # pragma: no cover - minimal source-only environments
    class _NoopMetric:
        def labels(self, **_kwargs):
            return self

        def inc(self, *_args, **_kwargs):
            return None

        def observe(self, *_args, **_kwargs):
            return None

    def Counter(*_args, **_kwargs):
        return _NoopMetric()

    def Histogram(*_args, **_kwargs):
        return _NoopMetric()

    def generate_latest():
        return b""

    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


REQUEST_DURATION = Histogram(
    "cortex_request_duration_seconds",
    "HTTP request latency by method and endpoint",
    ["method", "endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

EMBEDDING_CALLS = Counter(
    "cortex_embedding_calls_total",
    "Total embedding API calls",
    ["model", "status"],
)

RERANK_CALLS = Counter(
    "cortex_rerank_calls_total",
    "Total rerank API calls",
    ["model", "status"],
)

ANALYSIS_CALLS = Counter(
    "cortex_analysis_calls_total",
    "Total analysis LLM calls",
    ["model", "status"],
)

RETENTION_ROWS_ARCHIVED = Counter(
    "cortex_retention_rows_archived_total",
    "Rows moved from a tier-2 table to its archive by retention enforcement",
    ["table"],
)

BOOT_CACHE_HITS = Counter(
    "cortex_boot_cache_hits_total",
    "Boot context cache hits",
)

BOOT_CACHE_MISSES = Counter(
    "cortex_boot_cache_misses_total",
    "Boot context cache misses",
)

SEARCH_STAGE_DURATION = Histogram(
    "cortex_search_stage_seconds",
    "Search pipeline per-stage latency",
    ["stage"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)


def render_metrics() -> bytes:
    return generate_latest()
