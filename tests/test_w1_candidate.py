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
        "W1 behavior suite requires the w1 candidate or integrated instance",
        allow_module_level=True,
    )

SECRETS = Path(os.environ["CORTEX_V2_SANDBOX_SECRETS_DIR"])
API_URL = os.environ["CORTEX_V2_TEST_API_URL"]
FIXTURE = json.loads((SECRETS / PROFILE.secret_name("fixture")).read_text())
DATABASE_URL = (SECRETS / PROFILE.secret_name("database-url-app")).read_text().strip()
MIGRATOR_DATABASE_URL = (
    SECRETS / PROFILE.secret_name("database-url-migrator")
).read_text().strip()
WORKER_TOKEN = (SECRETS / PROFILE.secret_name("worker-token")).read_text().strip()


def _owner_token_live(token: str) -> bool:
    try:
        with httpx.Client(base_url=API_URL, timeout=10) as client:
            return (
                client.get(
                    "/v1/auth/principal", headers={"Authorization": f"Bearer {token}"}
                ).status_code
                == 200
            )
    except (OSError, httpx.HTTPError):
        return False


def _resolve_live_owner() -> str:
    owner = (SECRETS / PROFILE.secret_name("owner-token")).read_text().strip()
    if _owner_token_live(owner):
        return owner
    pytest.skip(
        "T19: seeded owner credential unavailable; recovery requires Kai written approval",
        allow_module_level=True,
    )


OWNER_TOKEN = _resolve_live_owner()

PROJECT_ALIAS = FIXTURE["project_alias"]
SHARED_ALIAS = FIXTURE["shared_alias"]
LOCAL_ALIAS = FIXTURE["local_alias"]
OWNER_PRINCIPAL = FIXTURE["owner_principal_id"]
WORKER_PRINCIPAL = FIXTURE["worker_principal_id"]
PROJECT_SCOPE = FIXTURE["project_scope_id"]
LOCAL_SCOPE = FIXTURE["local_scope_id"]
CONNECTOR_NAMESPACE = "w1-session"


class _RedactedAuthorization(str):
    def __repr__(self) -> str:
        return "'Bearer [REDACTED]'"


def headers(
    token: str,
    scope: str | None = None,
    key: str | None = None,
    read_scopes: str | None = None,
) -> dict[str, str]:
    result: dict[str, str] = {}
    if token:
        result["Authorization"] = _RedactedAuthorization(f"Bearer {token}")
    if scope:
        result["X-Cortex-Scope"] = scope
    if key:
        result["Idempotency-Key"] = key
    if read_scopes:
        result["X-Cortex-Read-Scopes"] = read_scopes
    return result


def db_value(
    query: str,
    *args: object,
    principal: str | None = None,
    read_scopes: str | None = None,
    write_scope: str | None = None,
    policy_revision: str | None = None,
):
    async def run():
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            async with connection.transaction():
                if principal:
                    await connection.execute(
                        "SELECT set_config('cortex.principal_id', $1, true)", principal
                    )
                if read_scopes:
                    await connection.execute(
                        "SELECT set_config('cortex.read_scope_ids', $1, true)",
                        read_scopes,
                    )
                if write_scope is not None:
                    await connection.execute(
                        "SELECT set_config('cortex.write_scope_id', $1, true)",
                        write_scope,
                    )
                if policy_revision is not None:
                    await connection.execute(
                        "SELECT set_config('cortex.policy_revision', $1, true)",
                        policy_revision,
                    )
                return await connection.fetchval(query, *args)
        finally:
            await connection.close()

    return asyncio.run(run())


def migrator_value(query: str, *args: object):
    async def run():
        connection = await asyncpg.connect(MIGRATOR_DATABASE_URL)
        try:
            return await connection.fetchval(query, *args)
        finally:
            await connection.close()

    return asyncio.run(run())


def create_content(
    client: httpx.Client,
    token: str,
    body: str,
    *,
    scope: str = PROJECT_ALIAS,
    content_class: str = "knowledge",
    payload: dict | None = None,
    key: str | None = None,
) -> httpx.Response:
    return client.post(
        "/v1/content",
        headers=headers(token, scope, key or str(uuid.uuid4())),
        json={
            "content_class": content_class,
            "payload": payload or {"content": body},
            "body": body,
        },
    )


def enroll(
    client: httpx.Client,
    name: str,
    *,
    token: str | None = None,
    scopes: list | None = None,
    key: str | None = None,
) -> httpx.Response:
    payload: dict = {"principal_name": name, "actor_kind": "agent"}
    if scopes is not None:
        payload["scopes"] = scopes
    return client.post(
        "/v1/auth/principals:enroll",
        headers=headers(OWNER_TOKEN if token is None else token, key=key or str(uuid.uuid4())),
        json=payload,
    )


# ---------------------------------------------------------------------------
# R04: ServiceAuth lifecycle under live calls
# ---------------------------------------------------------------------------


