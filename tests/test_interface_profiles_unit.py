"""Unit tests for compatibility profiles and adapter contracts (R21/R28, F12).

The legacy profile must be an explicit description, never emulated by the v2
API; KOS/OpenKai adapter contracts must not carry app-DB access or OMP
fallback semantics.
"""

from __future__ import annotations

import pytest

from cortex_v2.clients.adapters.kos import (
    FeedResyncRequired,
    KosAdapter,
    OperationUnavailable,
)
from cortex_v2.clients.adapters.openkai import (
    IngestConsentRequired,
    OpenKaiMemoryAdapter,
)
from cortex_v2.clients.errors import ClientConfigError, CortexApiError
from cortex_v2.interface.profiles import (
    KOS_ADAPTER_CONTRACT,
    LEGACY_PROFILE,
    OPENKAI_ADAPTER_CONTRACT,
    V2_PROFILE,
    compatibility_profiles,
)


def test_legacy_profile_is_documented_not_emulated():
    assert LEGACY_PROFILE["profile"] == "legacy-ctx1"
    assert LEGACY_PROFILE["authentication"] == "ctx1 bearer"
    assert "X-Project" in LEGACY_PROFILE["identity_headers"]
    assert "X-Agent-Name" in LEGACY_PROFILE["identity_headers"]
    assert LEGACY_PROFILE["owner_channel"] == "legacy private owner channel"
    assert LEGACY_PROFILE["status"] == (
        "documented-only; the v2 API does not emulate this profile"
    )


def test_v2_profile_declares_headers_and_idempotency():
    assert V2_PROFILE["profile"] == "v2"
    assert V2_PROFILE["authentication"] == "v2 bearer principal"
    assert V2_PROFILE["scope_header"] == "X-Cortex-Scope"
    assert V2_PROFILE["read_scopes_header"] == "X-Cortex-Read-Scopes"
    assert V2_PROFILE["idempotency"] == "Idempotency-Key required on writes"
    assert "registry_version" in V2_PROFILE


def test_profiles_never_mix_headers():
    assert "X-Cortex-Scope" not in LEGACY_PROFILE["identity_headers"]
    assert "X-Project" not in str(V2_PROFILE["identity_headers"])
    assert V2_PROFILE["identity_headers"] == []


def test_kos_contract_forbids_app_db_and_requires_feed_resync():
    assert KOS_ADAPTER_CONTRACT["kos_app_database"] == "not-accessed"
    assert KOS_ADAPTER_CONTRACT["feed_resync"] == "required"
    assert "scheduler" in KOS_ADAPTER_CONTRACT["mapped_surfaces"]
    assert "worktree" in KOS_ADAPTER_CONTRACT["mapped_surfaces"]
    assert "return-outbox" in KOS_ADAPTER_CONTRACT["mapped_surfaces"]


def test_openkai_contract_off_cortex_only_and_no_omp():
    assert OPENKAI_ADAPTER_CONTRACT["memory_backend"] == ["off", "cortex"]
    assert OPENKAI_ADAPTER_CONTRACT["operations"] == ["recall", "record", "learn"]
    assert OPENKAI_ADAPTER_CONTRACT["transcript_ingest"] == "opt-in"
    assert OPENKAI_ADAPTER_CONTRACT["omp_fallback"] == "forbidden"
    assert OPENKAI_ADAPTER_CONTRACT["omp_import"] == "not-authorized"


def test_compatibility_profiles_bundle_every_consumer():
    profiles = compatibility_profiles()
    assert profiles["profile_version"]
    assert profiles["legacy"] == LEGACY_PROFILE
    assert profiles["v2"] == V2_PROFILE
    assert profiles["adapters"]["kos"] == KOS_ADAPTER_CONTRACT
    assert profiles["adapters"]["openkai"] == OPENKAI_ADAPTER_CONTRACT


class _StubRegistry:
    def __init__(self, ids):
        self._ids = set(ids)

    def get(self, operation_id):
        if operation_id not in self._ids:
            raise KeyError(operation_id)
        return operation_id


class _StubClient:
    def __init__(self, results=None, error=None):
        self.calls = []
        self.results = results or {}
        self.error = error

    def call(self, operation_id, **kwargs):
        self.calls.append((operation_id, kwargs))
        if self.error is not None:
            raise self.error
        return self.results.get(operation_id)


def test_kos_adapter_refuses_database_configuration():
    client = _StubClient()
    with pytest.raises(TypeError):
        KosAdapter(client, database_url="postgresql://kos/appdb")


def test_kos_adapter_surfaces_unavailable_operations_honestly():
    client = _StubClient()
    adapter = KosAdapter(client, registry=_StubRegistry(set()))
    with pytest.raises(OperationUnavailable) as excinfo:
        adapter.poll_feed(scope="proj-a")
    assert "feed" in str(excinfo.value).lower()
    assert client.calls == []


def test_kos_return_retries_reuse_the_same_idempotency_key():
    results = {"coordination.return": object()}
    client = _StubClient(results=results)
    adapter = KosAdapter(
        client, registry=_StubRegistry({"coordination.return"})
    )
    first = adapter.publish_return(
        scope="proj-a",
        payload={"handoff_id": "h1", "work_product": {"summary": "done"}},
        idempotency_key="return-key-1",
    )
    second = adapter.publish_return(
        scope="proj-a",
        payload={"handoff_id": "h1", "work_product": {"summary": "done"}},
        idempotency_key="return-key-1",
    )
    assert first is results["coordination.return"]
    assert second is first
    keys = [call[1]["idempotency_key"] for call in client.calls]
    assert keys == ["return-key-1", "return-key-1"]


def test_kos_feed_cursor_expired_requires_resync():
    error = CortexApiError(
        status=409,
        code="cursor_expired",
        message="The feed cursor expired.",
        retryable=False,
        request_id="r1",
        operation_id="feed.poll",
    )
    client = _StubClient(error=error)
    adapter = KosAdapter(client, registry=_StubRegistry({"feed.poll"}))
    with pytest.raises(FeedResyncRequired):
        adapter.poll_feed(scope="proj-a", cursor="old-cursor")


def test_openkai_backend_off_is_explicitly_disabled():
    adapter = OpenKaiMemoryAdapter("off")
    result = adapter.recall("anything")
    assert result.state == "disabled"
    assert adapter.record("note", "text").state == "disabled"
    assert adapter.learn("lesson").state == "disabled"


def test_openkai_backend_cortex_requires_client():
    with pytest.raises(ClientConfigError):
        OpenKaiMemoryAdapter("cortex")


def test_openkai_ingest_requires_consent():
    client = _StubClient()
    adapter = OpenKaiMemoryAdapter("cortex", client=client, scope="proj-a")
    with pytest.raises(IngestConsentRequired):
        adapter.ingest_transcript(
            connector_namespace="openkai",
            source_key="chat-1",
            transcript_format="generic_jsonl",
            transcript='{"role":"user","text":"hi"}\n',
            idempotency_key="k1",
        )
    assert client.calls == []


def test_openkai_unavailable_is_honest_never_fallback():
    error = CortexApiError(
        status=503,
        code="storage_unavailable",
        message="down",
        retryable=True,
        request_id="r",
        operation_id="content.search.lexical",
    )
    client = _StubClient(error=error)
    adapter = OpenKaiMemoryAdapter("cortex", client=client, scope="proj-a")
    result = adapter.recall("query")
    assert result.state == "unavailable"
    assert result.reason == "storage_unavailable"
    assert len(client.calls) == 1  # exactly one attempt, no fallback backend
