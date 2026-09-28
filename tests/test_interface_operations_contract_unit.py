"""Operation contract tests: frozen handler signatures and mounted-path
invocation with correct idempotency handling (R23/F05).

The integration contract is:
  scoped_write: async (connection, context, idempotency_key, payload,
                       path_params) -> (status, data, replayed)
  scoped_read:  async (connection, context, payload, path_params) -> dict

Handlers are invoked against a fake connection that speaks just enough of the
W1 receipt/content SQL to exercise the real code paths, including replay and
key-reuse conflict.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid

import pytest
from pydantic import ValidationError

import cortex_v2.ingest.operations as ingest_operations
import cortex_v2.interface.operations as interface_operations
from cortex_v2.interface.context import enact_rule, get_skill, prepare_context
from cortex_v2.interface.models import SkillGetQuery
from cortex_v2.interface.registry import (
    clear_mounted_marks,
    mark_modules_mounted,
    mounted_modules,
)
from cortex_v2.receipts import request_digest
from cortex_v2.store import ApiProblem, Principal, Scope, ScopeContext

WRITE_PARAMS = ["connection", "context", "idempotency_key", "payload",
                "path_params"]
READ_PARAMS = ["connection", "context", "payload", "path_params"]

SCOPE_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
PRINCIPAL_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
INSTALLATION_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


def make_context() -> ScopeContext:
    scope = Scope(
        alias="proj-a",
        scope_id=SCOPE_ID,
        kind="project",
        can_read=True,
        can_write=True,
        can_publish=False,
    )
    return ScopeContext(
        principal=Principal(
            principal_id=PRINCIPAL_ID, installation_id=INSTALLATION_ID
        ),
        selected=scope,
        read_scopes=(scope,),
    )


class FakeRecord(dict):
    pass


class FakeConnection:
    """Records SQL; answers the exact reads the W1 use cases perform."""

    def __init__(self, receipt_row: FakeRecord | None = None) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.receipt_row = receipt_row

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        return "INSERT 0 1"

    async def fetchrow(self, sql: str, *args):
        self.executed.append((sql, args))
        if "command_receipts" in sql:
            return self.receipt_row
        if "source_connectors" in sql:
            return FakeRecord(connector_id=uuid.uuid4())
        return None

    async def fetchval(self, sql: str, *args):
        self.executed.append((sql, args))
        if "MAX(revision)" in sql:
            return 0
        if "skill_binding_sets" in sql:
            return 0
        return None

    async def fetch(self, sql: str, *args):
        self.executed.append((sql, args))
        return []

    def statements(self, needle: str) -> list[tuple[str, tuple]]:
        return [entry for entry in self.executed if needle in entry[0]]


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Signature conformance for every published operation
# ---------------------------------------------------------------------------


def _published_entries():
    yield from interface_operations.OPERATIONS
    yield from ingest_operations.OPERATIONS


def test_every_published_handler_uses_the_frozen_signature():
    checked = 0
    for entry in _published_entries():
        handler = entry["handler"]
        assert handler is not None, entry["operation_id"]
        assert inspect.iscoroutinefunction(handler), entry["operation_id"]
        signature = inspect.signature(handler)
        positional = [
            parameter.name
            for parameter in signature.parameters.values()
            if parameter.kind is parameter.POSITIONAL_OR_KEYWORD
        ]
        expected = (
            WRITE_PARAMS if entry["kind"] == "scoped_write" else READ_PARAMS
        )
        assert positional == expected, (
            f"{entry['operation_id']}: {positional} != {expected}"
        )
        for parameter in signature.parameters.values():
            if parameter.kind is parameter.KEYWORD_ONLY:
                assert parameter.default is not parameter.empty, (
                    f"{entry['operation_id']}: keyword-only parameter "
                    f"{parameter.name!r} must have a default"
                )
        checked += 1
    assert checked == len(interface_operations.OPERATIONS) + len(
        ingest_operations.OPERATIONS
    )
    assert checked >= 19


def test_all_scoped_write_operations_declare_idempotency():
    for entry in _published_entries():
        if entry["kind"] == "scoped_write":
            assert entry.get("requires_idempotency_key", True) is not False, (
                entry["operation_id"]
            )


@pytest.mark.parametrize(
    ("operation_id", "failure_code"),
    [
        ("ingest.local_state", "state_too_large"),
        ("ingest.diary", "message_too_large"),
        ("ingest.save_chat", "message_too_large"),
    ],
)
def test_ingest_over_limit_cards_include_plain_422_code(
    operation_id, failure_code
):
    card = next(
        entry["usage"] for entry in ingest_operations.OPERATIONS
        if entry["operation_id"] == operation_id
    )
    assert failure_code in card["failure_codes"]
    if operation_id == "ingest.local_state":
        assert "state_too_large_quarantined" in card["failure_codes"]


# ---------------------------------------------------------------------------
# Mounted-path invocation with idempotency handling
# ---------------------------------------------------------------------------

RULE_PAYLOAD = {
    "rule_id": None,
    "slug": "no-force-push",
    "obligation": "mandatory",
    "body": "Never force-push shared branches.\n",
}


def rule_digest(payload: dict) -> bytes:
    import hashlib

    return request_digest(
        {
            "operation": "rule.enact",
            "scope_id": str(SCOPE_ID),
            "rule_id": None,
            "slug": payload["slug"],
            "obligation": payload["obligation"],
            "body_sha256": hashlib.sha256(
                payload["body"].encode("utf-8")
            ).hexdigest(),
        }
    )


def test_scoped_write_fresh_command_commits_receipt():
    connection = FakeConnection()
    status, receipt, replayed = run(
        enact_rule(connection, make_context(), "key-1", dict(RULE_PAYLOAD), {})
    )
    assert (status, replayed) == (201, False)
    assert receipt["state"] == "committed"
    assert receipt["operation"] == "rule.enact"
    assert receipt["revision"] == 1
    assert connection.statements("INSERT INTO cortex_context.rule_revisions")
    assert connection.statements(
        "INSERT INTO cortex_core.command_receipts"
    )


def test_scoped_write_same_key_same_payload_replays_stored_receipt():
    stored = {"state": "committed", "operation": "rule.enact",
              "rule_id": "stored", "revision": 1}
    connection = FakeConnection(
        receipt_row=FakeRecord(
            request_hash=rule_digest(RULE_PAYLOAD),
            receipt=json.dumps(stored),
        )
    )
    status, receipt, replayed = run(
        enact_rule(connection, make_context(), "key-1", dict(RULE_PAYLOAD), {})
    )
    assert (status, replayed) == (200, True)
    assert receipt == stored
    assert not connection.statements(
        "INSERT INTO cortex_context.rule_revisions"
    )


def test_scoped_write_same_key_different_payload_conflicts():
    connection = FakeConnection(
        receipt_row=FakeRecord(
            request_hash=b"\x01" * 32,
            receipt=json.dumps({"state": "committed"}),
        )
    )
    with pytest.raises(ApiProblem) as excinfo:
        run(
            enact_rule(
                connection, make_context(), "key-1", dict(RULE_PAYLOAD), {}
            )
        )
    assert excinfo.value.status == 409
    assert excinfo.value.code == "idempotency_key_reused"


def test_prepare_context_invocation_preserves_mandatory_sections():
    connection = FakeConnection()
    status, receipt, replayed = run(
        prepare_context(
            connection,
            make_context(),
            "prep-key-1",
            {"intent": "prose_edit", "budget_bytes": 8192,
             "recall_limit": 0},
            {},
        )
    )
    assert (status, replayed) == (201, False)
    assert receipt["state"] == "committed"
    context_sections = receipt["context"]
    assert context_sections["identity"]["scope_alias"] == "proj-a"
    assert "policy" in context_sections
    assert "mandatory_rules" in context_sections
    assert receipt["budget"]["composed_bytes"] <= 8192
    assert connection.statements(
        "INSERT INTO cortex_context.context_preparations"
    )
    assert connection.statements("INSERT INTO cortex_core.command_receipts")


def test_ingest_local_state_invocation_commits_generation_one():
    from cortex_v2.ingest.connectors import ingest_local_state

    connection = FakeConnection()
    status, receipt, replayed = run(
        ingest_local_state(
            connection,
            make_context(),
            "ls-key-1",
            {
                "connector_namespace": "w2-local",
                "source_key": "state-1",
                "capture": {"mode": "dark", "tabs": 3},
                "captured_at": None,
            },
            {},
        )
    )
    assert (status, replayed) == (201, False)
    assert receipt["state"] == "committed"
    assert receipt["generation"] == 1
    assert receipt["item_count"] == 1
    assert len(receipt["content_ids"]) == 1
    assert connection.statements("INSERT INTO cortex_context.ingest_runs")
    assert connection.statements(
        "INSERT INTO cortex_context.ingest_run_contents"
    )


def test_ingest_oversized_capture_quarantines_through_mounted_path():
    from cortex_v2.ingest.connectors import ingest_local_state

    connection = FakeConnection()
    status, receipt, replayed = run(
        ingest_local_state(
            connection,
            make_context(),
            "ls-key-2",
            {
                "connector_namespace": "w2-local",
                "source_key": "state-2",
                "capture": {"blob": "x" * 70_000},
                "captured_at": None,
            },
            {},
        )
    )
    assert (status, replayed) == (422, False)
    assert receipt["state"] == "quarantined"
    assert receipt["failures"][0]["code"] == "state_too_large"
    inserts = connection.statements("INSERT INTO cortex_context.ingest_runs")
    assert inserts
    assert not connection.statements(
        "INSERT INTO cortex_context.ingest_run_contents"
    )


def test_skill_bind_invocation_advances_binding_revision():
    from cortex_v2.interface.context import bind_skills

    connection = FakeConnection()
    skill_id = str(uuid.uuid4())
    status, receipt, replayed = run(
        bind_skills(
            connection,
            make_context(),
            "bind-key-1",
            {
                "bindings": [
                    {"skill_id": skill_id, "revision": 1, "precedence": 10}
                ],
                "expected_revision": 0,
            },
            {},
        )
    )
    assert (status, replayed) == (201, False)
    assert receipt["binding_revision"] == 1
    assert receipt["bindings"][0]["skill_id"] == skill_id
    assert connection.statements(
        "INSERT INTO cortex_context.skill_bindings"
    )


def test_skill_bind_expected_revision_conflict():
    from cortex_v2.interface.context import bind_skills

    connection = FakeConnection()
    with pytest.raises(ApiProblem) as excinfo:
        run(
            bind_skills(
                connection,
                make_context(),
                "bind-key-2",
                {
                    "bindings": [
                        {"skill_id": str(uuid.uuid4()), "revision": 1,
                         "precedence": 10}
                    ],
                    "expected_revision": 5,
                },
                {},
            )
        )
    assert excinfo.value.status == 409
    assert excinfo.value.code == "binding_revision_conflict"


# ---------------------------------------------------------------------------
# skill.get query coercion: omitted/empty optional fields never 422
# ---------------------------------------------------------------------------


def test_skill_bindings_read_invocation_reports_revision():
    from cortex_v2.interface.context import get_bindings

    connection = FakeConnection()
    data = run(
        get_bindings(connection, make_context(), None, {})
    )
    assert data["revision"] == 0
    assert data["bindings"] == []
    assert data["scope_id"] == str(SCOPE_ID)


def test_skill_get_query_accepts_omitted_and_string_revisions():
    assert SkillGetQuery.model_validate({}).revision is None
    assert SkillGetQuery.model_validate({"revision": None}).revision is None
    assert SkillGetQuery.model_validate({"revision": ""}).revision is None
    assert SkillGetQuery.model_validate({"revision": "3"}).revision == 3
    assert SkillGetQuery.model_validate({"revision": 2}).revision == 2
    with pytest.raises(ValidationError):
        SkillGetQuery.model_validate({"revision": "0"})
    with pytest.raises(ValidationError):
        SkillGetQuery.model_validate({"revision": "abc"})


def test_skill_get_handler_tolerates_none_payload():
    connection = FakeConnection()
    with pytest.raises(ApiProblem) as excinfo:
        run(
            get_skill(
                connection,
                make_context(),
                None,
                {"skill_id": str(uuid.uuid4())},
            )
        )
    # Reaches the scoped lookup (404 on the fake), not a validation 422.
    assert excinfo.value.status == 404
    assert excinfo.value.code == "skill_not_found"


# ---------------------------------------------------------------------------
# Discovery mount honesty
# ---------------------------------------------------------------------------


def test_mounted_modules_env_and_marks():
    clear_mounted_marks()
    try:
        # Nothing is mounted by default - not even the W1 routes.
        assert mounted_modules(env={}) == frozenset()
        assert mounted_modules(
            env={"CORTEX_V2_MOUNTED_MODULES": "identity_memory, interface"}
        ) == frozenset({"identity_memory", "interface"})
        mark_modules_mounted(["interface"])
        assert mounted_modules(env={}) == frozenset({"interface"})
    finally:
        clear_mounted_marks()


def test_fresh_process_reports_w1_unmounted_until_integrator_marks():
    from cortex_v2.interface.discovery import (
        UNMOUNTED_REASON,
        effective_module_state,
        visible_operations,
    )
    from cortex_v2.interface.registry import build_registry

    registry = build_registry()
    clear_mounted_marks()
    try:
        # A fresh CLI/MCP/worker process: only the API's create_app mounts
        # the W1 routes, so nothing may claim they are served here.
        assert mounted_modules(env={}) == frozenset()
        state, reason = effective_module_state(
            registry, "identity_memory", mounted=frozenset()
        )
        assert state == "unavailable"
        assert reason == UNMOUNTED_REASON
        summaries = {
            item["operation_id"]: item
            for item in visible_operations(
                registry, can_write=True, is_owner=True,
                mounted=frozenset(),
            )
        }
        assert summaries["memory.record"]["state"] == "unavailable"
        assert summaries["memory.record"]["reason"] == UNMOUNTED_REASON

        # The API composition root marks W1 after mounting -> ready.
        mark_modules_mounted(["identity_memory"])
        state, reason = effective_module_state(registry, "identity_memory")
        assert (state, reason) == ("ready", None)
        summaries = {
            item["operation_id"]: item
            for item in visible_operations(
                registry, can_write=True, is_owner=True,
            )
        }
        assert summaries["memory.record"]["state"] == "ready"
    finally:
        clear_mounted_marks()


def test_unmounted_module_operations_are_marked_unavailable_with_reason():
    from cortex_v2.interface.discovery import (
        UNMOUNTED_REASON,
        effective_module_state,
        visible_operations,
    )
    from cortex_v2.interface.registry import build_registry

    registry = build_registry()
    clear_mounted_marks()
    try:
        state, reason = effective_module_state(
            registry, "ingest", mounted=frozenset({"identity_memory"})
        )
        assert state == "unavailable"
        assert reason == UNMOUNTED_REASON

        summaries = {
            item["operation_id"]: item
            for item in visible_operations(
                registry,
                can_write=True,
                is_owner=True,
                mounted=frozenset({"identity_memory"}),
            )
        }
        ingest_item = summaries["ingest.session"]
        assert ingest_item["state"] == "unavailable"
        assert ingest_item["reason"] == UNMOUNTED_REASON
        # W1 routes are mounted by app.py itself and stay ready.
        assert summaries["memory.record"]["state"] == "ready"

        mounted_summaries = {
            item["operation_id"]: item
            for item in visible_operations(
                registry,
                can_write=True,
                is_owner=True,
                mounted=frozenset({"identity_memory", "ingest"}),
            )
        }
        assert mounted_summaries["ingest.session"]["state"] == "ready"
    finally:
        clear_mounted_marks()


def test_importable_but_unmounted_module_state_stays_ready_in_registry():
    # The registry reports contract/import truth; mount truth is layered on
    # top by discovery. Both facts stay separately visible.
    from cortex_v2.interface.registry import build_registry

    registry = build_registry()
    assert registry.module_state("ingest").state == "ready"
    assert registry.module_state("ingest").reason is None