def test_self_profile_reports_canonical_identity_not_caller_names() -> None:
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        worker = client.get("/v1/auth/principal", headers=headers(WORKER_TOKEN))
        owner = client.get("/v1/auth/principal", headers=headers(OWNER_TOKEN))

    assert worker.status_code == 200
    assert worker.json()["data"]["principal_id"] == WORKER_PRINCIPAL
    assert owner.json()["data"]["principal_id"] == OWNER_PRINCIPAL
    worker_scopes = {
        scope["scope_id"]: scope for scope in worker.json()["data"]["scopes"]
    }
    assert worker_scopes[PROJECT_SCOPE]["can_write"] is True
    assert worker_scopes[PROJECT_SCOPE]["can_publish"] is False


def test_privileged_operations_are_owner_only() -> None:
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        worker_enroll = enroll(client, "w1-impersonator", token=WORKER_TOKEN)
        worker_roster = client.post(
            f"/v1/scopes/{PROJECT_ALIAS}/roster",
            headers=headers(WORKER_TOKEN, key=str(uuid.uuid4())),
            json={
                "entries": [
                    {"principal_id": WORKER_PRINCIPAL, "role": "owner"},
                ]
            },
        )
        worker_actions = client.get(
            "/v1/auth/privileged-actions", headers=headers(WORKER_TOKEN)
        )
        worker_connector = client.post(
            "/v1/connectors",
            headers=headers(WORKER_TOKEN, key=str(uuid.uuid4())),
            json={"namespace": "worker-connector", "connector_kind": "manual"},
        )

    assert worker_enroll.status_code == 403
    assert worker_enroll.json()["error"]["code"] == "owner_authority_required"
    assert worker_roster.status_code == 403
    assert worker_actions.status_code == 403
    assert worker_connector.status_code == 403


def test_owner_enrolls_principal_with_scoped_binding_and_token_is_single_use() -> None:
    name = f"w1-enrolled-{uuid.uuid4().hex[:8]}"
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        created = enroll(
            client,
            name,
            scopes=[{"alias": PROJECT_ALIAS, "can_read": True, "can_write": True}],
        )
        assert created.status_code == 201
        data = created.json()["data"]
        assert data["state"] == "committed"
        assert data["generation"] == 1
        new_token = data["token"]
        replay = client.post(
            "/v1/auth/principals:enroll",
            headers=headers(
                OWNER_TOKEN, key=created.request.headers["Idempotency-Key"]
            ),
            json={
                "principal_name": name,
                "actor_kind": "agent",
                "scopes": [
                    {"alias": PROJECT_ALIAS, "can_read": True, "can_write": True}
                ],
            },
        )
        assert replay.status_code == 200
        assert replay.headers["Idempotent-Replay"] == "true"
        replayed = replay.json()["data"]
        assert replayed["principal_id"] == data["principal_id"]
        assert "token" not in replayed
        assert replayed["token_fingerprint"] == data["token_fingerprint"]

        unknown_scope = enroll(
            client,
            f"w1-unknown-scope-{uuid.uuid4().hex[:8]}",
            scopes=[{"alias": "no-such-scope", "can_read": True}],
        )
        assert unknown_scope.status_code == 404

        who = client.get("/v1/auth/principal", headers=headers(new_token))
        assert who.status_code == 200
        assert who.json()["data"]["principal_id"] == data["principal_id"]

        wrote = create_content(
            client, new_token, f"{name} writes through its own grant"
        )
        assert wrote.status_code == 201


def test_credential_rotation_issues_new_generation() -> None:
    name = f"w1-rotate-{uuid.uuid4().hex[:8]}"
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        created = enroll(client, name)
        old_token = created.json()["data"]["token"]
        principal_id = created.json()["data"]["principal_id"]
        assert (
            client.get("/v1/auth/principal", headers=headers(old_token)).status_code
            == 200
        )

        rotated = client.post(
            "/v1/auth/credentials:rotate",
            headers=headers(old_token, key=str(uuid.uuid4())),
            json={},
        )
        assert rotated.status_code == 201
        rotation = rotated.json()["data"]
        assert rotation["generation"] == 2
        assert rotation["principal_id"] == principal_id
        new_token = rotation["token"]

        assert (
            client.get("/v1/auth/principal", headers=headers(new_token)).status_code
            == 200
        )

        worker_rotating_owner = client.post(
            "/v1/auth/credentials:rotate",
            headers=headers(WORKER_TOKEN, key=str(uuid.uuid4())),
            json={"principal_id": OWNER_PRINCIPAL},
        )
    assert worker_rotating_owner.status_code == 403



def test_revoked_credential_during_use_stops_new_calls() -> None:
    name = f"w1-revoke-{uuid.uuid4().hex[:8]}"
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        created = enroll(
            client,
            name,
            scopes=[{"alias": PROJECT_ALIAS, "can_read": True, "can_write": True}],
        )
        enrolled_token = created.json()["data"]["token"]
        credential_id = created.json()["data"]["credential_id"]
        assert (
            create_content(client, enrolled_token, f"{name} before revocation").status_code
            == 201
        )

        revoked = client.post(
            "/v1/auth/credentials:revoke",
            headers=headers(OWNER_TOKEN, key=str(uuid.uuid4())),
            json={"credential_id": credential_id},
        )
        assert revoked.status_code == 200
        assert revoked.json()["data"]["state"] == "committed"

        after = create_content(client, enrolled_token, f"{name} after revocation")
        profile_call = client.get(
            "/v1/auth/principal", headers=headers(enrolled_token)
        )

    assert after.status_code == 401
    assert profile_call.status_code == 401


