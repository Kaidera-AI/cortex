from __future__ import annotations

import asyncio
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlsplit

import asyncpg
import httpx
import pytest

from cortex_v2.config import active_profile
from cortex_v2.migrate import UNGRANTED_ALIAS, UNGRANTED_RECORD_ID, UNGRANTED_SCOPE_ID

PROFILE = active_profile()
EXPECTED_INSTANCE = PROFILE.instance_id

SECRETS = Path(os.environ["CORTEX_V2_SANDBOX_SECRETS_DIR"])
API_URL = os.environ["CORTEX_V2_TEST_API_URL"]
FIXTURE = json.loads((SECRETS / PROFILE.secret_name("fixture")).read_text())
DATABASE_URL = (
    SECRETS / PROFILE.secret_name("database-url-app")
).read_text().strip()
MIGRATOR_DATABASE_URL = (
    SECRETS / PROFILE.secret_name("database-url-migrator")
).read_text().strip()
if (
    urlsplit(API_URL).scheme != "http"
    or urlsplit(API_URL).hostname != PROFILE.api_host
    or urlsplit(API_URL).port != PROFILE.api_port
    or urlsplit(API_URL).username
    or urlsplit(API_URL).password
):
    raise RuntimeError("candidate tests refuse an API outside the candidate network")
for _database_url, _expected_user in (
    (DATABASE_URL, "cortex_v2_app"),
    (MIGRATOR_DATABASE_URL, "cortex_v2_migrator"),
):
    _parsed_database_url = urlsplit(_database_url)
    if (
        _parsed_database_url.scheme != "postgresql"
        or unquote(_parsed_database_url.username or "") != _expected_user
        or _parsed_database_url.hostname != PROFILE.database_host
        or _parsed_database_url.path != "/cortex_v2"
    ):
        raise RuntimeError(
            "candidate tests refuse a database outside the candidate network"
        )
if FIXTURE.get("instance_id") != EXPECTED_INSTANCE:
    raise RuntimeError("candidate fixture does not identify the candidate instance")

WORKER_PRINCIPAL = str(FIXTURE.get("principal_id") or FIXTURE["worker_principal_id"])
UNGRANTED_SCOPE = str(FIXTURE.get("ungranted_scope_id") or UNGRANTED_SCOPE_ID)
UNGRANTED_RECORD = str(FIXTURE.get("ungranted_record_id") or UNGRANTED_RECORD_ID)
UNGRANTED_PROBE_ALIAS = str(FIXTURE.get("ungranted_alias") or UNGRANTED_ALIAS)


class _RedactedAuthorization(str):
    def __repr__(self) -> str:
        return "'Bearer [REDACTED]'"


@pytest.fixture
def auth_headers() -> dict[str, str]:
    token = (
        SECRETS / PROFILE.secret_name("worker-token")
    ).read_text().strip()
    return {
        "Authorization": _RedactedAuthorization(f"Bearer {token}"),
        "X-Cortex-Scope": FIXTURE["project_alias"],
    }


def send_record(
    client: httpx.Client, headers: dict[str, str], key: str, body: str
) -> httpx.Response:
    return client.post(
        "/v1/memory/records",
        headers={**headers, "Idempotency-Key": key},
        json={"record_type": "decision", "body": body},
    )


def scalar(query: str, *args: object) -> int:
    async def run() -> int:
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            async with connection.transaction():
                scopes = str(FIXTURE["project_scope_id"])
                await connection.execute(
                    "SELECT set_config('cortex.principal_id', $1, true)",
                    WORKER_PRINCIPAL,
                )
                await connection.execute(
                    "SELECT set_config('cortex.read_scope_ids', $1, true)", scopes
                )
                return int(await connection.fetchval(query, *args))
        finally:
            await connection.close()

    return asyncio.run(run())


def test_health_and_protocol_are_explicitly_versioned() -> None:
    with httpx.Client(base_url=API_URL, timeout=5) as client:
        live = client.get("/health/live")
        protocol = client.get("/v1/protocol")

    assert live.status_code == 200
    assert live.json() == {"status": "live"}
    assert protocol.status_code == 200
    assert protocol.json()["data"]["api_version"] == "v1"
    assert protocol.json()["data"]["product_version"] == "0.02.001-sandbox"


