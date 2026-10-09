"""Design59 v1.2's closed Cortex metric families and four identity labels."""

import math
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from uuid import UUID


@dataclass(frozen=True)
class MetricIdentity:
    deployment_id: UUID
    environment: str
    release: str


class Metrics:
    """Last observed latency gauges; no extra histogram/route/user labels."""

    def __init__(self, identity: MetricIdentity):
        if not isinstance(identity.deployment_id, UUID) or any(
            not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", v)
            for v in (identity.environment, identity.release)
        ):
            raise ValueError("Invalid monitoring identity labels")
        self.identity = identity
        self._values = {}
        self._lock = Lock()

    def _observe(self, name, seconds):
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or not 0 <= seconds <= 3600
        ):
            raise ValueError("Latency must be finite, nonnegative and bounded")
        with self._lock:
            self._values[name] = float(seconds)

    def observe_api(self, seconds):
        self._observe("cortex_api_latency_seconds", seconds)

    def observe_search(self, seconds):
        self._observe("cortex_search_latency_seconds", seconds)

    @contextmanager
    def timer(self, kind):
        if kind not in {"api", "search"}:
            raise ValueError("Unknown metric family")
        started = time.perf_counter()
        try:
            yield
        finally:
            self._observe(
                "cortex_" + kind + "_latency_seconds", time.perf_counter() - started
            )

    def update_core(self, db_bytes, embed_backlog):
        if any(
            isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < 2**63
            for v in (db_bytes, embed_backlog)
        ):
            raise ValueError("Core observations must be nonnegative integers")
        with self._lock:
            self._values["cortex_db_bytes"] = db_bytes
            self._values["cortex_embed_backlog"] = embed_backlog

    def export(self):
        labels = f'deployment_id="{self.identity.deployment_id}",environment="{self.identity.environment}",component="cortex",release="{self.identity.release}"'
        with self._lock:
            samples = sorted(self._values.items())
        return "".join(
            f"# TYPE {name} gauge\n{name}{{{labels}}} {value}\n"
            for name, value in samples
        )