def test_principal_revocation_blocks_authentication() -> None:
    name = f"w1-principal-revoke-{uuid.uuid4().hex[:8]}"
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        created = enroll(client, name)
        principal_token = created.json()["data"]["token"]
        principal_id = created.json()["data"]["principal_id"]
        revoked = client.post(
            "/v1/auth/principals:revoke",
            headers=headers(OWNER_TOKEN, key=str(uuid.uuid4())),
            json={"principal_id": principal_id},
        )
        after = client.get("/v1/auth/principal", headers=headers(principal_token))
        self_revoke = client.post(
            "/v1/auth/principals:revoke",
            headers=headers(OWNER_TOKEN, key=str(uuid.uuid4())),
            json={"principal_id": OWNER_PRINCIPAL},
        )

    assert revoked.status_code == 200
    assert after.status_code == 401
    assert self_revoke.status_code == 422


def test_revoked_grant_shrinks_cached_alias_and_rebind_restores_it() -> None:
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        revoked = client.post(
            "/v1/auth/scopes:revoke",
            headers=headers(OWNER_TOKEN, key=str(uuid.uuid4())),
            json={"principal_id": WORKER_PRINCIPAL, "alias": PROJECT_ALIAS},
        )
        assert revoked.status_code == 200
        denied_write = create_content(client, WORKER_TOKEN, "cached alias cannot write")
        denied_search = client.post(
            "/v1/content/searches",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
            json={"query": "anything", "read_scopes": [PROJECT_ALIAS]},
        )
        profile = client.get("/v1/auth/principal", headers=headers(WORKER_TOKEN))
        assert PROJECT_SCOPE not in {
            scope["scope_id"] for scope in profile.json()["data"]["scopes"]
        }

        rebound = client.post(
            "/v1/auth/scopes:bind",
            headers=headers(OWNER_TOKEN, key=str(uuid.uuid4())),
            json={
                "principal_id": WORKER_PRINCIPAL,
                "alias": PROJECT_ALIAS,
                "can_read": True,
                "can_write": True,
                "can_publish": False,
            },
        )
        restored = create_content(client, WORKER_TOKEN, "rebound grant writes again")

    assert denied_write.status_code == 404
    assert denied_write.json()["error"]["code"] == "scope_not_found"
    assert denied_search.status_code == 404
    assert rebound.status_code == 200
    assert restored.status_code == 201


def test_privileged_action_audit_is_recorded_for_owner() -> None:
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        actions = client.get(
            "/v1/auth/privileged-actions?limit=200", headers=headers(OWNER_TOKEN)
        )

    assert actions.status_code == 200
    action_types = {
        action["action_type"] for action in actions.json()["data"]["actions"]
    }
    assert {
        "enroll_principal",
        "rotate_credential",
        "revoke_credential",
        "revoke_principal",
        "revoke_scope_grant",
        "bind_scope",
    } <= action_types


# ---------------------------------------------------------------------------
# R03: aliases, roster, writer policy
# ---------------------------------------------------------------------------


def test_alias_rename_preserves_identity_and_old_alias_resolution() -> None:
    new_alias = f"w1-renamed-{uuid.uuid4().hex[:8]}"
    marker = f"rename identity marker {uuid.uuid4()}"
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        created = create_content(client, WORKER_TOKEN, marker)
        assert created.status_code == 201
        content_id = created.json()["data"]["content_id"]

        renamed = client.post(
            f"/v1/scopes/{PROJECT_ALIAS}:rename",
            headers=headers(OWNER_TOKEN, key=str(uuid.uuid4())),
            json={"new_alias": new_alias},
        )
        assert renamed.status_code == 200
        receipt = renamed.json()["data"]
        assert receipt["state"] == "verified_effect"
        assert receipt["scope_id"] == PROJECT_SCOPE
        assert receipt["retained_alias"] == PROJECT_ALIAS

        via_old = client.get(
            f"/v1/content/{content_id}", headers=headers(WORKER_TOKEN, PROJECT_ALIAS)
        )
        via_new = client.get(
            f"/v1/content/{content_id}", headers=headers(WORKER_TOKEN, new_alias)
        )
        conflict = client.post(
            f"/v1/scopes/{PROJECT_ALIAS}:rename",
            headers=headers(OWNER_TOKEN, key=str(uuid.uuid4())),
            json={"new_alias": SHARED_ALIAS},
        )
        worker_rename = client.post(
            f"/v1/scopes/{PROJECT_ALIAS}:rename",
            headers=headers(WORKER_TOKEN, key=str(uuid.uuid4())),
            json={"new_alias": f"w1-worker-{uuid.uuid4().hex[:8]}"},
        )

    assert via_old.status_code == 200
    assert via_old.json()["data"]["content_id"] == content_id
    assert via_new.status_code == 200
    assert via_new.json()["data"]["scope_id"] == PROJECT_SCOPE
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "alias_reserved"
    assert worker_rename.status_code == 403