def test_record_write_is_durable_and_searchable(auth_headers: dict[str, str]) -> None:
    key = str(uuid.uuid4())
    original = "Keep source revisions and tenant boundaries explicit."
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        response = send_record(client, auth_headers, key, original)
        assert response.status_code == 201, response.text
        assert response.json()["data"]["state"] == "committed"
        record = response.json()["data"]
        assert record["revision"] == 1

        readback = client.get(
            f"/v1/memory/records/{record['record_id']}", headers=auth_headers
        )
        assert readback.status_code == 200
        assert readback.json()["data"]["body"] == original

        search = client.post(
            "/v1/searches",
            headers=auth_headers,
            json={"query": "tenant boundaries", "read_scopes": ["sandbox-project"]},
        )
        assert search.status_code == 200
        assert search.json()["data"]["stages_completed"] == ["lexical"]
        assert {"vectors_not_configured", "graphs_not_built"} <= set(
            search.json()["data"]["degraded"]
        )
        result = next(
            hit
            for hit in search.json()["data"]["hits"]
            if hit["record_id"] == record["record_id"]
        )
        assert result["body"] == original
        assert result["evidence"]["source_revision"] == 1
        assert "vectors_not_configured" in search.json()["data"]["degraded"]

    assert scalar(
        "SELECT count(*) FROM cortex_core.memory_records WHERE record_id = $1::uuid",
        record["record_id"],
    ) == 1
    assert scalar(
        "SELECT count(*) FROM cortex_core.outbox_events WHERE aggregate_id = $1::uuid",
        record["record_id"],
    ) == 1


def test_idempotency_replays_original_receipt_and_rejects_key_reuse(
    auth_headers: dict[str, str],
) -> None:
    key = str(uuid.uuid4())
    body = f"{key}: A repeated request must not duplicate a memory record."
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        created = send_record(client, auth_headers, key, body)
        replay = send_record(client, auth_headers, key, body)
        collision = send_record(client, auth_headers, key, body + " Changed.")

    assert created.status_code == 201, created.text
    assert replay.status_code == 200, replay.text
    original_receipt = created.json()["data"]
    replayed_receipt = replay.json()["data"]
    assert original_receipt["state"] == "committed"
    assert replayed_receipt["state"] == "committed"
    assert replayed_receipt == original_receipt
    assert collision.status_code == 409
    assert collision.json()["error"]["code"] == "idempotency_key_reused"
    assert scalar(
        "SELECT count(*) FROM cortex_core.memory_records WHERE body = $1", body
    ) == 1
    assert scalar(
        "SELECT count(*) FROM cortex_core.outbox_events WHERE aggregate_id = $1::uuid",
        created.json()["data"]["record_id"],
    ) == 1


def test_concurrent_same_key_creates_exactly_one_record(
    auth_headers: dict[str, str],
) -> None:
    key = str(uuid.uuid4())
    body = f"{key}: Concurrent retry must have one committed effect."

    def request(_: int) -> httpx.Response:
        with httpx.Client(base_url=API_URL, timeout=15) as client:
            return send_record(client, auth_headers, key, body)

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(request, range(8)))

    assert all(response.status_code in (200, 201) for response in responses), [
        (response.status_code, response.text) for response in responses
    ]
    assert len({response.json()["data"]["record_id"] for response in responses}) == 1
    assert scalar(
        "SELECT count(*) FROM cortex_core.memory_records WHERE body = $1", body
    ) == 1
    assert scalar(
        "SELECT count(*) FROM cortex_core.outbox_events WHERE aggregate_id = $1::uuid",
        responses[0].json()["data"]["record_id"],
    ) == 1


