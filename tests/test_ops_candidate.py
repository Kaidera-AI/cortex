from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import asyncpg
import httpx
import pytest

from cortex_v2.config import KAI_TEST_INSTANCE, W1_INSTANCE, active_profile

PROFILE = active_profile()
if PROFILE.instance_id not in (W1_INSTANCE, KAI_TEST_INSTANCE):
    pytest.skip(
        "ops candidate suite requires the w1 candidate or integrated instance",
        allow_module_level=True,
    )

SECRETS = Path(os.environ["CORTEX_V2_SANDBOX_SECRETS_DIR"])
API_URL = os.environ["CORTEX_V2_TEST_API_URL"]
FIXTURE = json.loads((SECRETS / PROFILE.secret_name("fixture")).read_text())
DATABASE_URL = (SECRETS / PROFILE.secret_name("database-url-app")).read_text().strip()
MIGRATOR_DATABASE_URL = (
    SECRETS / PROFILE.secret_name("database-url-migrator")
).read_text().strip()
OWNER_TOKEN = (SECRETS / PROFILE.secret_name("owner-token")).read_text().strip()
WORKER_TOKEN = (SECRETS / PROFILE.secret_name("worker-token")).read_text().strip()

PROJECT_ALIAS = FIXTURE["project_alias"]
PROJECT_SCOPE = FIXTURE["project_scope_id"]
WORKER_PRINCIPAL = FIXTURE["worker_principal_id"]


def _ops_migration_applied() -> bool:
    async def run() -> bool:
        try:
            connection = await asyncpg.connect(MIGRATOR_DATABASE_URL)
        except (OSError, asyncpg.PostgresError):
            return False
        try:
            return bool(
                await connection.fetchval(
                    "SELECT 1 FROM cortex_core.schema_migrations "
                    "WHERE migration_id = '0008_ops.sql'"
                )
            )
        finally:
            await connection.close()

    return asyncio.run(run())


if not _ops_migration_applied():
    pytest.skip(
        "ops migration 0008_ops.sql is not applied on this candidate yet",
        allow_module_level=True,
    )

class _RedactedAuthorization(str):
    def __repr__(self) -> str:
        return "'Bearer [REDACTED]'"


def headers(
    token: str,
    scope: str | None = None,
    key: str | None = None,
    read_scopes: str | None = None,
) -> dict[str, str]:
    result: dict[str, str] = {
        "Authorization": _RedactedAuthorization(f"Bearer {token}")
    }
    if scope:
        result["X-Cortex-Scope"] = scope
    if key:
        result["Idempotency-Key"] = key
    if read_scopes:
        result["X-Cortex-Read-Scopes"] = read_scopes
    return result


def _token_is_live(token: str) -> bool:
    """The seeded owner credential is one-shot across suites: the W1 recovery
    test (and any earlier owner-recovery use) rotates it on a shared database.
    Owner-path assertions run fully on fresh-DB ordering and degrade to an
    explicit skip — never a false pass — once the fixture token is consumed."""
    try:
        with httpx.Client(base_url=API_URL, timeout=10) as client:
            return (
                client.get("/v1/auth/principal", headers=headers(token)).status_code
                == 200
            )
    except (OSError, httpx.HTTPError):
        return False


OWNER_TOKEN_LIVE = _token_is_live(OWNER_TOKEN)


def db_value(query: str, *args: object):
    async def run():
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('cortex.principal_id', $1, true)",
                    WORKER_PRINCIPAL,
                )
                await connection.execute(
                    "SELECT set_config('cortex.read_scope_ids', $1, true)",
                    PROJECT_SCOPE,
                )
                await connection.execute(
                    "SELECT set_config('cortex.write_scope_id', $1, true)",
                    PROJECT_SCOPE,
                )
                return await connection.fetchval(query, *args)
        finally:
            await connection.close()

    return asyncio.run(run())