def test_roster_and_writer_policy_gate_scope_writers() -> None:
    with httpx.Client(base_url=API_URL, timeout=15) as client:
        baseline = client.get(
            f"/v1/scopes/{PROJECT_ALIAS}/roster", headers=headers(WORKER_TOKEN)
        )
        assert baseline.status_code == 200
        prior_roster = baseline.json()["data"]["roster_revision"]
        prior_policy = baseline.json()["data"]["writer_policy"]["revision"]

        ambiguous = client.post(
            f"/v1/scopes/{PROJECT_ALIAS}/roster",
            headers=headers(OWNER_TOKEN, key=str(uuid.uuid4())),
            json={
                "entries": [
                    {"principal_id": OWNER_PRINCIPAL, "role": "owner"},
                    {"principal_id": WORKER_PRINCIPAL, "role": "owner"},
                ]
            },
        )
        assert ambiguous.status_code == 422
        enacted = client.post(
            f"/v1/scopes/{PROJECT_ALIAS}/roster",
            headers=headers(OWNER_TOKEN, key=str(uuid.uuid4())),
            json={
                "entries": [
                    {
                        "principal_id": OWNER_PRINCIPAL,
                        "role": "lead",
                        "responsibility": "w1-candidate-lead",
                    },
                    {"principal_id": WORKER_PRINCIPAL, "role": "member"},
                ]
            },
        )
        assert enacted.status_code == 201
        assert enacted.json()["data"]["roster_revision"] == prior_roster + 1
        roster = client.get(
            f"/v1/scopes/{PROJECT_ALIAS}/roster", headers=headers(WORKER_TOKEN)
        )
        assert roster.status_code == 200
        roster_data = roster.json()["data"]
        assert roster_data["scope_id"] == PROJECT_SCOPE
        assert roster_data["roster_revision"] == prior_roster + 1
        assert {"lead", "member"} <= {
            entry["role"] for entry in roster_data["entries"]
        }

        if prior_policy == 0:
            before_policy = create_content(client, WORKER_TOKEN, "write before policy")
            assert before_policy.status_code == 201

        policy = client.post(
            f"/v1/scopes/{PROJECT_ALIAS}/writer-policy",
            headers=headers(OWNER_TOKEN, key=str(uuid.uuid4())),
            json={"allowed_roles": ["lead"], "expected_revision": prior_policy},
        )
        assert policy.status_code == 201
        assert policy.json()["data"]["policy_revision"] == prior_policy + 1

        denied = create_content(
            client, WORKER_TOKEN, "member write under lead-only policy"
        )
        allowed = create_content(client, OWNER_TOKEN, "lead write under lead policy")
        stale = client.post(
            f"/v1/scopes/{PROJECT_ALIAS}/writer-policy",
            headers=headers(OWNER_TOKEN, key=str(uuid.uuid4())),
            json={"allowed_roles": ["member"], "expected_revision": prior_policy},
        )
        widened = client.post(
            f"/v1/scopes/{PROJECT_ALIAS}/writer-policy",
            headers=headers(OWNER_TOKEN, key=str(uuid.uuid4())),
            json={
                "allowed_roles": ["lead", "member"],
                "expected_revision": prior_policy + 1,
            },
        )
        restored = create_content(client, WORKER_TOKEN, "member write after widening")

    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "writer_policy_denied"
    assert allowed.status_code == 201
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "policy_revision_conflict"
    assert widened.status_code == 201
    assert restored.status_code == 201


# ---------------------------------------------------------------------------
# R01/R02: typed canonical content, revisions, provenance, lifecycle
# ---------------------------------------------------------------------------


def test_original_and_revision_readback_is_exact() -> None:
    original = (
        "Decision: keep `cortex/v2/migrations` forward-only.\n"
        "Path: /Users/dev/repo/cortex/v2\n"
        'Output: {"rows": 42, "ok": true} — ünïcødé ✓'
    )
    revised = f"{original}\nAmendment: reviewed by W1 candidate."
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        created = create_content(
            client,
            WORKER_TOKEN,
            original,
            content_class="decision",
            payload={"statement": original},
        )
        assert created.status_code == 201
        data = created.json()["data"]
        assert data["state"] == "committed"
        assert data["revision"] == 1
        content_id = data["content_id"]

        readback = client.get(
            f"/v1/content/{content_id}", headers=headers(WORKER_TOKEN, PROJECT_ALIAS)
        )
        assert readback.status_code == 200
        current = readback.json()["data"]
        assert current["body"] == original
        assert current["payload"] == {"statement": original}
        assert current["content_hash"] == data["content_hash"]
        assert current["status"] == "current"
        assert current["citation"]["revision"] == 1

        revision = client.post(
            f"/v1/content/{content_id}/revisions",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS, str(uuid.uuid4())),
            json={
                "payload": {"statement": revised},
                "body": revised,
                "expected_revision": 1,
            },
        )
        assert revision.status_code == 201
        assert revision.json()["data"]["revision"] == 2

        stale = client.post(
            f"/v1/content/{content_id}/revisions",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS, str(uuid.uuid4())),
            json={
                "payload": {"statement": "stale"},
                "body": "stale",
                "expected_revision": 1,
            },
        )
        historical = client.get(
            f"/v1/content/{content_id}?revision=1",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
        )
        latest = client.get(
            f"/v1/content/{content_id}", headers=headers(WORKER_TOKEN, PROJECT_ALIAS)
        )
        history = client.get(
            f"/v1/content/{content_id}?history=true",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
        )

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "revision_conflict"
    assert historical.status_code == 200
    assert historical.json()["data"]["body"] == original
    assert historical.json()["data"]["revision"] == 1
    assert latest.json()["data"]["body"] == revised
    assert latest.json()["data"]["revision"] == 2
    assert history.status_code == 200
    lineage = history.json()["data"]["revisions"]
    assert [entry["revision"] for entry in lineage] == [1, 2]
    assert lineage[0]["body"] == original
    assert lineage[1]["supersedes_revision"] == 1