def test_shared_scope_is_readable_only_when_explicit_and_never_writable(
    auth_headers: dict[str, str],
) -> None:
    shared_id = FIXTURE["shared_record_id"]
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        hidden = client.get(f"/v1/memory/records/{shared_id}", headers=auth_headers)
        visible = client.get(
            f"/v1/memory/records/{shared_id}",
            headers={
                **auth_headers,
                "X-Cortex-Read-Scopes": "sandbox-project,shared-library",
            },
        )
        write_shared = client.post(
            "/v1/memory/records",
            headers={
                "Authorization": auth_headers["Authorization"],
                "X-Cortex-Scope": "shared-library",
                "Idempotency-Key": str(uuid.uuid4()),
            },
            json={"record_type": "knowledge", "body": "This write is not permitted."},
        )

    assert hidden.status_code == 404
    assert visible.status_code == 200
    assert visible.json()["data"]["record_id"] == shared_id
    assert write_shared.status_code == 403


def test_local_state_is_not_included_in_project_scope_by_default(
    auth_headers: dict[str, str],
) -> None:
    key = str(uuid.uuid4())
    local_headers = {**auth_headers, "X-Cortex-Scope": "local-state"}
    local_body = "Local state belongs only to an explicit local scope."
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        created = send_record(client, local_headers, key, local_body)
        hidden = client.post(
            "/v1/searches",
            headers=auth_headers,
            json={"query": "explicit local scope", "read_scopes": ["sandbox-project"]},
        )
        visible = client.post(
            "/v1/searches",
            headers=auth_headers,
            json={
                "query": "explicit local scope",
                "read_scopes": ["sandbox-project", "local-state"],
            },
        )

    assert created.status_code == 201, created.text
    assert all(hit["body"] != local_body for hit in hidden.json()["data"]["hits"])
    assert any(hit["body"] == local_body for hit in visible.json()["data"]["hits"])


def test_unknown_scope_and_missing_or_invalid_credentials_fail_closed(
    auth_headers: dict[str, str],
) -> None:
    with httpx.Client(base_url=API_URL, timeout=5) as client:
        no_token = client.get("/v1/capabilities")
        bad_token = client.get(
            "/v1/capabilities", headers={"Authorization": "Bearer not-a-valid-token"}
        )
        unknown_scope = client.post(
            "/v1/searches",
            headers=auth_headers,
            json={
                "query": "anything",
                "read_scopes": ["sandbox-project", "unregistered-foreign-scope"],
            },
        )

    assert no_token.status_code == 401
    assert bad_token.status_code == 401
    assert unknown_scope.status_code == 404
    assert "unregistered-foreign-scope" not in unknown_scope.text


def test_ungranted_probe_alias_is_indistinguishable_from_unknown_scope(
    auth_headers: dict[str, str],
) -> None:
    with httpx.Client(base_url=API_URL, timeout=5) as client:
        unknown = client.post(
            "/v1/searches",
            headers=auth_headers,
            json={
                "query": "anything",
                "read_scopes": ["sandbox-project", "unregistered-foreign-scope"],
            },
        )
        probe = client.post(
            "/v1/searches",
            headers=auth_headers,
            json={
                "query": "anything",
                "read_scopes": [
                    "sandbox-project",
                    UNGRANTED_PROBE_ALIAS,
                ],
            },
        )

    assert unknown.status_code == 404
    assert probe.status_code == 404
    assert probe.json()["error"] == unknown.json()["error"]
    assert UNGRANTED_PROBE_ALIAS not in probe.text


