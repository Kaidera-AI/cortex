"""OpenKai memory adapter (R28, plan W2 item 7, F12).

``memory.backend=off|cortex``. With ``cortex``, Cortex *replaces* the legacy
OMP memory pipeline — it is never a second pipeline alongside it, there is
no OMP fallback path, and legacy OMP/Hindsight/Mnemopi files and rows are
preserved but not imported (the current architecture authorizes no
importer). Transcript ingestion happens only with explicit opt-in consent.
Unavailable Cortex is reported honestly as an unavailable result.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from ..client import CortexClient
from ..errors import ClientConfigError, ClientError, CortexApiError

MemoryBackend = Literal["off", "cortex"]

RECALL_OPERATION = "content.search.lexical"
RECORD_OPERATION = "memory.record"
SESSION_INGEST_OPERATION = "ingest.session"


class IngestConsentRequired(ClientError):
    def __init__(self) -> None:
        super().__init__(
            "transcript ingestion requires explicit opt-in consent "
            "(ingest_consent=True); nothing was sent"
        )


@dataclass(frozen=True, slots=True)
class MemoryResult:
    state: Literal["ok", "disabled", "unavailable"]
    data: Any = None
    reason: str | None = None


class OpenKaiMemoryAdapter:
    def __init__(
        self,
        backend: MemoryBackend,
        *,
        client: CortexClient | None = None,
        scope: str | None = None,
        ingest_consent: bool = False,
    ) -> None:
        if backend not in ("off", "cortex"):
            raise ClientConfigError(
                f"memory.backend must be 'off' or 'cortex'; got {backend!r}"
            )
        if backend == "cortex" and client is None:
            raise ClientConfigError(
                "memory.backend=cortex requires a v2 client profile; there "
                "is no OMP or other fallback backend"
            )
        self.backend = backend
        self._client = client
        self._scope = scope
        self.ingest_consent = bool(ingest_consent)

    def _disabled(self) -> MemoryResult:
        return MemoryResult(state="disabled", reason="memory.backend=off")

    def _call(self, operation_id: str, **kwargs: Any) -> MemoryResult:
        assert self._client is not None
        try:
            result = self._client.call(operation_id, scope=self._scope,
                                       **kwargs)
        except CortexApiError as exc:
            return MemoryResult(state="unavailable", reason=exc.code)
        except ClientError as exc:
            return MemoryResult(state="unavailable", reason=str(exc))
        return MemoryResult(state="ok", data=result.data)

    def recall(
        self, query: str, *, limit: int = 10,
        read_scopes: tuple[str, ...] | None = None,
    ) -> MemoryResult:
        if self.backend == "off":
            return self._disabled()
        return self._call(
            RECALL_OPERATION,
            payload={
                "query": query,
                "read_scopes": list(read_scopes) if read_scopes
                else [self._scope] if self._scope else [],
                "limit": limit,
                "include_historical": False,
            },
            read_scopes=read_scopes,
        )

    def record(
        self, record_type: str, body: str, *,
        idempotency_key: str | None = None,
    ) -> MemoryResult:
        if self.backend == "off":
            return self._disabled()
        key = idempotency_key or self._derived_key(record_type, body)
        return self._call(
            RECORD_OPERATION,
            payload={"record_type": record_type, "body": body},
            idempotency_key=key,
        )

    def learn(self, lesson: str, *, idempotency_key: str | None = None
              ) -> MemoryResult:
        if self.backend == "off":
            return self._disabled()
        return self.record("lesson", lesson, idempotency_key=idempotency_key)

    def _derived_key(self, record_type: str, body: str) -> str:
        material = f"{self._scope}:{record_type}:{body}"
        return (
            "openkai-record-"
            + hashlib.sha256(material.encode("utf-8")).hexdigest()[:48]
        )

    def ingest_transcript(
        self,
        *,
        connector_namespace: str,
        source_key: str,
        transcript_format: str,
        transcript: str,
        idempotency_key: str,
        source_observed_at: str | None = None,
    ) -> MemoryResult:
        if self.backend == "off":
            return self._disabled()
        if not self.ingest_consent:
            raise IngestConsentRequired()
        return self._call(
            SESSION_INGEST_OPERATION,
            payload={
                "connector_namespace": connector_namespace,
                "source_key": source_key,
                "transcript_format": transcript_format,
                "transcript": transcript,
                "source_observed_at": source_observed_at,
            },
            idempotency_key=idempotency_key,
        )