CONTENT_CLASS_PAYLOADS: dict[str, dict] = {
    "decision": {"statement": "adopt forward-only migrations"},
    "lesson": {"lesson": "recheck ports immediately before bind"},
    "knowledge": {"content": "RLS is the second boundary"},
    "progress": {"summary": "W1 schema applied"},
    "diary": {"entry": "candidate day one"},
    "message": {"role": "user", "text": "run the suite"},
    "session": {"source": "w1-cli", "message_count": 3},
    "artifact": {
        "media_type": "text/plain",
        "bytes_sha256": "ab" * 32,
        "location": "candidate://w1/artifact-1",
    },
    "work_product": {"kind": "patch", "summary": "w1 diff", "references": []},
}


def test_every_content_class_is_typed_and_artifacts_are_pending_processing() -> None:
    with httpx.Client(base_url=API_URL, timeout=15) as client:
        receipts = {}
        for content_class, payload in CONTENT_CLASS_PAYLOADS.items():
            response = create_content(
                client,
                WORKER_TOKEN,
                f"typed {content_class} body",
                content_class=content_class,
                payload=payload,
            )
            assert response.status_code == 201, (content_class, response.json())
            receipts[content_class] = response.json()["data"]

        bad_message = create_content(
            client,
            WORKER_TOKEN,
            "mistyped message",
            content_class="message",
            payload={"role": "user"},
        )
        bad_artifact = create_content(
            client,
            WORKER_TOKEN,
            "mistyped artifact",
            content_class="artifact",
            payload={
                "media_type": "text/plain",
                "bytes_sha256": "nothex",
                "location": "x",
            },
        )

    assert receipts["artifact"]["state"] == "pending_processing"
    for content_class, receipt in receipts.items():
        if content_class != "artifact":
            assert receipt["state"] == "committed", content_class
        assert receipt["content_class"] == content_class
    assert bad_message.status_code == 422
    assert bad_message.json()["error"]["code"] == "invalid_content_payload"
    assert bad_artifact.status_code == 422


def test_invalidation_supersession_tombstone_and_undo() -> None:
    marker = f"lifecycle marker {uuid.uuid4().hex}"
    with httpx.Client(base_url=API_URL, timeout=15) as client:
        first = create_content(client, WORKER_TOKEN, f"{marker} original")
        content_id = first.json()["data"]["content_id"]
        successor = create_content(client, WORKER_TOKEN, f"{marker} successor")
        successor_id = successor.json()["data"]["content_id"]

        def search(include_historical: bool) -> httpx.Response:
            return client.post(
                "/v1/content/searches",
                headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
                json={
                    "query": marker,
                    "read_scopes": [PROJECT_ALIAS],
                    "include_historical": include_historical,
                },
            )

        def status_of(response: httpx.Response, wanted: str) -> str | None:
            for hit in response.json()["data"]["hits"]:
                if hit["content_id"] == wanted:
                    return hit["status"]
            return None

        assert status_of(search(False), content_id) == "current"

        invalidated = client.post(
            f"/v1/content/{content_id}:invalidate",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS, str(uuid.uuid4())),
            json={"reason": "superseded by review"},
        )
        assert invalidated.status_code == 200
        assert invalidated.json()["data"]["status"] == "invalidated"
        assert status_of(search(False), content_id) is None
        assert status_of(search(True), content_id) == "invalidated"

        inspected = client.get(
            f"/v1/content/{content_id}", headers=headers(WORKER_TOKEN, PROJECT_ALIAS)
        )
        assert inspected.json()["data"]["status"] == "invalidated"
        assert f"{marker} original" in inspected.json()["data"]["body"]

        restored = client.post(
            f"/v1/content/{content_id}:restore",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS, str(uuid.uuid4())),
            json={},
        )
        assert restored.status_code == 200
        assert status_of(search(False), content_id) == "current"

        superseded = client.post(
            f"/v1/content/{content_id}:supersede",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS, str(uuid.uuid4())),
            json={"successor_content_id": successor_id},
        )
        assert superseded.status_code == 200
        revise_superseded = client.post(
            f"/v1/content/{content_id}/revisions",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS, str(uuid.uuid4())),
            json={"payload": {"content": "x"}, "body": "x", "expected_revision": 1},
        )
        assert revise_superseded.status_code == 409
        assert revise_superseded.json()["error"]["code"] == "content_superseded"

        tombstoned = client.post(
            f"/v1/content/{successor_id}:tombstone",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS, str(uuid.uuid4())),
            json={"reason": "fixture cleanup"},
        )
        assert tombstoned.status_code == 200
        restore_tombstoned = client.post(
            f"/v1/content/{successor_id}:restore",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS, str(uuid.uuid4())),
            json={},
        )
        assert restore_tombstoned.status_code == 409
        assert restore_tombstoned.json()["error"]["code"] == "content_tombstoned"
        assert status_of(search(False), successor_id) is None

        history = client.get(
            f"/v1/content/{content_id}?history=true",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
        )

    assert history.status_code == 200
    history_data = history.json()["data"]
    assert history_data["status"] == "superseded"
    transitions = [
        (entry["prior_status"], entry["status"])
        for entry in history_data["status_history"]
    ]
    assert transitions == [
        ("current", "invalidated"),
        ("invalidated", "current"),
        ("current", "superseded"),
    ]
    superseded_entry = history_data["status_history"][-1]
    assert superseded_entry["superseded_by_content_id"] == successor_id
    assert [entry["revision"] for entry in history_data["revisions"]] == [1]