def test_doctor_reports_configured_versus_applied() -> None:
    with httpx.Client(base_url=API_URL, timeout=15) as client:
        response = client.get(
            "/v1/ops/doctor", headers=headers(WORKER_TOKEN, PROJECT_ALIAS)
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["state"] in ("ok", "degraded")
    checks = {check["check_id"]: check for check in data["checks"]}
    assert checks["runtime_role"]["status"] == "ok"
    assert checks["forced_rls"]["status"] == "ok"
    assert checks["transaction_context"]["status"] == "ok"
    assert checks["auth_function_definer"]["status"] == "ok"
    assert "migrations" in checks
    for check in checks.values():
        assert set(check) >= {"check_id", "configured", "applied", "status"}


def test_migration_status_lists_applied_and_expected() -> None:
    with httpx.Client(base_url=API_URL, timeout=15) as client:
        response = client.get(
            "/v1/ops/migration-status", headers=headers(WORKER_TOKEN, PROJECT_ALIAS)
        )

    assert response.status_code == 200
    data = response.json()["data"]
    states = {entry["migration_id"]: entry["state"] for entry in data["migrations"]}
    assert states["0001_core.sql"] == "applied"
    assert states["0002_w1_identity_memory.sql"] == "applied"
    assert states["0008_ops.sql"] == "applied"
    assert all(state != "checksum_mismatch" for state in states.values())
    assert data["expected_order"][0] == "0001_core.sql"


def test_release_status_names_the_candidate_lane() -> None:
    with httpx.Client(base_url=API_URL, timeout=15) as client:
        response = client.get(
            "/v1/ops/release-status", headers=headers(WORKER_TOKEN, PROJECT_ALIAS)
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["instance_id"] == PROFILE.instance_id
    assert data["deployment_class"] == PROFILE.deployment_class
    assert data["contract_version"] == PROFILE.contract_version
    assert data["release_class"] == "local-candidate-evidence"
    assert "0008_ops.sql" in data["schema_expected"]


def test_metrics_exposes_counters_and_honest_unavailability() -> None:
    with httpx.Client(base_url=API_URL, timeout=15) as client:
        response = client.get(
            "/v1/ops/metrics", headers=headers(WORKER_TOKEN, PROJECT_ALIAS)
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert isinstance(data["content"]["memory_records"], int)
    assert isinstance(data["outbox"]["content"]["undelivered"], int)
    assert "denial_counters_not_persisted" in data["unavailable"]
    assert data["coverage"]["selected_scope"] == PROJECT_ALIAS


def test_retention_policy_is_owner_enacted_and_revision_checked() -> None:
    with httpx.Client(base_url=API_URL, timeout=15) as client:
        status = client.get(
            "/v1/ops/retention-status", headers=headers(WORKER_TOKEN, PROJECT_ALIAS)
        )
        assert status.status_code == 200
        current = next(
            (
                policy["revision"]
                for policy in status.json()["data"]["policies"]
                if policy["content_class"] == "diary"
            ),
            0,
        )
        denied = client.post(
            "/v1/ops/retention-policies",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS, str(uuid.uuid4())),
            json={
                "content_class": "diary",
                "min_age_days": 0,
                "action": "archive",
                "expected_revision": current,
            },
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "owner_authority_required"
        if not OWNER_TOKEN_LIVE:
            pytest.skip(
                "seeded owner credential already rotated on this shared database; "
                "owner enactment path is asserted on fresh-DB suite ordering"
            )
        enacted = client.post(
            "/v1/ops/retention-policies",
            headers=headers(OWNER_TOKEN, PROJECT_ALIAS, str(uuid.uuid4())),
            json={
                "content_class": "diary",
                "min_age_days": 0,
                "action": "archive",
                "expected_revision": current,
            },
        )
        stale = client.post(
            "/v1/ops/retention-policies",
            headers=headers(OWNER_TOKEN, PROJECT_ALIAS, str(uuid.uuid4())),
            json={
                "content_class": "diary",
                "min_age_days": 0,
                "action": "archive",
                "expected_revision": current,
            },
        )

    assert enacted.status_code == 201
    assert enacted.json()["data"]["policy_revision"] == current + 1
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "policy_revision_conflict"


def test_archive_ledger_dry_run_apply_restore_cycle() -> None:
    marker = f"retention diary {uuid.uuid4().hex}"
    archive_token = OWNER_TOKEN if OWNER_TOKEN_LIVE else WORKER_TOKEN
    # Archiving is a scope write, but the diary policy enactment that makes
    # rows eligible is owner-gated; the skip below is explicit when the
    # seeded owner credential was already rotated by an earlier suite.
    with httpx.Client(base_url=API_URL, timeout=20) as client:
        created = client.post(
            "/v1/content",
            headers=headers(archive_token, PROJECT_ALIAS, str(uuid.uuid4())),
            json={
                "content_class": "diary",
                "payload": {"entry": marker},
                "body": marker,
            },
        )
        assert created.status_code == 201
        content_id = created.json()["data"]["content_id"]

        if not OWNER_TOKEN_LIVE:
            pytest.skip(
                "retention archiving needs an enacted diary policy; owner "
                "credential already rotated on this shared database"
            )

        dry = client.post(
            "/v1/ops/retention:archive",
            headers=headers(archive_token, PROJECT_ALIAS, str(uuid.uuid4())),
            json={"content_class": "diary", "dry_run": True},
        )
        assert dry.status_code == 200
        dry_data = dry.json()["data"]
        assert dry_data["dry_run"] is True
        assert any(
            entry["content_id"] == content_id for entry in dry_data["entries"]
        )

        applied = client.post(
            "/v1/ops/retention:archive",
            headers=headers(archive_token, PROJECT_ALIAS, str(uuid.uuid4())),
            json={"content_class": "diary"},
        )
        assert applied.status_code == 201
        entries = applied.json()["data"]["entries"]
        target = next(entry for entry in entries if entry["content_id"] == content_id)
        entry_id = target["entry_id"]

        again = client.post(
            "/v1/ops/retention:archive",
            headers=headers(archive_token, PROJECT_ALIAS, str(uuid.uuid4())),
            json={"content_class": "diary"},
        )
        assert again.status_code == 201
        assert all(
            entry["content_id"] != content_id
            for entry in again.json()["data"]["entries"]
        )

        restored = client.post(
            "/v1/ops/repairs",
            headers=headers(archive_token, PROJECT_ALIAS, str(uuid.uuid4())),
            json={"repair_kind": "retention_restore", "target_reference": entry_id},
        )
        assert restored.status_code == 200
        assert restored.json()["data"]["outcome"] == "applied"

        second_restore = client.post(
            "/v1/ops/repairs",
            headers=headers(archive_token, PROJECT_ALIAS, str(uuid.uuid4())),
            json={"repair_kind": "retention_restore", "target_reference": entry_id},
        )
        assert second_restore.status_code == 200
        assert second_restore.json()["data"]["outcome"] == "precondition_failed"

        unknown = client.post(
            "/v1/ops/repairs",
            headers=headers(archive_token, PROJECT_ALIAS, str(uuid.uuid4())),
            json={
                "repair_kind": "retention_restore",
                "target_reference": str(uuid.uuid4()),
            },
        )
        assert unknown.status_code == 200
        assert unknown.json()["data"]["outcome"] == "not_found"

        invalid = client.post(
            "/v1/ops/repairs",
            headers=headers(archive_token, PROJECT_ALIAS, str(uuid.uuid4())),
            json={"repair_kind": "retention_restore", "target_reference": "not-a-uuid"},
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "invalid_target"

        original = client.get(
            f"/v1/content/{content_id}",
            headers=headers(archive_token, PROJECT_ALIAS),
        )
    assert original.status_code == 200
    assert original.json()["data"]["body"] == marker


def test_backup_manifest_verifies_then_detects_drift() -> None:
    with httpx.Client(base_url=API_URL, timeout=30) as client:
        key = str(uuid.uuid4())
        created = client.post(
            "/v1/ops/backups", headers=headers(WORKER_TOKEN, PROJECT_ALIAS, key)
        )
        assert created.status_code == 201
        manifest = created.json()["data"]
        manifest_id = manifest["manifest_id"]
        assert manifest["row_visibility"] == "rls-scoped-caller-view"

        replayed = client.post(
            "/v1/ops/backups", headers=headers(WORKER_TOKEN, PROJECT_ALIAS, key)
        )
        assert replayed.status_code == 200
        assert replayed.headers["Idempotent-Replay"] == "true"
        assert replayed.json()["data"]["manifest_id"] == manifest_id

        verified = client.get(
            f"/v1/ops/backups/{manifest_id}/verify",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
        )
        assert verified.status_code == 200
        assert verified.json()["data"]["state"] == "verified"

        marker = f"drift marker {uuid.uuid4().hex}"
        drift = client.post(
            "/v1/content",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS, str(uuid.uuid4())),
            json={
                "content_class": "knowledge",
                "payload": {"content": marker},
                "body": marker,
            },
        )
        assert drift.status_code == 201

        after = client.get(
            f"/v1/ops/backups/{manifest_id}/verify",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
        )
        assert after.status_code == 200
        after_data = after.json()["data"]
        assert after_data["state"] == "drifted"
        assert "cortex_core.content_items" in after_data["drifted_tables"]

        unknown = client.get(
            f"/v1/ops/backups/{uuid.uuid4()}/verify",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
        )
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "manifest_not_found"

        replay_after_drift = client.post(
            "/v1/ops/backups",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS, key),
        )
    assert replay_after_drift.status_code == 200
    assert replay_after_drift.json()["data"]["manifest_id"] == manifest_id


def test_backup_verify_reports_coverage_differs_not_phantom_drift() -> None:
    local_alias = FIXTURE["local_alias"]
    with httpx.Client(base_url=API_URL, timeout=30) as client:
        created = client.post(
            "/v1/ops/backups",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS, str(uuid.uuid4())),
        )
        assert created.status_code == 201
        manifest_id = created.json()["data"]["manifest_id"]

        same = client.get(
            f"/v1/ops/backups/{manifest_id}/verify",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
        )
        assert same.status_code == 200
        assert same.json()["data"]["state"] == "verified"

        wider = client.get(
            f"/v1/ops/backups/{manifest_id}/verify",
            headers=headers(
                WORKER_TOKEN,
                PROJECT_ALIAS,
                read_scopes=f"{PROJECT_ALIAS},{local_alias}",
            ),
        )
        assert wider.status_code == 200
        wider_data = wider.json()["data"]
        assert wider_data["state"] == "coverage_differs"
        assert wider_data["coverage_same"] is False
        assert "drifted_tables" not in wider_data

        stable = client.get(
            f"/v1/ops/backups/{manifest_id}/verify",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
        )
        assert stable.status_code == 200
        assert stable.json()["data"]["state"] == "verified"
