"""DB-backed integration suite for the Context & worker interface module.

Runs against the disposable integrated candidate, including shared-DB reruns.

Fixture needs (candidate secret mounts plus migrations through 0009):
  * the ``cortex_kai_test`` instance profile with Interface and ingest mounted;
  * fixture fields ``instance_id``, ``installation_id``,
    ``owner_principal_id``, ``worker_principal_id``;
  * secrets: ``owner-token``, ``recovery-token``, ``worker-token``,
    ``token-pepper``, ``database-url-migrator``, and ``fixture``
    under ``CORTEX_V2_SANDBOX_SECRETS_DIR``;
  * env ``CORTEX_V2_TEST_API_URL=http://api:8601`` in the candidate network;
  * API-service env ``CORTEX_V2_MOUNTED_MODULES`` naming every module the
    composition root actually mounted (the kai compose sets
    ``interface,ingest,coordination,processing,retrieval,feed,
    verification,ops``; this suite additionally requires
    ``identity_memory`` because ``create_app`` mounts the W1 routes -
    nothing is mounted by default and discovery reports registered-but-
    unmounted modules as unavailable with a reason);
  * the same env may be set in the test-service process for suites that
    consult the local registry (e.g. retrieval's); this suite itself only
    trusts server responses - the bind-mounted test tree may differ from
    the image the API was built from, so digests are compared against the
    server's own discovery payload, never a client-side rebuild;
  * the seeded worker principal remains active and writable; this suite
    provisions a separate run-scoped project with worker and owner grants,
    so append-only context rules from older runs cannot skew its budget.

The suite registers a run-scoped connector namespace via an owner bearer
credential. If the one-shot owner/recovery seed credentials are spent,
test-only candidate migrator break-glass calls the existing recovery SQL
function and verifies the resulting bearer over HTTP.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
import warnings
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
import pytest

from cortex_v2.config import EXPECTED_DATABASE, KAI_TEST_INSTANCE, active_profile
from cortex_v2.interface.context import CAPABILITY_OPERATIONS
from cortex_v2.store import token_digest

API_URL = os.environ.get("CORTEX_V2_TEST_API_URL")
SECRETS_DIR = os.environ.get("CORTEX_V2_SANDBOX_SECRETS_DIR")

if not API_URL or not SECRETS_DIR:
    pytest.skip(
        "interface integration suite requires CORTEX_V2_TEST_API_URL and "
        "CORTEX_V2_SANDBOX_SECRETS_DIR",
        allow_module_level=True,
    )

SECRETS = Path(SECRETS_DIR)
FIXTURE = json.loads(_read := (SECRETS / "cortex-v2-fixture").read_text()) \
    if (SECRETS / "cortex-v2-fixture").exists() else None
if FIXTURE is None:
    for candidate in sorted(SECRETS.glob("*fixture*")):
        FIXTURE = json.loads(candidate.read_text())
        break
if FIXTURE is None:
    pytest.skip("no candidate fixture secret found", allow_module_level=True)


def _secret(pattern: str) -> str:
    match = next(iter(sorted(SECRETS.glob(pattern))), None)
    if match is None:
        pytest.skip(f"missing secret {pattern}", allow_module_level=True)
    return match.read_text().strip()


class _RedactedToken(str):
    def __repr__(self) -> str:
        return "[REDACTED]"


OWNER_TOKEN = _RedactedToken(_secret("*owner-token*"))
RECOVERY_TOKEN = _RedactedToken(_secret("*recovery-token*"))
WORKER_TOKEN = _RedactedToken(_secret("*worker-token*"))
MIGRATOR_DATABASE_URL = _secret("*database-url-migrator*")
# A fresh project prevents append-only mandatory rules and persona revisions
# from preceding test runs from changing the budget and selection under test.
RUN = uuid.uuid4().hex[:12]
PROJECT_ALIAS = f"interface-project-{RUN}"
CONNECTOR_NAMESPACE = f"w2-interface-{RUN}"
MIRROR_LABEL = f"integration-mirror-{RUN}"


class _RedactedAuthorization(str):
    def __repr__(self) -> str:
        return "'Bearer [REDACTED]'"


def headers(token: str, scope: str | None = None, key: str | None = None,
            read_scopes: str | None = None) -> dict[str, str]:
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


def _is_candidate_database() -> bool:
    try:
        profile = active_profile()
        parsed = urlsplit(MIGRATOR_DATABASE_URL)
        return (
            profile.instance_id == KAI_TEST_INSTANCE
            and FIXTURE["instance_id"] == KAI_TEST_INSTANCE
            and API_URL == "http://api:8601"
            and parsed.scheme == "postgresql"
            and unquote(parsed.username or "") == "cortex_v2_migrator"
            and parsed.hostname == profile.database_host == "db"
            and parsed.port == 5432
            and parsed.path == f"/{EXPECTED_DATABASE}"
            and not parsed.query
            and not parsed.fragment
        )
    except (KeyError, ValueError):
        return False


def _assert_recovery_identity(
    db_installation: uuid.UUID | None,
    db_owner: uuid.UUID | None,
    db_worker: uuid.UUID | None,
    api_installation: uuid.UUID,
    api_worker: uuid.UUID,
) -> None:
    if (
        db_installation != uuid.UUID(FIXTURE["installation_id"])
        or db_owner != uuid.UUID(FIXTURE["owner_principal_id"])
        or db_worker != uuid.UUID(FIXTURE["worker_principal_id"])
        or api_installation != db_installation
        or api_worker != db_worker
    ):
        raise RuntimeError("candidate recovery identity mismatch")


async def _attest_candidate_installation(connection) -> None:
    if not _is_candidate_database():
        raise RuntimeError("candidate recovery identity mismatch")
    async with httpx.AsyncClient(base_url=API_URL, timeout=10) as client:
        authenticated = await client.get(
            "/v1/auth/principal", headers=headers(WORKER_TOKEN)
        )
    if authenticated.status_code != 200:
        raise RuntimeError("candidate recovery identity mismatch")
    api_identity = authenticated.json()["data"]
    db_identity = await connection.fetchrow(
        """
        SELECT i.installation_id,
               o.principal_id AS owner_principal_id,
               w.principal_id AS worker_principal_id
          FROM cortex_auth.installations AS i
          JOIN cortex_auth.installation_owners AS o
            ON o.installation_id = i.installation_id
           AND o.revoked_at IS NULL
          JOIN cortex_auth.principals AS w
            ON w.installation_id = i.installation_id
           AND w.status = 'active'
         WHERE i.installation_id = $1
           AND o.principal_id = $2
           AND w.principal_id = $3
        """,
        uuid.UUID(FIXTURE["installation_id"]),
        uuid.UUID(FIXTURE["owner_principal_id"]),
        uuid.UUID(FIXTURE["worker_principal_id"]),
    )
    _assert_recovery_identity(
        db_identity["installation_id"] if db_identity else None,
        db_identity["owner_principal_id"] if db_identity else None,
        db_identity["worker_principal_id"] if db_identity else None,
        uuid.UUID(api_identity["installation_id"]),
        uuid.UUID(api_identity["principal_id"]),
    )


async def _recover_candidate_owner_through_sql() -> _RedactedToken:
    import asyncpg

    if not _is_candidate_database():
        raise RuntimeError("candidate recovery identity mismatch")
    connection = await asyncpg.connect(MIGRATOR_DATABASE_URL, timeout=20)
    try:
        async with connection.transaction():
            await _attest_candidate_installation(connection)
            owner = _RedactedToken(secrets.token_urlsafe(32))
            recovery = _RedactedToken(secrets.token_urlsafe(32))
            row = await connection.fetchrow(
                """
                SELECT installation_id, principal_id
                  FROM cortex_auth.recover_owner(
                    (SELECT recovery_token_hash
                       FROM cortex_auth.installation_recovery
                      WHERE installation_id = $1),
                    $2, $3, NULL::timestamptz)
                """,
                uuid.UUID(FIXTURE["installation_id"]),
                token_digest(owner, bytes.fromhex(_secret("*token-pepper*"))),
                token_digest(recovery, bytes.fromhex(_secret("*token-pepper*"))),
            )
            if (
                row["installation_id"] != uuid.UUID(FIXTURE["installation_id"])
                or row["principal_id"] != uuid.UUID(FIXTURE["owner_principal_id"])
            ):
                raise RuntimeError("candidate recovery identity mismatch")
    finally:
        await connection.close()
    return owner


def _recover_candidate_owner_break_glass() -> _RedactedToken:
    if not _is_candidate_database():
        raise RuntimeError("candidate-only owner recovery refused: profile mismatch")
    try:
        return asyncio.run(_recover_candidate_owner_through_sql())
    except Exception:
        raise RuntimeError("candidate-only owner recovery failed") from None


@pytest.fixture(scope="module")
def run_scope():
    if not _is_candidate_database():
        raise RuntimeError("interface test scope requires the integrated candidate")

    async def seed():
        import asyncpg

        connection = await asyncpg.connect(MIGRATOR_DATABASE_URL, timeout=20)
        try:
            async with connection.transaction():
                await _attest_candidate_installation(connection)
                scope_id = uuid.uuid4()
                await connection.execute(
                    "INSERT INTO cortex_core.scopes(scope_id, scope_kind, display_name) "
                    "VALUES ($1, 'project', 'Interface integration run')",
                    scope_id,
                )
                await connection.execute(
                    "INSERT INTO cortex_core.scope_aliases(alias, scope_id, is_primary) "
                    "VALUES ($1, $2, true)",
                    PROJECT_ALIAS, scope_id,
                )
                for principal, can_publish in (
                    (FIXTURE["owner_principal_id"], True),
                    (FIXTURE["worker_principal_id"], False),
                ):
                    await connection.execute(
                        "INSERT INTO cortex_auth.scope_grants("
                        "principal_id, scope_id, can_read, can_write, can_publish) "
                        "VALUES ($1, $2, true, true, $3)",
                        uuid.UUID(principal), scope_id, can_publish,
                    )
        finally:
            await connection.close()

    asyncio.run(seed())


@pytest.fixture(scope="module")
def owner_token(api: httpx.Client) -> _RedactedToken:
    current = api.get("/v1/auth/principal", headers=headers(OWNER_TOKEN))
    if current.status_code == 200:
        return OWNER_TOKEN
    if current.status_code != 401:
        raise RuntimeError("candidate owner liveness check failed")

    recovered = api.post(
        "/v1/auth/owner:recover",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"recovery_token": RECOVERY_TOKEN, "expires_in_seconds": 3600},
    )
    if recovered.status_code == 201:
        return _RedactedToken(recovered.json()["data"]["owner_token"])
    if (
        recovered.status_code != 401
        or recovered.json().get("error", {}).get("code")
        != "recovery_credential_invalid"
    ):
        raise RuntimeError("candidate owner recovery returned an unexpected response")

    issued = _recover_candidate_owner_break_glass()
    verified = api.get("/v1/auth/principal", headers=headers(issued))
    if verified.status_code != 200:
        raise RuntimeError("candidate owner recovery did not authenticate")
    warnings.warn(
        "owner recovered via candidate migrator break-glass",
        RuntimeWarning,
        stacklevel=2,
    )
    return issued


@pytest.fixture(scope="module")
def api(run_scope) -> httpx.Client:
    with httpx.Client(base_url=API_URL, timeout=20) as client:
        yield client


@pytest.fixture(scope="module")
def connector_registered(api: httpx.Client, owner_token: _RedactedToken) -> None:
    response = api.post(
        "/v1/connectors",
        headers=headers(owner_token, key=f"connector-{uuid.uuid4()}"),
        json={
            "namespace": CONNECTOR_NAMESPACE,
            "connector_kind": "session_ingest",
        },
    )
    assert response.status_code == 201, response.text


@pytest.fixture(scope="module")
def db():
    """Run migrator reads in one event loop per query, never reuse a closed loop."""
    async def fetch(query: str, *args: object):
        import asyncpg

        connection = await asyncpg.connect(MIGRATOR_DATABASE_URL, timeout=20)
        try:
            return await connection.fetch(query, *args)
        finally:
            await connection.close()

    return fetch


# ---------------------------------------------------------------------------
# Discovery and usage cards (R27, F13)
# ---------------------------------------------------------------------------


def test_discovery_lists_modules_with_unavailable_reasons(api):
    response = api.get(
        "/v1/capabilities", headers=headers(WORKER_TOKEN, PROJECT_ALIAS)
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    modules = {module["module"]: module for module in data["modules"]}
    # The API composition root mounts the W1 routes in create_app and the
    # interface/ingest routes generically; it must therefore mark
    # identity_memory, interface and ingest as mounted (env
    # CORTEX_V2_MOUNTED_MODULES or registry.mark_modules_mounted). Nothing
    # is mounted by default, so a missing mark fails here on purpose.
    assert modules["identity_memory"]["state"] == "ready", (
        "the composition root did not mark identity_memory as mounted"
    )
    assert modules["interface"]["state"] == "ready"
    assert modules["ingest"]["state"] == "ready"
    for module in data["modules"]:
        if module["state"] == "unavailable":
            assert module["reason"]
    ids = {item["operation_id"]: item for item in data["operations"]}
    assert "context.prepare" in ids
    for item in ids.values():
        assert item["state"] != "ready" or not item.get("reason")


def test_discovery_reports_worker_roles(api):
    response = api.get(
        "/v1/capabilities", headers=headers(WORKER_TOKEN, PROJECT_ALIAS)
    )
    roles = response.json()["data"]["worker_roles"]
    assert {"doc", "embed", "graph"} <= set(roles)
    for role in ("doc", "embed", "graph"):
        assert roles[role]["state"] in (
            "ready", "blocked", "unavailable", "not_configured"
        )
        if roles[role]["state"] == "unavailable":
            assert roles[role]["reason"]
    # media roles appear only when the server activated them; whenever
    # present they must carry an honest state
    for name in ("audio", "video", "vision"):
        if name in roles:
            assert roles[name]["state"]


def test_discovery_filters_owner_operations_for_non_owner(api, owner_token):
    worker = api.get(
        "/v1/capabilities", headers=headers(WORKER_TOKEN, PROJECT_ALIAS)
    ).json()["data"]
    worker_ids = {item["operation_id"] for item in worker["operations"]}
    assert "auth.enroll_principal" not in worker_ids
    response = api.get(
        "/v1/capabilities", headers=headers(owner_token, PROJECT_ALIAS)
    )
    assert response.status_code == 200, response.text
    owner_ids = {item["operation_id"] for item in response.json()["data"]["operations"]}
    assert "auth.enroll_principal" in owner_ids


def test_usage_card_is_complete(api):
    response = api.get(
        "/v1/capabilities/context.prepare",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert response.status_code == 200, response.text
    card = response.json()["data"]
    for field in ("purpose", "use_when", "avoid_when", "cost_class",
                  "freshness", "evidence_contract", "failure_codes",
                  "recovery_actions", "examples", "counterexamples"):
        assert card[field], field


def test_card_for_unknown_operation_is_404(api):
    response = api.get(
        "/v1/capabilities/nope.missing",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "operation_not_found"


# ---------------------------------------------------------------------------
# Context preparation (R05, F05)
# ---------------------------------------------------------------------------


PERSONA_BODY = "You are a careful Cortex v2 integration worker.\n"
MANDATORY_RULE = "Never force-push a shared branch.\n"
OPTIONAL_RULE = "Prefer small commits.\n"
SKILL_BODY = "# Debugging\nReproduce first, then bisect.\n"
# A large mandatory rule makes mandatory_bytes exceed the 2048-byte model
# minimum deterministically, so the budget-error path is testable.
LARGE_MANDATORY_RULE = "Mandatory acceptance material. " * 120 + "\n"
RULE_SLUG = f"no-force-push-{RUN}"
OPTIONAL_RULE_SLUG = f"small-commits-{RUN}"
LARGE_RULE_SLUG = f"acceptance-bulk-{RUN}"
SKILL_SLUG = f"debugging-{RUN}"


def _enact_rule(api, key, slug, obligation, body):
    response = api.post(
        "/v1/context/rules",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, key),
        json={
            "rule_id": None,
            "slug": slug,
            "obligation": obligation,
            "body": body,
        },
    )
    assert response.status_code == 201, response.text
    return response


@pytest.fixture(scope="module")
def context_material(api) -> dict[str, str]:
    key = uuid.uuid4().hex
    persona = api.post(
        "/v1/context/personas",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, key + "-p"),
        json={
            "persona_id": None,
            "template_version": "persona.v1",
            "body": PERSONA_BODY,
            "payload": {"acceptance_criteria": ["tests pass"]},
        },
    )
    assert persona.status_code == 201, persona.text
    _enact_rule(api, key + "-rm", RULE_SLUG, "mandatory", MANDATORY_RULE)
    _enact_rule(api, key + "-rl", LARGE_RULE_SLUG, "mandatory",
                LARGE_MANDATORY_RULE)
    _enact_rule(api, key + "-ro", OPTIONAL_RULE_SLUG, "optional",
                OPTIONAL_RULE)
    skill = api.post(
        "/v1/context/skills",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, key + "-s"),
        json={
            "skill_id": None,
            "slug": SKILL_SLUG,
            "when_to_use": "Use when a test fails unexpectedly.",
            "body": SKILL_BODY,
        },
    )
    assert skill.status_code == 201, skill.text
    skill_id = skill.json()["data"]["skill_id"]
    # Re-runnable rebinding: read the current optimistic-lock revision
    # through skill.bindings instead of assuming a fresh scope.
    current = api.get(
        "/v1/context/skill-bindings",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert current.status_code == 200, current.text
    expected_revision = current.json()["data"]["revision"]
    binding = api.post(
        "/v1/context/skill-bindings",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, key + "-b"),
        json={
            "bindings": [
                {"skill_id": skill_id, "revision": None, "precedence": 10}
            ],
            "expected_revision": expected_revision,
        },
    )
    assert binding.status_code in (201, 200), binding.text
    return {
        "persona_id": persona.json()["data"]["persona_id"],
        "skill_id": skill_id,
        "binding_revision": binding.json()["data"]["binding_revision"],
    }


def prepare(api, *, budget: int, key: str | None = None, intent="code_change"):
    return api.post(
        "/v1/context/prepare",
        headers=headers(
            WORKER_TOKEN, PROJECT_ALIAS, key or uuid.uuid4().hex
        ),
        json={
            "intent": intent,
            "budget_bytes": budget,
            "task_text": "handoff claim semantics",
            "repository_ref": "helix",
            "change_ref": "diff-1",
        },
    )


def test_prepare_returns_sections_with_provenance(api, context_material):
    response = prepare(api, budget=65_536)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    context = data["context"]
    assert context["identity"]["scope_alias"] == PROJECT_ALIAS
    assert context["policy"]["revision"] >= 0
    assert any(
        rule["slug"] == RULE_SLUG and rule["revision"] == 1
        for rule in context["mandatory_rules"]["items"]
    )
    assert context["persona"]["body"] == PERSONA_BODY
    assert context["persona"]["revision"] == 1
    skills = context["skills"]["items"]
    assert skills[0]["slug"] == SKILL_SLUG
    assert skills[0]["fetch"]["operation_id"] == "skill.get"
    provenance = data["provenance"]
    # The provenance digest must equal the SERVER's live registry digest
    # (the bind-mounted test tree may legitimately differ from the image
    # the API process was built from, so never compare against a
    # client-side rebuild).
    discovery = api.get(
        "/v1/capabilities", headers=headers(WORKER_TOKEN, PROJECT_ALIAS)
    )
    assert discovery.status_code == 200, discovery.text
    assert provenance["registry_digest"] == (
        discovery.json()["data"]["registry_digest"]
    )
    assert provenance["persona"]["persona_id"] == context_material["persona_id"]
    recommended = [
        item["operation_id"]
        for item in context["recommendations"]["items"]
    ]
    assert "code.assess-change" in recommended
    # Missing-prerequisite honesty is registry-driven: the capability must be
    # listed missing exactly when the server's discovery shows no ready
    # operation serving it.
    served = {
        item["operation_id"]: item
        for item in discovery.json()["data"]["operations"]
    }
    capability_served = any(
        served.get(candidate, {}).get("state") == "ready"
        for candidate in CAPABILITY_OPERATIONS["code.assess-change"]
    )
    missing = {
        item["prerequisite"]
        for item in context["missing_prerequisites"]["items"]
    }
    if capability_served:
        assert "code.assess-change" not in missing
    else:
        assert "code.assess-change" in missing
        entry = next(
            item for item in context["missing_prerequisites"]["items"]
            if item["prerequisite"] == "code.assess-change"
        )
        assert entry["reason"]
        assert entry["recovery"]
    assert data["next_work"]


def test_prepare_budget_error_is_explicit(api, context_material):
    # The fixture's large mandatory rule guarantees mandatory_bytes > 2048,
    # so the minimum budget must fail with the explicit typed error.
    response = prepare(api, budget=2048)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "budget_exhausted"

    # A budget that fits the mandatory material exactly must succeed with
    # explicit truncation records for the optional sections.
    sized = prepare(api, budget=65_536)
    assert sized.status_code == 201, sized.text
    mandatory_bytes = sized.json()["data"]["budget"]["mandatory_bytes"]
    assert mandatory_bytes > 2048
    tight = prepare(api, budget=mandatory_bytes + 64)
    assert tight.status_code == 201, tight.text
    budget = tight.json()["data"]["budget"]
    assert budget["budget_bytes"] == mandatory_bytes + 64
    assert budget["composed_bytes"] <= budget["budget_bytes"]
    assert budget["truncations"]
    tight_context = tight.json()["data"]["context"]
    wire_context_bytes = len(json.dumps(
        tight_context, ensure_ascii=False, allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8"))
    assert wire_context_bytes == budget["composed_bytes"]
    assert wire_context_bytes <= budget["budget_bytes"]
    assert any(
        item["section"] == "persona"
        and item["kept"] == 0
        and item["dropped"] == 1
        for item in budget["truncations"]
    )
    # mandatory material survives the tight budget untouched
    assert any(
        rule["slug"] == RULE_SLUG
        for rule in tight_context["mandatory_rules"]["items"]
    )


def test_prepare_replay_is_identical(api, context_material):
    key = uuid.uuid4().hex
    first = prepare(api, budget=65_536, key=key)
    second = prepare(api, budget=65_536, key=key)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers.get("Idempotent-Replay") == "true"
    assert first.json()["data"] == second.json()["data"]


def test_prepare_rejects_reused_key_with_different_body(api, context_material):
    key = uuid.uuid4().hex
    first = prepare(api, budget=65_536, key=key)
    assert first.status_code == 201
    second = api.post(
        "/v1/context/prepare",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, key),
        json={"intent": "prose_edit", "budget_bytes": 65_536},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_key_reused"


def test_skill_get_on_demand(api, context_material):
    response = api.get(
        f"/v1/context/skills/{context_material['skill_id']}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["body"] == SKILL_BODY
    assert data["slug"] == SKILL_SLUG


def test_binding_expected_revision_conflict(api, context_material):
    stale_revision = context_material["binding_revision"] - 1
    response = api.post(
        "/v1/context/skill-bindings",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "bindings": [
                {"skill_id": context_material["skill_id"],
                 "revision": None, "precedence": 5}
            ],
            "expected_revision": stale_revision,
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "binding_revision_conflict"


def test_skill_bindings_read_reports_current_revision(api, context_material):
    response = api.get(
        "/v1/context/skill-bindings",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["revision"] >= context_material["binding_revision"]
    slugs = {binding["slug"] for binding in data["bindings"]}
    assert SKILL_SLUG in slugs


# ---------------------------------------------------------------------------
# Harness mirror (R06)
# ---------------------------------------------------------------------------


def test_harness_preview_is_deterministic(api, context_material):
    payload = {"mirror_label": MIRROR_LABEL, "pins": {}}
    first = api.post(
        "/v1/harness/previews",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
        json=payload,
    )
    second = api.post(
        "/v1/harness/previews",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
        json=payload,
    )
    assert first.status_code == 200, first.text
    assert first.json()["data"]["manifest_sha256"] == (
        second.json()["data"]["manifest_sha256"]
    )


def test_harness_apply_drift_rollback_cycle(api, context_material, tmp_path):
    from cortex_v2.clients.config import ClientProfile
    from cortex_v2.clients.client import CortexClient
    from cortex_v2.clients import harness_sync

    profile = ClientProfile(
        base_url=API_URL,
        token=WORKER_TOKEN,
        default_scope=PROJECT_ALIAS,
        default_read_scopes=(),
        installation_label=None,
        principal_label=None,
        source="integration-test",
    )
    client = CortexClient(profile)
    root = tmp_path / "mirror"
    root.mkdir()

    applied = harness_sync.sync_mirror(
        client, root, MIRROR_LABEL, idempotency_key=uuid.uuid4().hex
    )
    assert applied["generation"] == 1
    assert (root / "CONTEXT_MANIFEST.json").exists()
    assert (root / "persona.md").read_text() == PERSONA_BODY
    assert (root / "rules" / "mandatory" / f"{RULE_SLUG}.md").exists()
    assert (root / "rules" / "mandatory" / f"{LARGE_RULE_SLUG}.md").exists()
    # on-demand skills: an index of references, never skill bodies
    assert (root / "skills" / "INDEX.json").exists()
    assert not list((root / "skills").glob("*.md"))

    # hand edit must be detected and block the next apply
    (root / "persona.md").write_text("hand edited\n")
    (root / "extra.txt").write_text("untracked\n")
    drift = harness_sync.check_drift(client, root, MIRROR_LABEL)
    assert drift["status"] == "drifted"
    states = {item["path"]: item["state"] for item in drift["files"]}
    assert states["persona.md"] == "hand_edited"
    assert states["extra.txt"] == "untracked"

    from cortex_v2.clients.errors import CortexApiError

    with pytest.raises(CortexApiError) as excinfo:
        harness_sync.sync_mirror(
            client, root, MIRROR_LABEL,
            idempotency_key=uuid.uuid4().hex,
        )
    assert excinfo.value.code == "harness_drift_detected"

    # rollback restores generated bytes and removes newly introduced files
    rolled = harness_sync.rollback_mirror(
        client, root, MIRROR_LABEL, idempotency_key=uuid.uuid4().hex
    )
    assert rolled["generation"] == 2
    assert (root / "persona.md").read_text() == PERSONA_BODY
    assert "extra.txt" in rolled["removed"] or not (root / "extra.txt").exists()
    assert not (root / "extra.txt").exists()
    after = harness_sync.check_drift(client, root, MIRROR_LABEL)
    assert after["status"] == "clean"


# ---------------------------------------------------------------------------
# Ingestion connectors (R11)
# ---------------------------------------------------------------------------


def _transcript(bodies: list[str]) -> str:
    rows = [
        {
            "type": "user" if index % 2 == 0 else "assistant",
            "timestamp": "2026-09-25T10:00:00Z",
            "message": {"role": "user" if index % 2 == 0 else "assistant",
                        "content": body},
        }
        for index, body in enumerate(bodies)
    ]
    return "\n".join(json.dumps(row) for row in rows) + "\n"


def test_session_ingest_replacement_and_idempotency(api, connector_registered):
    source_key = f"session-{uuid.uuid4().hex[:8]}"
    payload = {
        "connector_namespace": CONNECTOR_NAMESPACE,
        "source_key": source_key,
        "transcript_format": "claude_jsonl",
        "transcript": _transcript(["first", "second", "third"]),
        "source_observed_at": None,
    }
    key = uuid.uuid4().hex
    first = api.post(
        "/v1/ingest/sessions", headers=headers(WORKER_TOKEN, PROJECT_ALIAS, key),
        json=payload,
    )
    assert first.status_code == 201, first.text
    receipt = first.json()["data"]
    assert receipt["generation"] == 1
    assert receipt["item_count"] == 4  # session header + 3 messages
    header = api.get(
        f"/v1/content/{receipt['content_ids'][0]}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert header.status_code == 200, header.text
    assert header.json()["data"]["payload"]["source"] == CONNECTOR_NAMESPACE
    assert header.json()["data"]["payload"]["message_count"] == 3
    message = api.get(
        f"/v1/content/{receipt['content_ids'][1]}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert message.status_code == 200, message.text
    assert message.json()["data"]["payload"]["text"] == "first"
    assert message.json()["data"]["payload"]["role"] == "user"
    replay = api.post(
        "/v1/ingest/sessions", headers=headers(WORKER_TOKEN, PROJECT_ALIAS, key),
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert replay.json()["data"] == receipt

    replaced = dict(payload)
    replaced["transcript"] = _transcript(["only"])
    second = api.post(
        "/v1/ingest/sessions",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json=replaced,
    )
    assert second.status_code == 201, second.text
    data = second.json()["data"]
    assert data["generation"] == 2
    assert data["replaced_run_id"] == receipt["run_id"]
    assert data["item_count"] == 2

    run = api.get(
        f"/v1/ingest/runs/{receipt['run_id']}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert run.status_code == 200
    contents = run.json()["data"]["contents"]
    assert len(contents) == 4
    # the previous generation's session content is superseded, not duplicated
    inspect = api.get(
        f"/v1/content/{receipt['content_ids'][0]}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert inspect.status_code == 200
    assert inspect.json()["data"]["status"] == "superseded"


def test_message_stream_ingest_preserves_typed_message(
    api, connector_registered
):
    response = api.post(
        "/v1/ingest/messages",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": f"messages-{uuid.uuid4().hex}",
            "transcript_format": "claude_jsonl",
            "transcript": _transcript(["one message"]),
        },
    )
    assert response.status_code == 201, response.text
    receipt = response.json()["data"]
    assert receipt["item_count"] == 1
    readback = api.get(
        f"/v1/content/{receipt['content_ids'][0]}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert readback.status_code == 200, readback.text
    content = readback.json()["data"]
    assert content["content_class"] == "message"
    assert content["payload"]["role"] == "user"
    assert content["payload"]["text"] == content["body"] == "one message"


def test_save_chat_ingest_preserves_session_and_message_payloads(
    api, connector_registered
):
    response = api.post(
        "/v1/ingest/save-chats",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": f"chat-{uuid.uuid4().hex}",
            "title": "saved conversation",
            "messages": [{"role": "user", "text": "hello", "observed_at": None}],
        },
    )
    assert response.status_code == 201, response.text
    content_ids = response.json()["data"]["content_ids"]
    assert len(content_ids) == 2
    header = api.get(
        f"/v1/content/{content_ids[0]}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    message = api.get(
        f"/v1/content/{content_ids[1]}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert header.status_code == message.status_code == 200
    assert header.json()["data"]["payload"]["source"] == CONNECTOR_NAMESPACE
    assert header.json()["data"]["payload"]["message_count"] == 1
    assert message.json()["data"]["payload"]["text"] == "hello"
    assert message.json()["data"]["body"] == "hello"


def test_local_state_ingest_preserves_typed_knowledge_content(
    api, connector_registered
):
    capture = {"status": "captured", "position": 42}
    response = api.post(
        "/v1/ingest/local-state",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": f"local-{uuid.uuid4().hex}",
            "capture": capture,
            "captured_at": None,
        },
    )
    assert response.status_code == 201, response.text
    content_id = response.json()["data"]["content_ids"][0]
    readback = api.get(
        f"/v1/content/{content_id}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert readback.status_code == 200, readback.text
    content = json.dumps(capture, ensure_ascii=False, sort_keys=True)
    assert readback.json()["data"]["payload"]["content"] == content
    assert readback.json()["data"]["body"] == content


def test_oversized_local_state_quarantine_replays_typed_error(
    api, connector_registered
):
    payload = {
        "connector_namespace": CONNECTOR_NAMESPACE,
        "source_key": f"large-state-{uuid.uuid4().hex}",
        "capture": {"blob": "x" * 65_536},
    }
    key = uuid.uuid4().hex
    first = api.post(
        "/v1/ingest/local-state",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, key),
        json=payload,
    )
    assert first.status_code == 422, first.text
    assert first.json()["error"]["code"] == "state_too_large_quarantined"
    assert any(
        field["path"] == "capture" and field["type"] == "state_too_large"
        for field in first.json()["error"]["fields"]
    )
    run_id = next(
        field["value"] for field in first.json()["error"]["fields"]
        if field["path"] == "run_id"
    )
    replay = api.post(
        "/v1/ingest/local-state",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, key),
        json=payload,
    )
    assert replay.status_code == 422
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.content == first.content
    run = api.get(
        f"/v1/ingest/runs/{run_id}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert run.status_code == 200
    assert run.json()["data"]["status"] == "quarantined"
    assert run.json()["data"]["item_count"] == 0


def test_diary_ingest_preserves_typed_entry(api, connector_registered):
    response = api.post(
        "/v1/ingest/diaries",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": f"diary-{uuid.uuid4().hex}",
            "entries": [{"entry_date": "2026-09-26", "text": "Retain this entry."}],
        },
    )
    assert response.status_code == 201, response.text
    content_id = response.json()["data"]["content_ids"][0]
    readback = api.get(
        f"/v1/content/{content_id}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert readback.status_code == 200, readback.text
    assert readback.json()["data"]["payload"]["entry"] == "Retain this entry."
    assert readback.json()["data"]["body"] == "Retain this entry."


def test_thinking_only_claude_session_ingests_without_loss(
    api, connector_registered
):
    transcript = json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "synthetic plan"}],
        },
    }) + "\n"
    response = api.post(
        "/v1/ingest/sessions",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": f"thinking-{uuid.uuid4().hex}",
            "transcript_format": "claude_jsonl",
            "transcript": transcript,
        },
    )
    assert response.status_code == 201, response.text
    receipt = response.json()["data"]
    assert receipt["item_count"] == 2
    content = api.get(
        f"/v1/content/{receipt['content_ids'][1]}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert content.status_code == 200, content.text
    assert content.json()["data"]["body"] == "[thinking] synthetic plan"
    assert content.json()["data"]["payload"]["parts"] == [
        {"type": "thinking", "text": "synthetic plan"}
    ]


def test_codex_text_plus_function_call_ingests_exact_arguments(
    api, connector_registered
):
    rows = [
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "synthetic answer"}],
        }},
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "lookup",
            "arguments": '{"z":1,"a":2}', "call_id": "fake-call",
            "namespace": "synthetic-tools",
        }},
    ]
    response = api.post(
        "/v1/ingest/sessions",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": f"codex-call-{uuid.uuid4().hex}",
            "transcript_format": "codex_jsonl",
            "transcript": "\n".join(json.dumps(row) for row in rows) + "\n",
        },
    )
    assert response.status_code == 201, response.text
    receipt = response.json()["data"]
    assert receipt["item_count"] == 3
    text = api.get(
        f"/v1/content/{receipt['content_ids'][1]}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    call = api.get(
        f"/v1/content/{receipt['content_ids'][2]}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert text.status_code == call.status_code == 200
    assert text.json()["data"]["body"] == "synthetic answer"
    assert text.json()["data"]["payload"]["parts"] == [
        {"type": "output_text", "text": "synthetic answer"}
    ]
    assert call.json()["data"]["body"] == (
        '[function_call:lookup] {"a":2,"z":1}'
    )
    assert call.json()["data"]["payload"]["parts"][0]["arguments"] == (
        '{"a":2,"z":1}'
    )
    assert call.json()["data"]["payload"]["parts"][0]["namespace"] == (
        "synthetic-tools"
    )


def test_claude_mixed_blocks_roundtrip_as_typed_parts(
    api, connector_registered
):
    import base64
    import hashlib

    image = b"synthetic-image"
    hidden = "fake-redacted"
    blocks = [
        {"type": "text", "text": "synthetic answer"},
        {"type": "thinking", "thinking": "synthetic plan", "signature": "fake"},
        {"type": "redacted_thinking", "data": hidden},
        {"type": "tool_use", "name": "lookup", "id": "fake-tool",
         "input": {"z": 1, "a": 2}},
        {"type": "tool_result", "tool_use_id": "fake-tool",
         "content": [{"type": "text", "text": "synthetic result"}]},
        {"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.b64encode(image).decode("ascii"),
        }},
    ]
    response = api.post(
        "/v1/ingest/messages",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": f"claude-mixed-{uuid.uuid4().hex}",
            "transcript_format": "claude_jsonl",
            "transcript": json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": blocks},
            }) + "\n",
        },
    )
    assert response.status_code == 201, response.text
    content_id = response.json()["data"]["content_ids"][0]
    readback = api.get(
        f"/v1/content/{content_id}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert readback.status_code == 200, readback.text
    data = readback.json()["data"]
    assert data["body"].startswith("synthetic answer\n[thinking] synthetic plan")
    assert '[tool_use:lookup] {"a":2,"z":1}' in data["body"]
    parts = data["payload"]["parts"]
    assert [part["type"] for part in parts] == [
        "text", "thinking", "redacted_thinking", "tool_use", "tool_result", "image"
    ]
    assert parts[1]["signature"] == {
        "field_path": "row.message.content[1].signature",
        "json_type": "string",
        "byte_length": len("fake".encode()),
        "sha256": hashlib.sha256(b"fake").hexdigest(),
    }
    assert parts[2]["sha256"] == hashlib.sha256(hidden.encode()).hexdigest()
    assert parts[3]["input"] == '{"a":2,"z":1}'
    assert parts[4]["content"] == "synthetic result"
    assert parts[5]["media_type"] == "image/png"
    assert parts[5]["byte_length"] == len(image)
    assert parts[5]["sha256"] == hashlib.sha256(image).hexdigest()


@pytest.mark.parametrize("kind", [
    "CommandExecution", "McpToolCall", "CollabAgentToolCall",
])
def test_codex_completed_tool_items_roundtrip_exactly(
    api, connector_registered, kind
):
    nested = {
        "type": kind, "id": "fake-id", "arguments": {"query": "synthetic"},
    }
    canonical = json.dumps(
        nested, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    response = api.post(
        "/v1/ingest/messages",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": f"codex-{kind}-{uuid.uuid4().hex}",
            "transcript_format": "codex_jsonl",
            "transcript": json.dumps({
                "type": "event_msg",
                "payload": {"type": "item_completed", "item": nested},
            }) + "\n",
        },
    )
    assert response.status_code == 201, response.text
    content_id = response.json()["data"]["content_ids"][0]
    readback = api.get(
        f"/v1/content/{content_id}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert readback.status_code == 200, readback.text
    assert readback.json()["data"]["body"] == (
        f"[item_completed:{kind}] {canonical}"
    )
    assert readback.json()["data"]["payload"]["parts"] == [
        {"type": "item_completed", "item": canonical}
    ]


def test_codex_reasoning_custom_call_and_outputs_roundtrip(
    api, connector_registered
):
    import hashlib

    rows = [
        {"type": "response_item", "payload": {
            "type": "reasoning", "encrypted_content": "fake-cipher",
        }},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "custom",
            "input": '{"q":"synthetic"}', "call_id": "fake-call",
        }},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call_output",
            "output": "synthetic custom result", "call_id": "fake-call",
        }},
        {"type": "response_item", "payload": {
            "type": "function_call_output",
            "output": "synthetic function result", "call_id": "fake-call-2",
        }},
        {"type": "event_msg", "payload": {
            "type": "agent_message", "content": "synthetic update",
        }},
    ]
    response = api.post(
        "/v1/ingest/messages",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": f"codex-mixed-{uuid.uuid4().hex}",
            "transcript_format": "codex_jsonl",
            "transcript": "\n".join(json.dumps(row) for row in rows) + "\n",
        },
    )
    assert response.status_code == 201, response.text
    ids = response.json()["data"]["content_ids"]
    assert len(ids) == len(rows)
    readbacks = [
        api.get(f"/v1/content/{content_id}",
                headers=headers(WORKER_TOKEN, PROJECT_ALIAS))
        for content_id in ids
    ]
    assert all(result.status_code == 200 for result in readbacks)
    contents = [result.json()["data"] for result in readbacks]
    assert contents[0]["payload"]["parts"][0]["encrypted_content"]["sha256"] == (
        hashlib.sha256(b"fake-cipher").hexdigest()
    )
    assert contents[1]["body"] == '[custom_tool_call:custom] {"q":"synthetic"}'
    assert contents[1]["payload"]["parts"][0]["input"] == '{"q":"synthetic"}'
    assert contents[2]["payload"]["parts"][0]["output"] == (
        "synthetic custom result"
    )
    assert contents[3]["payload"]["parts"][0]["output"] == (
        "synthetic function result"
    )
    assert contents[4]["payload"]["parts"][0]["content"] == "synthetic update"


@pytest.mark.parametrize(
    ("transcript_format", "row"),
    [
        ("claude_jsonl", {
            "type": "system", "content": "synthetic system instruction",
        }),
        ("codex_jsonl", {
            "type": "compacted",
            "payload": {"encrypted_content": "fake-cipher"},
        }),
    ],
)
def test_system_content_and_compacted_event_roundtrip(
    api, connector_registered, transcript_format, row
):
    import hashlib

    response = api.post(
        "/v1/ingest/messages",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": f"system-compaction-{uuid.uuid4().hex}",
            "transcript_format": transcript_format,
            "transcript": json.dumps(row) + "\n",
        },
    )
    assert response.status_code == 201, response.text
    content_id = response.json()["data"]["content_ids"][0]
    readback = api.get(
        f"/v1/content/{content_id}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert readback.status_code == 200, readback.text
    data = readback.json()["data"]
    if transcript_format == "claude_jsonl":
        assert data["body"] == "synthetic system instruction"
        assert data["payload"]["parts"] == [
            {"type": "text", "text": "synthetic system instruction"}
        ]
    else:
        digest = hashlib.sha256(b"fake-cipher").hexdigest()
        assert data["body"] == (
            f"[compaction encrypted bytes=11 sha256={digest}]"
        )
        assert data["payload"]["parts"][0]["encrypted_content"]["sha256"] == digest


@pytest.mark.parametrize("transcript_format", ["claude_jsonl", "codex_jsonl"])
def test_opaque_and_unknown_fields_never_enter_canonical_content(
    api, connector_registered, db, transcript_format
):
    import base64
    import hashlib

    sentinel = f"ROUND3_OPAQUE_SENTINEL_{uuid.uuid4().hex}"
    image_data = base64.b64encode(sentinel.encode()).decode("ascii")
    if transcript_format == "claude_jsonl":
        rows = [
            {"type": "assistant", "unknown_extra": sentinel,
             "message": {
                 "role": "assistant", "unknown_extra": sentinel,
                 "content": [
                     {"type": "text", "text": "safe", "unknown_extra": sentinel},
                     {"type": "thinking", "thinking": "safe",
                      "signature": sentinel, "unknown_extra": sentinel},
                     {"type": "redacted_thinking", "data": sentinel,
                      "unknown_extra": sentinel},
                     {"type": "tool_use", "name": "lookup",
                      "input": {"q": "safe", "unknown_extra": sentinel},
                      "unknown_extra": sentinel},
                     {"type": "tool_result", "content": "safe",
                      "unknown_extra": sentinel},
                     {"type": "image", "unknown_extra": sentinel,
                      "source": {"type": "base64", "media_type": "image/png",
                                 "data": image_data,
                                 "unknown_extra": sentinel}},
                 ],
             }},
            {"type": "attachment", "unknown_extra": sentinel,
             "attachment": {"type": "image", "unknown_extra": sentinel,
                            "source": {"type": "base64",
                                       "media_type": "image/png",
                                       "data": image_data}}},
            {"type": "result", "unknown_extra": sentinel,
             "result": {"type": "text", "text": "safe",
                        "unknown_extra": sentinel}},
        ]
    else:
        rows = [
            {"type": "response_item", "unknown_extra": sentinel,
             "payload": {"type": "message", "role": "assistant",
                         "encrypted_content": sentinel,
                         "unknown_extra": sentinel,
                         "content": [{"type": "output_text", "text": "safe",
                                      "unknown_extra": sentinel}]}},
            {"type": "event_msg", "payload": {
                "type": "item_completed",
                "item": {"type": "ImageView", "id": "fake",
                         "path": "/fake/image.png",
                         "image_url": "data:image/png;base64," + image_data,
                         "encrypted_content": sentinel,
                         "unknown_extra": sentinel}}},
            {"type": "event_msg", "payload": {
                "type": "item_completed",
                "item": {"type": "Reasoning", "id": "fake",
                         "raw_content": "safe",
                         "encrypted_content": sentinel,
                         "unknown_extra": sentinel}}},
            {"type": "response_item", "payload": {
                "type": "function_call", "name": "lookup",
                "arguments": json.dumps(
                    {"q": "safe", "unknown_extra": sentinel}
                ), "unknown_extra": sentinel}},
            {"type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "custom",
                "input": {"q": "safe", "unknown_extra": sentinel},
                "unknown_extra": sentinel}},
            {"type": "response_item", "payload": {
                "type": "compaction", "encrypted_content": sentinel,
                "unknown_extra": sentinel}},
            {"type": "response_item", "payload": {
                "type": "agent_message", "content": "safe",
                "unknown_extra": sentinel}},
            {"type": "response_item", "payload": {
                "type": "function_call_output", "output": "safe",
                "unknown_extra": sentinel}},
        ]
    response = api.post(
        "/v1/ingest/messages",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": f"opaque-{uuid.uuid4().hex}",
            "transcript_format": transcript_format,
            "transcript": "\n".join(json.dumps(row) for row in rows) + "\n",
        },
    )
    assert response.status_code == 201, response.status_code
    assert sentinel not in response.text
    receipt = response.json()["data"]
    assert len(receipt["content_ids"]) == len(rows)
    all_parts = []
    for content_id in receipt["content_ids"]:
        readback = api.get(
            f"/v1/content/{content_id}",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
        )
        assert readback.status_code == 200
        assert sentinel not in readback.text
        all_parts.extend(readback.json()["data"]["payload"]["parts"])
    records = asyncio.run(db(
        "SELECT body_text, payload::text AS payload_json "
        "FROM cortex_core.content_revisions "
        "WHERE scope_id = $1 AND content_id = ANY($2::uuid[])",
        uuid.UUID(receipt["scope_id"]),
        [uuid.UUID(content_id) for content_id in receipt["content_ids"]],
    ))
    assert len(records) == len(rows)
    assert all(
        sentinel not in record["body_text"]
        and sentinel not in record["payload_json"]
        for record in records
    )
    markers = []
    def collect(value):
        if isinstance(value, dict):
            if {"field_path", "json_type", "byte_length", "sha256"} <= value.keys():
                markers.append(value)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    collect(all_parts)
    assert markers
    assert any("unknown_extra" in marker["field_path"] for marker in markers)
    assert any(
        marker["sha256"] == hashlib.sha256(sentinel.encode()).hexdigest()
        for marker in markers
    )


@pytest.mark.parametrize("transcript_format", ["claude_jsonl", "codex_jsonl"])
def test_json_string_tool_arguments_and_data_uris_never_leak(
    api, connector_registered, db, transcript_format
):
    import base64
    import hashlib

    sentinel = f"ROUND4_OPAQUE_SENTINEL_{uuid.uuid4().hex}"
    data_uri = (
        "data:image/png;base64,"
        + base64.b64encode(sentinel.encode()).decode("ascii")
    )
    if transcript_format == "claude_jsonl":
        arguments = json.dumps(
            {"data": sentinel, "url": data_uri, "q": "safe"}
        )
        row = {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "lookup",
                         "input": arguments}],
        }}
    else:
        arguments = json.dumps({
            "args": [
                {"url": data_uri},
                {"url": "https://example.invalid/fake"},
            ],
            "data": sentinel,
        })
        row = {"type": "response_item", "payload": {
            "type": "function_call", "name": "lookup",
            "arguments": arguments,
        }}
    response = api.post(
        "/v1/ingest/messages",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": f"uri-{uuid.uuid4().hex}",
            "transcript_format": transcript_format,
            "transcript": json.dumps(row) + "\n",
        },
    )
    assert response.status_code == 201, response.status_code
    assert sentinel not in response.text
    receipt = response.json()["data"]
    content_id = receipt["content_ids"][0]
    readback = api.get(
        f"/v1/content/{content_id}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert readback.status_code == 200
    assert sentinel not in readback.text
    records = asyncio.run(db(
        "SELECT body_text, payload::text AS payload_json "
        "FROM cortex_core.content_revisions "
        "WHERE scope_id = $1 AND content_id = $2",
        uuid.UUID(receipt["scope_id"]), uuid.UUID(content_id),
    ))
    assert len(records) == 1
    assert sentinel not in records[0]["body_text"]
    assert sentinel not in records[0]["payload_json"]
    part = readback.json()["data"]["payload"]["parts"][0]
    field = "input" if transcript_format == "claude_jsonl" else "arguments"
    projected = json.loads(part[field])
    markers = []
    def collect(value):
        if isinstance(value, dict):
            if {"field_path", "json_type", "byte_length", "sha256"} <= value.keys():
                markers.append(value)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    collect(projected)
    assert any(
        marker["field_path"].endswith(".url")
        and marker["media_type"] == "image/png"
        and marker["byte_length"] == len(sentinel.encode())
        and marker["sha256"] == hashlib.sha256(sentinel.encode()).hexdigest()
        for marker in markers
    )
    assert any(marker["field_path"].endswith(".data") for marker in markers)
    if transcript_format == "codex_jsonl":
        assert "https://example.invalid/fake" in part[field]


def test_invalid_json_string_claude_tool_input_quarantines(
    api, connector_registered, db
):
    scope_id = asyncio.run(db(
        "SELECT scope_id FROM cortex_core.scope_aliases WHERE alias = $1",
        PROJECT_ALIAS,
    ))[0]["scope_id"]
    def count():
        return asyncio.run(db(
            "SELECT count(*) AS count FROM cortex_core.content_items "
            "WHERE scope_id = $1",
            scope_id,
        ))[0]["count"]
    before = count()
    row = {"type": "assistant", "message": {
        "role": "assistant",
        "content": [{"type": "tool_use", "name": "lookup",
                     "input": "not-json"}],
    }}
    response = api.post(
        "/v1/ingest/messages",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": f"invalid-input-{uuid.uuid4().hex}",
            "transcript_format": "claude_jsonl",
            "transcript": json.dumps(row) + "\n",
        },
    )
    assert response.status_code == 422, response.status_code
    assert any(
        field["type"] == "unsupported_content_shape"
        for field in response.json()["error"]["fields"]
    )
    assert count() == before


def test_nested_codex_json_string_arguments_quarantine_not_server_error(
    api, connector_registered, db
):
    scope_id = asyncio.run(db(
        "SELECT scope_id FROM cortex_core.scope_aliases WHERE alias = $1",
        PROJECT_ALIAS,
    ))[0]["scope_id"]
    before = asyncio.run(db(
        "SELECT (SELECT count(*) FROM cortex_core.content_items "
        "WHERE scope_id = $1) AS content_count, "
        "(SELECT count(*) FROM cortex_core.content_outbox_events "
        "WHERE scope_id = $1) AS outbox_count",
        scope_id,
    ))[0]
    nested = {"q": "safe"}
    for _ in range(15):
        nested = {"args": nested}
    row = {"type": "response_item", "payload": {
        "type": "function_call", "name": "lookup",
        "arguments": json.dumps(nested),
    }}
    response = api.post(
        "/v1/ingest/messages",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": f"nested-{uuid.uuid4().hex}",
            "transcript_format": "codex_jsonl",
            "transcript": json.dumps(row) + "\n",
        },
    )
    assert response.status_code == 422, response.status_code
    assert response.json()["error"]["code"] == "transcript_parse_failed_quarantined"
    assert any(
        field["type"] == "unsupported_content_shape"
        for field in response.json()["error"]["fields"]
    )
    after = asyncio.run(db(
        "SELECT (SELECT count(*) FROM cortex_core.content_items "
        "WHERE scope_id = $1) AS content_count, "
        "(SELECT count(*) FROM cortex_core.content_outbox_events "
        "WHERE scope_id = $1) AS outbox_count",
        scope_id,
    ))[0]
    assert dict(after) == dict(before)


@pytest.mark.parametrize("case", ["body_and_payload", "parts_expansion"])
def test_transcript_item_limit_quarantines_before_any_w1_write(
    api, connector_registered, db, case
):
    from cortex_v2.ingest.parsers import MAX_ITEM_BYTES

    scope_id = asyncio.run(db(
        "SELECT scope_id FROM cortex_core.scope_aliases WHERE alias = $1",
        PROJECT_ALIAS,
    ))[0]["scope_id"]
    def counts():
        return asyncio.run(db(
            "SELECT (SELECT count(*) FROM cortex_core.content_items "
            "WHERE scope_id = $1) AS content_count, "
            "(SELECT count(*) FROM cortex_core.content_outbox_events "
            "WHERE scope_id = $1) AS outbox_count",
            scope_id,
        ))[0]

    before = counts()
    if case == "body_and_payload":
        row = {"type": "assistant", "message": {
            "role": "assistant", "content": "x" * (MAX_ITEM_BYTES // 2 + 128)
        }}
        transcript_format = "claude_jsonl"
    else:
        row = {"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": "ok",
            **{f"extra_{index:04d}": "v" for index in range(700)},
        }}
        transcript_format = "codex_jsonl"
    response = api.post(
        "/v1/ingest/messages",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": f"bounded-{uuid.uuid4().hex}",
            "transcript_format": transcript_format,
            "transcript": json.dumps(row) + "\n",
        },
    )
    assert response.status_code == 422, response.status_code
    assert response.json()["error"]["code"] == "transcript_parse_failed_quarantined"
    assert any(
        field["type"] == "message_too_large"
        for field in response.json()["error"]["fields"]
    )
    assert dict(counts()) == dict(before)


@pytest.mark.parametrize(
    ("route", "path"),
    [
        ("save_chat", "/v1/ingest/save-chats"),
        ("diary", "/v1/ingest/diaries"),
        ("local_state", "/v1/ingest/local-state"),
    ],
)
def test_every_non_transcript_ingress_item_exact_limit_and_one_over(
    api, connector_registered, db, route, path
):
    from cortex_v2.ingest.parsers import MAX_ITEM_BYTES

    def item_size(value):
        if route == "save_chat":
            body = value
            payload = {
                "role": "user", "text": value,
                "origin": {"kind": "save_chat"},
            }
        elif route == "diary":
            body = value
            payload = {
                "entry": value, "origin": {"kind": "diary"},
                "entry_date": "2026-09-26",
            }
        else:
            body = json.dumps(
                {"blob": value}, ensure_ascii=False, sort_keys=True
            )
            payload = {
                "content": body, "origin": {"kind": "local_state"}
            }
        return len(body.encode()) + len(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode())

    def boundary(target):
        for suffix in ("", '"', "\\", "\n", "é", '"\\', "\t"):
            base = item_size(suffix)
            stride = item_size("x" + suffix) - base
            estimate = (target - base) // stride
            for length in range(max(1, estimate - 3), estimate + 4):
                value = "x" * length + suffix
                if item_size(value) == target:
                    return value
        raise AssertionError("request shape cannot express the item boundary")

    def request(value):
        common = {
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": f"limit-{uuid.uuid4().hex}",
        }
        if route == "save_chat":
            return {
                **common, "title": "synthetic chat",
                "messages": [{"role": "user", "text": value}],
            }
        if route == "diary":
            return {
                **common,
                "entries": [{"entry_date": "2026-09-26", "text": value}],
            }
        return {**common, "capture": {"blob": value}}

    scope_id = asyncio.run(db(
        "SELECT scope_id FROM cortex_core.scope_aliases WHERE alias = $1",
        PROJECT_ALIAS,
    ))[0]["scope_id"]
    def counts():
        return dict(asyncio.run(db(
            "SELECT (SELECT count(*) FROM cortex_core.content_items "
            "WHERE scope_id = $1) AS content_count, "
            "(SELECT count(*) FROM cortex_core.content_outbox_events "
            "WHERE scope_id = $1) AS outbox_count",
            scope_id,
        ))[0])

    exact, over = boundary(MAX_ITEM_BYTES), boundary(MAX_ITEM_BYTES + 1)
    assert item_size(exact) == MAX_ITEM_BYTES
    assert item_size(over) == MAX_ITEM_BYTES + 1
    first = api.post(
        path, headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json=request(exact),
    )
    assert first.status_code == 201, first.status_code
    after_exact = counts()
    rejected = api.post(
        path, headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json=request(over),
    )
    assert rejected.status_code == 422, rejected.status_code
    expected_code = (
        "state_too_large" if route == "local_state" else "message_too_large"
    )
    assert rejected.json()["error"]["code"] == expected_code
    assert counts() == after_exact


def test_malformed_transcript_is_quarantined_not_partially_applied(
    api, connector_registered, db
):
    source_key = f"bad-{uuid.uuid4().hex[:8]}"
    key = uuid.uuid4().hex
    payload = {
        "connector_namespace": CONNECTOR_NAMESPACE,
        "source_key": source_key,
        "transcript_format": "claude_jsonl",
        "transcript": '{"type":"user","message":{"role":"user",'
                      '"content":"ok"}}\n{broken\n',
        "source_observed_at": None,
    }
    response = api.post(
        "/v1/ingest/sessions",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, key),
        json=payload,
    )
    assert response.status_code == 422, response.text
    problem = response.json()
    assert "data" not in problem
    assert problem["error"]["code"] == "transcript_parse_failed_quarantined"
    fields = problem["error"]["fields"]
    run_id = next(field["value"] for field in fields if field["path"] == "run_id")
    assert run_id in problem["error"]["message"]
    assert any(field.get("type") == "malformed_json" for field in fields)
    replay = api.post(
        "/v1/ingest/sessions",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, key),
        json=payload,
    )
    assert replay.status_code == 422
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert replay.content == response.content
    assert response.headers["X-Request-ID"] != replay.headers["X-Request-ID"]
    assert response.headers["X-Request-ID"] != run_id
    assert replay.headers["X-Request-ID"] != run_id

    rows = asyncio.run(db(
        "SELECT status, item_count, quarantine_reason FROM cortex_context.ingest_runs "
        "WHERE source_key = $1",
        source_key,
    ))
    assert len(rows) == 1
    assert rows[0]["status"] == "quarantined"
    assert rows[0]["item_count"] == 0
    reason = rows[0]["quarantine_reason"]
    if isinstance(reason, str):
        reason = json.loads(reason)
    assert reason["failures"][0]["code"] == "malformed_json"
    receipts = asyncio.run(db(
        "SELECT receipt_kind, receipt FROM cortex_core.command_receipts "
        "WHERE principal_id = $1 AND operation = 'ingest.session' "
        "AND idempotency_key = $2",
        uuid.UUID(FIXTURE["worker_principal_id"]), key,
    ))
    assert len(receipts) == 1
    assert receipts[0]["receipt_kind"] == "committed"
    persisted = receipts[0]["receipt"]
    if isinstance(persisted, str):
        persisted = json.loads(persisted)
    assert persisted["state"] == "quarantined"
    assert persisted["run_id"] == run_id
    assert persisted["content_ids"] == []


def test_unparseable_message_quarantines_without_losing_previous_generation(
    api, connector_registered, db
):
    source_key = f"partial-{uuid.uuid4().hex}"
    first = api.post(
        "/v1/ingest/sessions",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": source_key,
            "transcript_format": "claude_jsonl",
            "transcript": _transcript(["original"]),
        },
    )
    assert first.status_code == 201, first.text
    previous = first.json()["data"]
    scope_id = uuid.UUID(previous["scope_id"])

    def row_counts():
        rows = asyncio.run(db(
            "SELECT (SELECT count(*) FROM cortex_core.content_items "
            "WHERE scope_id = $1) AS content_count, "
            "(SELECT count(*) FROM cortex_core.content_outbox_events "
            "WHERE scope_id = $1) AS outbox_count",
            scope_id,
        ))
        return rows[0]["content_count"], rows[0]["outbox_count"]

    before = row_counts()
    bad_row = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "image", "data": "unreadable"}],
        },
    }
    bad = api.post(
        "/v1/ingest/sessions",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": source_key,
            "transcript_format": "claude_jsonl",
            "transcript": _transcript(["new"]) + json.dumps(bad_row) + "\n",
        },
    )
    assert bad.status_code == 422, bad.text
    assert bad.json()["error"]["code"] == "transcript_parse_failed_quarantined"
    assert any(
        field.get("type") == "missing_text"
        for field in bad.json()["error"]["fields"]
    )
    assert row_counts() == before
    old = api.get(
        f"/v1/content/{previous['content_ids'][0]}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert old.status_code == 200
    assert old.json()["data"]["status"] == "current"
    old_run = api.get(
        f"/v1/ingest/runs/{previous['run_id']}",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
    )
    assert old_run.status_code == 200
    assert old_run.json()["data"]["status"] == "committed"
    assert old_run.json()["data"]["item_count"] == previous["item_count"]


def test_near_limit_malformed_transcript_has_bounded_valid_quarantine(
    api, connector_registered, db
):
    import hashlib

    source_key = f"large-bad-{uuid.uuid4().hex}"
    transcript = "{" + "x" * 999_998
    response = api.post(
        "/v1/ingest/sessions",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "connector_namespace": CONNECTOR_NAMESPACE,
            "source_key": source_key,
            "transcript_format": "claude_jsonl",
            "transcript": transcript,
        },
    )
    assert response.status_code == 422, response.text
    fields = response.json()["error"]["fields"]
    run_id = next(field["value"] for field in fields if field["path"] == "run_id")
    rows = asyncio.run(db(
        "SELECT status, quarantine_payload FROM cortex_context.ingest_runs "
        "WHERE run_id = $1",
        uuid.UUID(run_id),
    ))
    assert len(rows) == 1
    assert rows[0]["status"] == "quarantined"
    stored = rows[0]["quarantine_payload"]
    if isinstance(stored, str):
        stored = json.loads(stored)
    original = json.dumps(
        {"transcript": transcript, "transcript_format": "claude_jsonl"},
        ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    assert stored["original_length"] == len(original)
    assert stored["sha256"] == hashlib.sha256(original).hexdigest()
    assert stored["excerpt"] == original[:125_000].decode("utf-8")
    assert len(json.dumps(stored, ensure_ascii=False).encode("utf-8")) <= 1_000_000


def test_quarantine_error_is_typed_across_sdk_cli_and_mcp(
    api, connector_registered
):
    import io

    from cortex_v2.cli.main import EXIT_API, main as cli_main
    from cortex_v2.clients.client import CortexClient
    from cortex_v2.clients.config import ClientProfile
    from cortex_v2.clients.errors import CortexApiError
    from cortex_v2.mcp.protocol import McpCredentials, McpServer

    profile = ClientProfile(
        base_url=API_URL,
        token=WORKER_TOKEN,
        default_scope=PROJECT_ALIAS,
        default_read_scopes=(),
        installation_label=None,
        principal_label=None,
        source="quarantine-parity-test",
    )
    client = CortexClient(profile)
    payload = {
        "connector_namespace": CONNECTOR_NAMESPACE,
        "source_key": f"parity-bad-{uuid.uuid4().hex}",
        "transcript_format": "claude_jsonl",
        "transcript": "{broken\n",
    }
    key = uuid.uuid4().hex
    with pytest.raises(CortexApiError) as first:
        client.call("ingest.session", payload=payload, idempotency_key=key)
    with pytest.raises(CortexApiError) as replay:
        client.call("ingest.session", payload=payload, idempotency_key=key)
    assert first.value.status == replay.value.status == 422
    assert first.value.code == replay.value.code == (
        "transcript_parse_failed_quarantined"
    )
    assert first.value.request_id != replay.value.request_id
    assert first.value.fields == replay.value.fields
    run_id = next(
        field["value"] for field in first.value.fields
        if field["path"] == "run_id"
    )
    assert run_id in first.value.message

    out, err = io.StringIO(), io.StringIO()
    exit_code = cli_main(
        [
            "ingest.session", "--data", json.dumps(payload),
            "--scope", PROJECT_ALIAS,
            "--idempotency-key", uuid.uuid4().hex,
        ],
        client=client, out=out, err=err,
    )
    assert exit_code == EXIT_API
    assert json.loads(err.getvalue())["error"]["code"] == first.value.code

    server = McpServer(client_factory=lambda credentials: CortexClient(
        ClientProfile(
            base_url=API_URL,
            token=credentials.token,
            default_scope=credentials.scope,
            default_read_scopes=credentials.read_scopes,
            installation_label=None,
            principal_label=None,
            source="quarantine-mcp-test",
        )
    ))
    message = asyncio.run(server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "ingest_session",
                "arguments": {**payload, "idempotency_key": uuid.uuid4().hex},
            },
        },
        credentials=McpCredentials(
            token=WORKER_TOKEN, scope=PROJECT_ALIAS, read_scopes=(),
        ),
    ))
    assert message["result"]["isError"] is True
    error = json.loads(message["result"]["content"][0]["text"])["error"]
    assert error["code"] == first.value.code
    assert error["status"] == 422
    assert "quarantined as run " in error["message"]


# ---------------------------------------------------------------------------
# Transport parity (R23/F05)
# ---------------------------------------------------------------------------


def test_same_command_equal_semantics_across_http_cli_mcp(api):
    from cortex_v2.cli.main import main as cli_main
    from cortex_v2.clients.client import CortexClient
    from cortex_v2.clients.config import ClientProfile
    from cortex_v2.mcp.protocol import McpCredentials, McpServer

    profile = ClientProfile(
        base_url=API_URL,
        token=WORKER_TOKEN,
        default_scope=PROJECT_ALIAS,
        default_read_scopes=(),
        installation_label=None,
        principal_label=None,
        source="parity-test",
    )
    body = f"parity {uuid.uuid4().hex}"
    payload = {"record_type": "note", "body": body}

    http_key = uuid.uuid4().hex
    http = api.post(
        "/v1/memory/records",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, http_key),
        json=payload,
    )
    assert http.status_code == 201, http.text

    client = CortexClient(profile)
    cli_key = uuid.uuid4().hex
    import io

    out, err = io.StringIO(), io.StringIO()
    code = cli_main(
        [
            "memory.record",
            "--data", json.dumps(payload),
            "--scope", PROJECT_ALIAS,
            "--idempotency-key", cli_key,
        ],
        client=client,
        out=out,
        err=err,
    )
    assert code == 0, err.getvalue()
    cli_receipt = json.loads(out.getvalue())["data"]
    assert cli_receipt["record_id"]

    server = McpServer(client_factory=lambda credentials: CortexClient(
        ClientProfile(
            base_url=API_URL,
            token=credentials.token,
            default_scope=credentials.scope,
            default_read_scopes=credentials.read_scopes,
            installation_label=None,
            principal_label=None,
            source="parity-test",
        )
    ))
    mcp_key = uuid.uuid4().hex
    response = asyncio.run(
        server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "memory_record",
                    "arguments": {**payload, "idempotency_key": mcp_key},
                },
            },
            credentials=McpCredentials(
                token=WORKER_TOKEN, scope=PROJECT_ALIAS, read_scopes=()
            ),
        )
    )
    result = response["result"]
    assert result["isError"] is False
    mcp_receipt = json.loads(result["content"][0]["text"])["data"]
    assert mcp_receipt["record_id"]

    # replaying the CLI key over HTTP yields the identical receipt
    replay = api.post(
        "/v1/memory/records",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, cli_key),
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.json()["data"] == cli_receipt
    # MCP replay of its own key is identical too
    replay_mcp = asyncio.run(
        server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "memory_record",
                    "arguments": {**payload, "idempotency_key": mcp_key},
                },
            },
            credentials=McpCredentials(
                token=WORKER_TOKEN, scope=PROJECT_ALIAS, read_scopes=()
            ),
        )
    )
    assert json.loads(replay_mcp["result"]["content"][0]["text"])["data"] == (
        mcp_receipt
    )


def test_write_without_idempotency_key_rejected_on_every_transport(api):
    response = api.post(
        "/v1/context/prepare",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
        json={"intent": "prose_edit", "budget_bytes": 8192},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_required"


def test_scope_header_required_for_scoped_operations(api):
    response = api.post(
        "/v1/context/prepare",
        headers=headers(WORKER_TOKEN, key=uuid.uuid4().hex),
        json={"intent": "prose_edit", "budget_bytes": 8192},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "scope_required"


def test_latest_persona_enactment_wins_over_uuid_order(api, context_material):
    first_id = str(uuid.UUID(int=1))
    latest_id = str(uuid.UUID(int=(1 << 128) - 1))
    for persona_id, body in (
        (first_id, "Earlier persona\n"),
        (latest_id, "Newest persona\n"),
    ):
        response = api.post(
            "/v1/context/personas",
            headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
            json={
                "persona_id": persona_id,
                "template_version": "persona.v1",
                "body": body,
                "payload": {},
            },
        )
        assert response.status_code == 201, response.text

    prepared = prepare(api, budget=65_536)
    assert prepared.status_code == 201, prepared.text
    persona = prepared.json()["data"]["context"]["persona"]
    assert persona["persona_id"] == latest_id
    assert persona["body"] == "Newest persona\n"


def test_prepare_and_harness_preview_select_same_latest_persona(
    api, context_material
):
    revised = api.post(
        "/v1/context/personas",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "persona_id": context_material["persona_id"],
            "template_version": "persona.v2",
            "body": "Older persona, now revision two.\n",
            "payload": {},
        },
    )
    assert revised.status_code == 201, revised.text
    latest = api.post(
        "/v1/context/personas",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS, uuid.uuid4().hex),
        json={
            "persona_id": None,
            "template_version": "persona.v1",
            "body": "Latest enacted persona.\n",
            "payload": {},
        },
    )
    assert latest.status_code == 201, latest.text
    latest_id = latest.json()["data"]["persona_id"]
    prepared = prepare(api, budget=65_536)
    preview = api.post(
        "/v1/harness/previews",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
        json={"mirror_label": f"persona-parity-{RUN}", "pins": {}},
    )
    assert prepared.status_code == 201, prepared.text
    assert preview.status_code == 200, preview.text
    persona = prepared.json()["data"]["provenance"]["persona"]
    mirrored = preview.json()["data"]["input_provenance"]["persona"]
    assert persona["persona_id"] == latest_id
    assert mirrored == persona
    files = {file["path"]: file["body"] for file in preview.json()["data"]["files"]}
    assert files["persona.md"] == "Latest enacted persona.\n"


def test_prepare_and_mirror_have_total_order_for_tied_personas(api):
    async def seed_tie():
        import asyncpg

        first_id, last_id = uuid.UUID(int=2), uuid.UUID(int=3)
        connection = await asyncpg.connect(MIGRATOR_DATABASE_URL, timeout=20)
        try:
            async with connection.transaction():
                await _attest_candidate_installation(connection)
                scope_id = await connection.fetchval(
                    "SELECT scope_id FROM cortex_core.scope_aliases WHERE alias = $1",
                    PROJECT_ALIAS,
                )
                for persona_id in (first_id, last_id):
                    await connection.execute(
                        "INSERT INTO cortex_context.persona_revisions "
                        "(scope_id, persona_id, revision, template_version, payload, "
                        "body, created_by_principal) "
                        "VALUES ($1, $2, 1, 'persona.v1', '{}'::jsonb, $3, $4)",
                        scope_id, persona_id, "Tied persona.\n",
                        uuid.UUID(FIXTURE["worker_principal_id"]),
                    )
                timestamps = await connection.fetchval(
                    "SELECT count(DISTINCT created_at) "
                    "FROM cortex_context.persona_revisions "
                    "WHERE scope_id = $1 AND persona_id = ANY($2::uuid[])",
                    scope_id, [first_id, last_id],
                )
                assert timestamps == 1
        finally:
            await connection.close()
        return str(last_id)

    expected = asyncio.run(seed_tie())
    prepared = prepare(api, budget=65_536)
    preview = api.post(
        "/v1/harness/previews",
        headers=headers(WORKER_TOKEN, PROJECT_ALIAS),
        json={"mirror_label": f"persona-tie-{RUN}", "pins": {}},
    )
    assert prepared.status_code == 201, prepared.text
    assert preview.status_code == 200, preview.text
    assert prepared.json()["data"]["provenance"]["persona"]["persona_id"] == expected
    assert preview.json()["data"]["input_provenance"]["persona"]["persona_id"] == expected


@pytest.mark.parametrize("wrong_identity", ["database", "api_installation", "api_worker"])
def test_break_glass_refuses_mismatched_installation_identity(wrong_identity):
    installation = uuid.UUID(FIXTURE["installation_id"])
    owner = uuid.UUID(FIXTURE["owner_principal_id"])
    worker = uuid.UUID(FIXTURE["worker_principal_id"])
    db_installation = uuid.uuid4() if wrong_identity == "database" else installation
    api_installation = (
        uuid.uuid4() if wrong_identity == "api_installation" else installation
    )
    api_worker = uuid.uuid4() if wrong_identity == "api_worker" else worker

    with pytest.raises(RuntimeError, match="candidate recovery identity mismatch"):
        _assert_recovery_identity(
            db_installation, owner, worker, api_installation, api_worker
        )