def test_ingest_dedupes_on_connector_source_key() -> None:
    source_key = f"session-{uuid.uuid4().hex}"
    body = f"ingested session content {source_key}"
    request = {
        "connector_namespace": CONNECTOR_NAMESPACE,
        "source_key": source_key,
        "content_class": "session",
        "payload": {"source": CONNECTOR_NAMESPACE, "message_count": 2},
        "body": body,
    }
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        first = client.post(
            "/v1/content/ingest",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
            json=request,
        )
        replay = client.post(
            "/v1/content/ingest",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
            json=request,
        )
        conflict = client.post(
            "/v1/content/ingest",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
            json={**request, "body": f"{body} changed"},
        )
        unknown = client.post(
            "/v1/content/ingest",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
            json={**request, "connector_namespace": "no-such-connector"},
        )
        cross_principal = client.post(
            "/v1/content/ingest",
            headers=headers(OWNER_TOKEN, PROJECT_ALIAS),
            json=request,
        )

    assert first.status_code == 201
    data = first.json()["data"]
    assert data["state"] == "committed"
    assert data["provenance"]["connector_namespace"] == CONNECTOR_NAMESPACE
    assert data["provenance"]["source_key"] == source_key
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json()["data"]["content_id"] == data["content_id"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_key_reused"
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "connector_not_found"
    assert cross_principal.status_code == 409
    assert cross_principal.json()["error"]["code"] == "source_key_conflict"


def test_content_idempotency_replays_receipt_and_conflicts_on_reuse() -> None:
    key = str(uuid.uuid4())
    body = f"idempotent content {key}"
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        created = create_content(client, WORKER_TOKEN, body, key=key)
        replayed = create_content(client, WORKER_TOKEN, body, key=key)
        collision = create_content(client, WORKER_TOKEN, f"{body} changed", key=key)

    assert created.status_code == 201
    assert replayed.status_code == 200
    assert replayed.headers["Idempotent-Replay"] == "true"
    assert replayed.json()["data"] == created.json()["data"]
    assert collision.status_code == 409
    assert collision.json()["error"]["code"] == "idempotency_key_reused"
    assert (
        db_value(
            "SELECT count(*) FROM cortex_core.content_revisions WHERE body_text = $1",
            body,
            principal=WORKER_PRINCIPAL,
            read_scopes=PROJECT_SCOPE,
        )
        == 1
    )


# ---------------------------------------------------------------------------
# Failure handling, policy recheck, RLS and no-null-to-global at the DB level
# ---------------------------------------------------------------------------


def test_outbox_failure_rolls_back_content_command() -> None:
    key = str(uuid.uuid4())
    body = f"rollback marker {key}"

    async def install_fault() -> None:
        connection = await asyncpg.connect(MIGRATOR_DATABASE_URL)
        try:
            await connection.execute(
                "CREATE OR REPLACE FUNCTION cortex_core.w1_reject_outbox() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'w1 injected outbox failure'; END $$"
            )
            await connection.execute(
                "CREATE TRIGGER w1_reject_content_outbox BEFORE INSERT ON "
                "cortex_core.content_outbox_events FOR EACH ROW EXECUTE FUNCTION "
                "cortex_core.w1_reject_outbox()"
            )
        finally:
            await connection.close()

    async def remove_fault() -> None:
        connection = await asyncpg.connect(MIGRATOR_DATABASE_URL)
        try:
            await connection.execute(
                "DROP TRIGGER IF EXISTS w1_reject_content_outbox "
                "ON cortex_core.content_outbox_events"
            )
            await connection.execute(
                "DROP FUNCTION IF EXISTS cortex_core.w1_reject_outbox()"
            )
        finally:
            await connection.close()

    items_before = db_value(
        "SELECT count(*) FROM cortex_core.content_items",
        principal=WORKER_PRINCIPAL,
        read_scopes=PROJECT_SCOPE,
    )
    asyncio.run(install_fault())
    try:
        with httpx.Client(base_url=API_URL, timeout=10) as client:
            response = create_content(client, WORKER_TOKEN, body, key=key)
    finally:
        asyncio.run(remove_fault())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert response.json()["error"]["retryable"] is True
    assert (
        db_value(
            "SELECT count(*) FROM cortex_core.content_revisions WHERE body_text = $1",
            body,
            principal=WORKER_PRINCIPAL,
            read_scopes=PROJECT_SCOPE,
        )
        == 0
    )
    assert (
        db_value(
            "SELECT count(*) FROM cortex_core.command_receipts "
            "WHERE idempotency_key = $1",
            key,
            principal=WORKER_PRINCIPAL,
            read_scopes=PROJECT_SCOPE,
        )
        == 0
    )
    assert (
        db_value(
            "SELECT count(*) FROM cortex_core.content_items",
            principal=WORKER_PRINCIPAL,
            read_scopes=PROJECT_SCOPE,
        )
        == items_before
    )


def test_stale_or_unset_policy_revision_is_rejected_at_insert() -> None:
    content_id = str(uuid.uuid4())

    async def attempt(policy_revision: str | None) -> str:
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('cortex.principal_id', $1, true)",
                    WORKER_PRINCIPAL,
                )
                await connection.execute(
                    "SELECT set_config('cortex.read_scope_ids', $1, true)", PROJECT_SCOPE
                )
                await connection.execute(
                    "SELECT set_config('cortex.write_scope_id', $1, true)", PROJECT_SCOPE
                )
                if policy_revision is not None:
                    await connection.execute(
                        "SELECT set_config('cortex.policy_revision', $1, true)",
                        policy_revision,
                    )
                await connection.execute(
                    """
                    INSERT INTO cortex_core.content_items
                        (scope_id, content_id, content_class, created_by_principal)
                    VALUES ($1::uuid, $2::uuid, 'knowledge', $3::uuid)
                    """,
                    PROJECT_SCOPE,
                    content_id,
                    WORKER_PRINCIPAL,
                )
                await connection.execute(
                    """
                    INSERT INTO cortex_core.content_revisions
                        (scope_id, content_id, revision, payload, body_text,
                         content_hash, author_principal_id)
                    VALUES ($1::uuid, $2::uuid, 1, '{"content": "x"}'::jsonb,
                            'stale policy write', $3, $4::uuid)
                    """,
                    PROJECT_SCOPE,
                    content_id,
                    b"\x00" * 32,
                    WORKER_PRINCIPAL,
                )
                return "inserted"
        except asyncpg.PostgresError as exc:
            return exc.sqlstate or "unknown"
        finally:
            await connection.close()

    assert asyncio.run(attempt("999")) == "55000"
    assert asyncio.run(attempt(None)) == "55000"
    assert (
        db_value(
            "SELECT count(*) FROM cortex_core.content_items WHERE content_id = $1::uuid",
            content_id,
            principal=WORKER_PRINCIPAL,
            read_scopes=PROJECT_SCOPE,
        )
        == 0
    )