def test_ungranted_probe_record_is_hidden_from_project_scope(
    auth_headers: dict[str, str],
) -> None:
    with httpx.Client(base_url=API_URL, timeout=5) as client:
        response = client.get(
            f"/v1/memory/records/{UNGRANTED_RECORD}",
            headers=auth_headers,
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "record_not_found"
    assert UNGRANTED_RECORD not in response.text


def test_forged_ungranted_read_scope_cannot_read_probe_record_or_alias() -> None:
    async def run() -> tuple[int, int, bool]:
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('cortex.principal_id', $1, true)",
                    WORKER_PRINCIPAL,
                )
                await connection.execute(
                    "SELECT set_config('cortex.read_scope_ids', $1, true)",
                    UNGRANTED_SCOPE,
                )
                records = int(
                    await connection.fetchval(
                        """
                        SELECT count(*)
                          FROM cortex_core.memory_records
                         WHERE scope_id = $1::uuid AND record_id = $2::uuid
                        """,
                        UNGRANTED_SCOPE,
                        UNGRANTED_RECORD,
                    )
                )
                aliases = int(
                    await connection.fetchval(
                        """
                        SELECT count(*)
                          FROM cortex_core.scope_aliases
                         WHERE alias = $1
                        """,
                        UNGRANTED_PROBE_ALIAS,
                    )
                )
                policy_allows = await connection.fetchval(
                    "SELECT cortex_core.scope_read_policy($1::uuid)",
                    UNGRANTED_SCOPE,
                )
                return records, aliases, bool(policy_allows)
        finally:
            await connection.close()

    assert asyncio.run(run()) == (0, 0, False)


def test_unset_database_scope_cannot_read_tenant_rows() -> None:
    async def run() -> int:
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            return int(
                await connection.fetchval(
                    "SELECT count(*) FROM cortex_core.memory_records"
                )
            )
        finally:
            await connection.close()

    assert asyncio.run(run()) == 0


def test_unscoped_application_role_cannot_enumerate_registry_or_credentials() -> None:
    async def run() -> tuple[str, bool, int, int, int]:
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            async with connection.transaction():
                identity = await connection.fetchrow(
                    """
                    SELECT current_user AS role, rolbypassrls
                      FROM pg_roles
                     WHERE rolname = current_user
                    """
                )
                grants = int(
                    await connection.fetchval(
                        "SELECT count(*) FROM cortex_auth.scope_grants"
                    )
                )
                scopes = int(
                    await connection.fetchval("SELECT count(*) FROM cortex_core.scopes")
                )
                aliases = int(
                    await connection.fetchval(
                        "SELECT count(*) FROM cortex_core.scope_aliases"
                    )
                )
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await connection.fetchval(
                        "SELECT count(*) FROM cortex_auth.credentials"
                    )
                return (
                    identity["role"],
                    identity["rolbypassrls"],
                    grants,
                    scopes,
                    aliases,
                )
        finally:
            await connection.close()

    role, bypasses_rls, grants, scopes, aliases = asyncio.run(run())
    assert role == "cortex_v2_app"
    assert not bypasses_rls
    assert (grants, scopes, aliases) == (0, 0, 0)


def test_outbox_failure_rolls_back_record_and_receipt(
    auth_headers: dict[str, str],
) -> None:
    key = str(uuid.uuid4())
    body = "The entire accepted command must roll back on event failure."

    async def install_fault() -> None:
        connection = await asyncpg.connect(MIGRATOR_DATABASE_URL)
        try:
            await connection.execute(
                "CREATE OR REPLACE FUNCTION cortex_core.sandbox_reject_outbox() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'sandbox injected outbox failure'; END $$"
            )
            await connection.execute(
                "CREATE TRIGGER sandbox_reject_outbox BEFORE INSERT ON "
                "cortex_core.outbox_events FOR EACH ROW EXECUTE FUNCTION "
                "cortex_core.sandbox_reject_outbox()"
            )
        finally:
            await connection.close()

    async def remove_fault() -> None:
        connection = await asyncpg.connect(MIGRATOR_DATABASE_URL)
        try:
            await connection.execute(
                "DROP TRIGGER IF EXISTS sandbox_reject_outbox "
                "ON cortex_core.outbox_events"
            )
            await connection.execute(
                "DROP FUNCTION IF EXISTS cortex_core.sandbox_reject_outbox()"
            )
        finally:
            await connection.close()

    asyncio.run(install_fault())
    try:
        with httpx.Client(base_url=API_URL, timeout=10) as client:
            response = send_record(client, auth_headers, key, body)
    finally:
        asyncio.run(remove_fault())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert scalar(
        "SELECT count(*) FROM cortex_core.memory_records WHERE body = $1", body
    ) == 0
    assert scalar(
        "SELECT count(*) FROM cortex_core.idempotency_receipts "
        "WHERE idempotency_key = $1", key
    ) == 0