def test_content_rls_hides_cross_scope_and_unset_context() -> None:
    marker = f"rls isolation marker {uuid.uuid4().hex}"
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        created = create_content(client, WORKER_TOKEN, marker)
    assert created.status_code == 201
    content_id = created.json()["data"]["content_id"]

    query = "SELECT count(*) FROM cortex_core.content_items WHERE content_id = $1::uuid"
    assert (
        db_value(query, content_id, principal=WORKER_PRINCIPAL, read_scopes=PROJECT_SCOPE)
        == 1
    )
    assert (
        db_value(query, content_id, principal=WORKER_PRINCIPAL, read_scopes=LOCAL_SCOPE)
        == 0
    )
    assert db_value("SELECT count(*) FROM cortex_core.content_items") == 0
    assert db_value("SELECT count(*) FROM cortex_core.command_receipts") == 0


def test_shared_scope_content_is_readable_not_writable_by_worker() -> None:
    marker = f"shared marker {uuid.uuid4().hex}"
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        owner_created = create_content(
            client, OWNER_TOKEN, marker, scope=SHARED_ALIAS
        )
        assert owner_created.status_code == 201
        content_id = owner_created.json()["data"]["content_id"]

        hidden = client.get(
            f"/v1/content/{content_id}", headers=headers(WORKER_TOKEN, PROJECT_ALIAS)
        )
        visible = client.get(
            f"/v1/content/{content_id}",
            headers=headers(
                WORKER_TOKEN,
                PROJECT_ALIAS,
                read_scopes=f"{PROJECT_ALIAS},{SHARED_ALIAS}",
            ),
        )
        worker_write = create_content(
            client, WORKER_TOKEN, "worker shared write", scope=SHARED_ALIAS
        )

    assert hidden.status_code == 404
    assert visible.status_code == 200
    assert visible.json()["data"]["body"] == marker
    assert worker_write.status_code == 403
    assert worker_write.json()["error"]["code"] == "scope_write_denied"


def test_local_scope_content_is_excluded_from_project_search() -> None:
    marker = f"local marker {uuid.uuid4().hex}"
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        created = create_content(client, WORKER_TOKEN, marker, scope=LOCAL_ALIAS)
        assert created.status_code == 201
        project_only = client.post(
            "/v1/content/searches",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
            json={"query": marker, "read_scopes": [PROJECT_ALIAS]},
        )
        with_local = client.post(
            "/v1/content/searches",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
            json={"query": marker, "read_scopes": [PROJECT_ALIAS, LOCAL_ALIAS]},
        )

    assert all(
        hit["body"] != marker for hit in project_only.json()["data"]["hits"]
    )
    assert any(hit["body"] == marker for hit in with_local.json()["data"]["hits"])


def test_registry_secrecy_blocks_direct_table_access() -> None:
    async def run() -> dict[str, object]:
        connection = await asyncpg.connect(DATABASE_URL)
        results: dict[str, object] = {}
        try:
            for name, query in (
                ("actors", "SELECT count(*) FROM cortex_auth.actors"),
                ("memberships", "SELECT count(*) FROM cortex_auth.memberships"),
                (
                    "roster_revisions",
                    "SELECT count(*) FROM cortex_auth.roster_revisions",
                ),
                (
                    "writer_policies",
                    "SELECT count(*) FROM cortex_auth.writer_policies",
                ),
            ):
                results[name] = int(await connection.fetchval(query))
            for name, query in (
                (
                    "installations",
                    "SELECT count(*) FROM cortex_auth.installations",
                ),
                (
                    "installation_owners",
                    "SELECT count(*) FROM cortex_auth.installation_owners",
                ),
                (
                    "installation_recovery",
                    "SELECT count(*) FROM cortex_auth.installation_recovery",
                ),
                (
                    "privileged_actions",
                    "SELECT count(*) FROM cortex_auth.privileged_actions",
                ),
                ("credentials", "SELECT count(*) FROM cortex_auth.credentials"),
                ("principals", "SELECT count(*) FROM cortex_auth.principals"),
                (
                    "conversion_quarantine",
                    "SELECT count(*) FROM cortex_core.conversion_quarantine",
                ),
            ):
                try:
                    await connection.fetchval(query)
                    results[name] = "readable"
                except asyncpg.InsufficientPrivilegeError:
                    results[name] = "denied"
        finally:
            await connection.close()
        return results

    results = asyncio.run(run())
    assert results["actors"] == 0
    assert results["memberships"] == 0
    assert results["roster_revisions"] == 0
    assert results["writer_policies"] == 0
    for name in (
        "installations",
        "installation_owners",
        "installation_recovery",
        "privileged_actions",
        "credentials",
        "principals",
        "conversion_quarantine",
    ):
        assert results[name] == "denied", name


def test_content_rows_cannot_enter_with_null_scope() -> None:
    async def run() -> str:
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('cortex.principal_id', $1, true)",
                    WORKER_PRINCIPAL,
                )
                await connection.execute(
                    "SELECT set_config('cortex.write_scope_id', $1, true)", PROJECT_SCOPE
                )
                await connection.execute(
                    """
                    INSERT INTO cortex_core.content_items
                        (scope_id, content_id, content_class, created_by_principal)
                    VALUES (NULL, $1::uuid, 'knowledge', $2::uuid)
                    """,
                    str(uuid.uuid4()),
                    WORKER_PRINCIPAL,
                )
                return "inserted"
        except asyncpg.NotNullViolationError:
            return "not_null_violation"
        except asyncpg.PostgresError as exc:
            return exc.sqlstate or "unknown"
        finally:
            await connection.close()

    assert asyncio.run(run()) in {"not_null_violation", "42501"}


# ---------------------------------------------------------------------------
# Owner recovery requires separate written T19 approval.
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="T19: owner recovery requires Kai written approval")
def test_owner_recovery_reenrolls_owner_and_consumes_recovery_token() -> None:
    recovery_token = (
        SECRETS / PROFILE.secret_name("recovery-token")
    ).read_text().strip()
    pre_recovery_generation = migrator_value(
        "SELECT generation FROM cortex_auth.installation_recovery LIMIT 1"
    )
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        recovered = client.post(
            "/v1/auth/owner:recover",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"recovery_token": recovery_token},
        )
        assert recovered.status_code == 201, recovered.json().get("error")
        data = recovered.json()["data"]
        new_owner_token = data["owner_token"]
        new_recovery_token = data["recovery_token"]
        assert data["state"] == "committed"
        assert data["principal_id"] == OWNER_PRINCIPAL
        assert data["generation"] >= 2
        assert data["recovery_generation"] == pre_recovery_generation + 1

        replayed = client.post(
            "/v1/auth/owner:recover",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"recovery_token": recovery_token},
        )
        old_owner = client.get("/v1/auth/principal", headers=headers(OWNER_TOKEN))
        new_owner = client.get("/v1/auth/principal", headers=headers(new_owner_token))
        forged = client.post(
            "/v1/auth/owner:recover",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"recovery_token": WORKER_TOKEN},
        )
        second_recovery = client.post(
            "/v1/auth/owner:recover",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"recovery_token": new_recovery_token},
        )

    assert replayed.status_code == 401
    assert replayed.json()["error"]["code"] == "recovery_credential_invalid"
    assert old_owner.status_code == 401
    assert new_owner.status_code == 200
    assert forged.status_code == 401
    assert second_recovery.status_code == 201
