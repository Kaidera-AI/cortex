"""Retrieval DB integration suite (runs in the integration phase).

Fixtures and needs (stated for the integrator):
* A fresh disposable candidate database with migrations 0001, 0002, 0003,
  0004 and 0005 applied in ascending order, plus the standard W1-style
  fixture secrets directory (owner/worker tokens, project/shared/local
  aliases and scope ids) exported through CORTEX_V2_SANDBOX_SECRETS_DIR.
* CORTEX_V2_TEST_API_URL and a resolvable instance profile
  (CORTEX_V2_SANDBOX_INSTANCE) exactly like tests/test_w1_candidate.py.
* The HTTP sections additionally require the integrator to have mounted
  cortex_v2.retrieval.operations.OPERATIONS generically; they are skipped
  unless CORTEX_V2_TEST_RETRIEVAL_MOUNTED=1 is set.
* The vector-contract sections require 0004 (cortex_processing) to be
  applied; they assert honest degradation when it is absent and real
  behavior when a space has been published.

The suite never touches old production, the Slice 1 sandbox or the W1
candidate; it runs only against the disposable candidate named by the
active instance profile.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

INSTANCE = os.environ.get("CORTEX_V2_SANDBOX_INSTANCE", "")
SECRETS_DIR = os.environ.get("CORTEX_V2_SANDBOX_SECRETS_DIR", "")
API_URL = os.environ.get("CORTEX_V2_TEST_API_URL", "")
if not INSTANCE or not SECRETS_DIR or not API_URL:
    pytest.skip(
        "retrieval integration suite requires a candidate instance profile, "
        "secrets directory and API URL",
        allow_module_level=True,
    )

SECRETS = Path(SECRETS_DIR)


def secret(name_suffix: str) -> str:
    path = SECRETS / f"{INSTANCE}-{name_suffix}"
    if not path.is_file():
        pytest.skip(f"candidate secret {name_suffix} is unavailable")
    return path.read_text().strip()

APP_URL = secret("database-url-app")
MIGRATOR_URL = secret("database-url-migrator")
OWNER_TOKEN = secret(
    "owner-token"
)
WORKER_TOKEN = secret(
    "worker-token"
)
FIXTURE = json.loads(secret("fixture"))

PROJECT_ALIAS = FIXTURE["project_alias"]
PROJECT_SCOPE = uuid.UUID(FIXTURE["project_scope_id"])
OWNER_PRINCIPAL = uuid.UUID(FIXTURE["owner_principal_id"])
WORKER_PRINCIPAL = uuid.UUID(FIXTURE["worker_principal_id"])
MOUNTED = os.environ.get("CORTEX_V2_TEST_RETRIEVAL_MOUNTED", "") == "1"


def run(coroutine):
    return asyncio.run(coroutine)


async def connect_as_app():
    return await asyncpg.connect(APP_URL)


async def app_connection_context(
    connection, *, write: bool = False, declare_policy: bool = False
):
    """Set the transaction-local scope context, then (optionally) declare the
    writer-policy revision exactly like the W1 command path does: the context
    must be visible BEFORE the revision is read, or RLS hides writer_policies
    and the 0002 insert-time recheck fails on a stale '0' declaration."""
    await connection.execute(
        "SELECT set_config('cortex.principal_id', $1, true)", str(OWNER_PRINCIPAL)
    )
    await connection.execute(
        "SELECT set_config('cortex.read_scope_ids', $1, true)", str(PROJECT_SCOPE)
    )
    await connection.execute(
        "SELECT set_config('cortex.write_scope_id', $1, true)",
        str(PROJECT_SCOPE) if write else "",
    )
    if declare_policy:
        revision = await connection.fetchval(
            "SELECT COALESCE(max(revision), 0)::text "
            "FROM cortex_auth.writer_policies WHERE scope_id = $1",
            PROJECT_SCOPE,
        )
        await connection.execute(
            "SELECT set_config('cortex.policy_revision', $1, true)", revision or "0"
        )


async def insert_content(
    connection, *, body: str, content_class="knowledge", payload=None
) -> uuid.UUID:
    content_id = uuid.uuid4()
    payload = payload if payload is not None else {"content": body}
    await connection.execute(
        """
        INSERT INTO cortex_core.content_items
            (scope_id, content_id, content_class, created_by_principal)
        VALUES ($1, $2, $3, $4)
        """,
        PROJECT_SCOPE,
        content_id,
        content_class,
        OWNER_PRINCIPAL,
    )
    await connection.execute(
        """
        INSERT INTO cortex_core.content_revisions
            (scope_id, content_id, revision, payload, body_text, content_hash,
             author_principal_id)
        VALUES ($1, $2, 1, $3::jsonb, $4, $5, $6)
        """,
        PROJECT_SCOPE,
        content_id,
        json.dumps(payload, sort_keys=True),
        body,
        b"\xab" * 32,
        OWNER_PRINCIPAL,
    )
    await connection.execute(
        """
        INSERT INTO cortex_core.content_lexical_documents
            (scope_id, content_id, revision, source_text)
        VALUES ($1, $2, 1, $3)
        """,
        PROJECT_SCOPE,
        content_id,
        body,
    )
    return content_id


# ---------------------------------------------------------------------------
# Migration and schema contract
# ---------------------------------------------------------------------------


def test_migration_0005_applied_after_0004_in_ascending_order():
    async def scenario():
        connection = await asyncpg.connect(MIGRATOR_URL)
        try:
            rows = await connection.fetch(
                "SELECT migration_id FROM cortex_core.schema_migrations"
            )
            ids = [row["migration_id"] for row in rows]
            assert "0005_retrieval.sql" in ids
            if "0004_processing.sql" in ids:
                assert ids.index("0004_processing.sql") < ids.index(
                    "0005_retrieval.sql"
                )
        finally:
            await connection.close()

    run(scenario())


def test_retrieval_tables_force_row_level_security():
    async def scenario():
        connection = await asyncpg.connect(MIGRATOR_URL)
        try:
            rows = await connection.fetch(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                  FROM pg_class
                 WHERE relnamespace = 'cortex_retrieval'::regnamespace
                   AND relkind = 'r'
                """
            )
            assert rows, "cortex_retrieval tables must exist"
            for row in rows:
                assert row["relrowsecurity"], row["relname"]
                assert row["relforcerowsecurity"], row["relname"]
            trgm = await connection.fetchval(
                """
                SELECT n.nspname
                  FROM pg_extension e
                  JOIN pg_namespace n ON n.oid = e.extnamespace
                 WHERE e.extname = 'pg_trgm'
                """
            )
            assert trgm == "public"
        finally:
            await connection.close()

    run(scenario())


def test_app_role_cannot_read_retrieval_rows_without_scope_context():
    async def scenario():
        connection = await connect_as_app()
        try:
            async with connection.transaction():
                for table in (
                    "graph_entities",
                    "graph_assertions",
                    "graph_assertion_evidence",
                    "code_repositories",
                    "code_generations",
                    "code_symbols",
                    "code_edges",
                    "code_annotations",
                    "trigram_documents",
                ):
                    count = await connection.fetchval(
                        f"SELECT count(*) FROM cortex_retrieval.{table}"
                    )
                    assert count == 0, table
        finally:
            await connection.close()

    run(scenario())


# ---------------------------------------------------------------------------
# End-to-end projection, retrieval and retraction as the app role
# ---------------------------------------------------------------------------


def test_memory_search_end_to_end_with_trigram_projection():
    async def scenario():
        from cortex_v2.retrieval.models import MemorySearchRequest
        from cortex_v2.retrieval.planner import RetrievalPlanner
        from cortex_v2.store import Principal, Scope, ScopeContext

        connection = await connect_as_app()
        try:
            async with connection.transaction():
                await app_connection_context(
                    connection, write=True, declare_policy=True
                )
                body = "Payments depends on Ledger for double-entry bookkeeping"
                content_id = await insert_content(connection, body=body)
                rebuilt = await connection.fetchval(
                    "SELECT cortex_retrieval.rebuild_trigram_projection($1)",
                    PROJECT_SCOPE,
                )
                assert rebuilt >= 1

                principal = Principal(OWNER_PRINCIPAL, uuid.UUID(int=0))
                scope = Scope(
                    alias=PROJECT_ALIAS,
                    scope_id=PROJECT_SCOPE,
                    kind="project",
                    can_read=True,
                    can_write=True,
                    can_publish=False,
                )
                context = ScopeContext(principal, scope, (scope,))
                planner = RetrievalPlanner()
                result = await planner.search(
                    connection,
                    context,
                    MemorySearchRequest(query="Ledger bookkeeping", intent="concept"),
                )
                hits = {
                    hit["citation"]["content_id"] for hit in result["hits"]
                }
                assert str(content_id) in hits
                assert result["coverage"]["vector_space"] is None
                assert "vectors_not_configured" in result["degraded"]
                stages = {entry["stage"] for entry in result["stages"]}
                assert {"lexical", "trigram"} <= stages
        finally:
            await connection.close()

    run(scenario())


def test_graph_extraction_retraction_and_salvage_round_trip():
    async def scenario():
        from cortex_v2.retrieval import jobs
        from cortex_v2.retrieval.memory_graph import retract_source

        connection = await connect_as_app()
        try:
            async with connection.transaction():
                await app_connection_context(
                    connection, write=True, declare_policy=True
                )
                first = await insert_content(
                    connection, body="Payments depends on Ledger exclusively here."
                )
                second = await insert_content(
                    connection, body="Payments depends on Ledger as well, elsewhere."
                )

                context = _attempt_context(
                    "graph.memory.extract",
                    payload={"pin": {"content_id": str(first), "revision": 1}},
                )
                services = _ConnectionServices(connection, first, 1)
                report = await jobs.handle_memory_extract(context, services)
                assert report.ok, report.detail
                await jobs.publish_memory_extract(connection, context, report)

                context2 = _attempt_context(
                    "graph.memory.extract",
                    payload={"pin": {"content_id": str(second), "revision": 1}},
                )
                services2 = _ConnectionServices(connection, second, 1)
                report2 = await jobs.handle_memory_extract(context2, services2)
                await jobs.publish_memory_extract(connection, context2, report2)

                active = await connection.fetch(
                    """
                    SELECT assertion_id, status
                      FROM cortex_retrieval.graph_assertions
                     WHERE scope_id = $1 AND status = 'active'
                    """,
                    PROJECT_SCOPE,
                )
                # Deduplication: one active assertion, two evidence edges.
                assert len(active) == 1
                evidence = await connection.fetchval(
                    """
                    SELECT count(*) FROM cortex_retrieval.graph_assertion_evidence
                     WHERE scope_id = $1
                    """,
                    PROJECT_SCOPE,
                )
                assert evidence == 2

                # Invalidating one source must retract only its evidence: the
                # assertion survives on the remaining source (salvage).
                stats = await retract_source(
                    connection,
                    scope_id=PROJECT_SCOPE,
                    content_id=first,
                    reason="source_invalidated",
                )
                assert stats["evidence_removed"] == 1
                assert stats["retracted"] == 0
                assert stats["salvaged"] == 1
                still_active = await connection.fetchval(
                    """
                    SELECT count(*) FROM cortex_retrieval.graph_assertions
                     WHERE scope_id = $1 AND status = 'active'
                    """,
                    PROJECT_SCOPE,
                )
                assert still_active == 1

                # Removing the last source tombstones the assertion.
                stats = await retract_source(
                    connection,
                    scope_id=PROJECT_SCOPE,
                    content_id=second,
                    reason="source_invalidated",
                )
                assert stats["retracted"] == 1
                retracted = await connection.fetchrow(
                    """
                    SELECT status, retraction_reason, retracted_at
                      FROM cortex_retrieval.graph_assertions
                     WHERE scope_id = $1
                    """,
                    PROJECT_SCOPE,
                )
                assert retracted["status"] == "retracted"
                assert retracted["retraction_reason"] == "source_invalidated"
                assert retracted["retracted_at"] is not None
        finally:
            await connection.close()

    run(scenario())


def test_authored_assertions_survive_source_retraction():
    async def scenario():
        from cortex_v2.retrieval.memory_graph import retract_source

        connection = await connect_as_app()
        try:
            async with connection.transaction():
                await app_connection_context(
                    connection, write=True, declare_policy=True
                )
                content_id = await insert_content(
                    connection, body="Human authored relation evidence body."
                )
                subject = uuid.uuid4()
                obj = uuid.uuid4()
                for entity_id, entity_key in ((subject, "settlement"), (obj, "audit")):
                    await connection.execute(
                        """
                        INSERT INTO cortex_retrieval.graph_entities
                            (scope_id, entity_id, entity_key, entity_type)
                        VALUES ($1, $2, $3, 'concept')
                        """,
                        PROJECT_SCOPE,
                        entity_id,
                        entity_key,
                    )
                assertion_id = uuid.uuid4()
                await connection.execute(
                    """
                    INSERT INTO cortex_retrieval.graph_assertions
                        (scope_id, assertion_id, subject_entity_id, relation,
                         object_entity_id, origin, author_principal_id)
                    VALUES ($1, $2, $3, 'relates_to', $4, 'authored', $5)
                    """,
                    PROJECT_SCOPE,
                    assertion_id,
                    subject,
                    obj,
                    OWNER_PRINCIPAL,
                )
                await connection.execute(
                    """
                    INSERT INTO cortex_retrieval.graph_assertion_evidence
                        (scope_id, assertion_id, evidence_seq, content_id,
                         source_revision, span_start, span_end)
                    VALUES ($1, $2, 1, $3, 1, 0, 10)
                    """,
                    PROJECT_SCOPE,
                    assertion_id,
                    content_id,
                )
                stats = await retract_source(
                    connection,
                    scope_id=PROJECT_SCOPE,
                    content_id=content_id,
                    reason="source_deleted",
                )
                assert stats["preserved_authored"] == 1
                assert stats["retracted"] == 0
                status = await connection.fetchval(
                    """
                    SELECT status FROM cortex_retrieval.graph_assertions
                     WHERE scope_id = $1 AND assertion_id = $2
                    """,
                    PROJECT_SCOPE,
                    assertion_id,
                )
                assert status == "active"
                # Authored assertions can never be deleted by the app role.
                with pytest.raises(asyncpg.PostgresError):
                    await connection.execute(
                        """
                        DELETE FROM cortex_retrieval.graph_assertions
                         WHERE scope_id = $1 AND assertion_id = $2
                        """,
                        PROJECT_SCOPE,
                        assertion_id,
                    )
        finally:
            await connection.close()

    run(scenario())


def test_code_graph_publish_freshness_and_annotations_round_trip():
    async def scenario():
        from cortex_v2.retrieval import jobs
        from cortex_v2.retrieval.code_graph import (
            callers_of_symbol,
            hotspots,
            resolve_repository,
        )

        connection = await connect_as_app()
        run_id = uuid.uuid4().hex[:12]
        repository_key = f"helix/demo-{run_id}"
        try:
            async with connection.transaction():
                await app_connection_context(connection, write=True)
                payload = {
                    "pin": {
                        "repository_key": repository_key,
                        "commit_sha": "a" * 40,
                        "snapshot_id": "snap-1",
                    },
                    "files": [
                        {
                            "path": "src/ledger.py",
                            "source": "def post_entry():\n    validate()\n\n"
                            "def validate():\n    pass\n",
                        },
                        {
                            "path": "src/api.py",
                            "source": "from src.ledger import post_entry\n\n"
                            "def handler():\n    post_entry()\n",
                        },
                    ],
                }
                context = _attempt_context("graph.code.extract", payload=payload)
                report = await jobs.handle_code_extract(
                    context, _ConnectionServices(connection)
                )
                assert report.ok, report.detail
                await jobs.publish_code_extract(connection, context, report)

                repository = await resolve_repository(
                    connection, scope_id=PROJECT_SCOPE, repository_key=repository_key
                )
                assert repository is not None
                callers = await callers_of_symbol(
                    connection,
                    scope_id=PROJECT_SCOPE,
                    repository_id=repository["repository_id"],
                    generation=1,
                    symbol_key="src/ledger.py::validate",
                    limit=10,
                )
                assert any(
                    caller["symbol_key"] == "src/ledger.py::post_entry"
                    for caller in callers["callers"]
                )
                assert callers["graph"]["status"] == "fresh"

                stale = await callers_of_symbol(
                    connection,
                    scope_id=PROJECT_SCOPE,
                    repository_id=repository["repository_id"],
                    generation=1,
                    symbol_key="src/ledger.py::validate",
                    limit=10,
                    head_commit="b" * 40,
                )
                assert stale["graph"]["status"] == "stale"
                assert stale["degraded"] and "code_graph_stale" in stale["degraded"]

                spots = await hotspots(
                    connection,
                    scope_id=PROJECT_SCOPE,
                    repository_id=repository["repository_id"],
                    generation=1,
                    limit=5,
                )
                top_two = {spot["symbol_key"] for spot in spots["hotspots"][:2]}
                assert top_two == {
                    "src/ledger.py::post_entry",
                    "src/ledger.py::validate",
                }
                assert all(spot["fan_in"] == 1 for spot in spots["hotspots"][:2])

                # Authored annotation survives a republished generation.
                await connection.execute(
                    """
                    INSERT INTO cortex_retrieval.code_annotations
                        (scope_id, annotation_id, repository_id, symbol_key,
                         annotation, author_principal_id)
                    VALUES ($1, $2, $3, 'src/ledger.py::validate',
                            'Money path: never reorder without review.', $4)
                    """,
                    PROJECT_SCOPE,
                    uuid.uuid4(),
                    repository["repository_id"],
                    OWNER_PRINCIPAL,
                )
                payload2 = dict(payload)
                payload2["pin"] = {
                    "repository_key": repository_key,
                    "commit_sha": "c" * 40,
                    "snapshot_id": "snap-2",
                }
                context2 = _attempt_context("graph.code.extract", payload=payload2)
                report2 = await jobs.handle_code_extract(
                    context2, _ConnectionServices(connection)
                )
                await jobs.publish_code_extract(connection, context2, report2)
                annotations = await connection.fetch(
                    """
                    SELECT symbol_key, annotation
                      FROM cortex_retrieval.code_annotations
                     WHERE scope_id = $1 AND repository_id = $2
                    """,
                    PROJECT_SCOPE,
                    repository["repository_id"],
                )
                # The run-unique repository_key keeps this scenario rerun-safe:
                # annotations from prior runs live under different repositories.
                assert len(annotations) == 1
                assert annotations[0]["symbol_key"] == "src/ledger.py::validate"
                generations = await connection.fetch(
                    """
                    SELECT generation, status FROM cortex_retrieval.code_generations
                     WHERE scope_id = $1 AND repository_id = $2
                     ORDER BY generation
                    """,
                    PROJECT_SCOPE,
                    repository["repository_id"],
                )
                assert [row["status"] for row in generations] == [
                    "retired",
                    "published",
                ]
        finally:
            await connection.close()

    run(scenario())


def test_cross_scope_retrieval_rows_are_invisible():
    async def scenario():
        connection = await connect_as_app()
        try:
            async with connection.transaction():
                await app_connection_context(connection, write=True)
                foreign_scope = uuid.UUID("99999999-9999-4999-8999-999999999999")
                with pytest.raises(asyncpg.PostgresError):
                    await connection.execute(
                        """
                        INSERT INTO cortex_retrieval.graph_entities
                            (scope_id, entity_id, entity_key, entity_type)
                        VALUES ($1, $2, 'foreign', 'concept')
                        """,
                        foreign_scope,
                        uuid.uuid4(),
                    )
        finally:
            await connection.close()

    run(scenario())


# ---------------------------------------------------------------------------
# HTTP surface (only when the integrator has mounted OPERATIONS)
# ---------------------------------------------------------------------------


def _token_is_live(bearer: str) -> bool:
    """Probe a fixture credential; one-shot credentials may be consumed by
    earlier suites sharing the candidate database."""
    import httpx

    try:
        with httpx.Client(base_url=API_URL, timeout=10) as client:
            response = client.get(
                "/v1/auth/principal",
                headers={"Authorization": f"Bearer {bearer}"},
            )
            return response.status_code == 200
    except (OSError, httpx.HTTPError):
        return False



@pytest.mark.skipif(not MOUNTED, reason="retrieval OPERATIONS are not mounted")
def test_http_memory_search_and_inspect_hit():
    import httpx

    token = WORKER_TOKEN if _token_is_live(WORKER_TOKEN) else OWNER_TOKEN
    if not _token_is_live(token):
        pytest.skip(
            "no live fixture bearer credential on this shared database; "
            "the HTTP retrieval contract needs a live token to prove"
        )
    with httpx.Client(
        base_url=API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Cortex-Scope": PROJECT_ALIAS,
        },
    ) as client:
        response = client.post(
            "/v1/memory/searches",
            json={"query": "ledger", "intent": "concept", "limit": 5},
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert "stages" in data and "coverage" in data and "degraded" in data


def _attempt_context(kind, *, content_id=None, revision=1, payload=None):
    from test_retrieval_fakes import make_attempt_context

    return make_attempt_context(
        job_kind=kind,
        scope_id=PROJECT_SCOPE,
        content_id=content_id,
        source_revision=revision,
        payload=payload if payload is not None else {"pin": {}},
    )


class _ConnectionServices:
    """Minimal AttemptServices backed by the already-scoped app connection.

    Only read_source and run_blocking are implemented; graph handlers use
    nothing else. Each read is a short statement inside the ambient
    transaction of the integration scenario, mirroring what the real worker
    services provide in their own short transactions.
    """

    role = "graph"

    def __init__(self, connection, content_id=None, revision=1):
        self.connection = connection
        self.content_id = content_id
        self.revision = revision

    async def read_source(self, context):
        from cortex_v2.processing.contracts import SourceRef

        row = await self.connection.fetchrow(
            """
            SELECT r.scope_id, r.content_id, r.revision, i.content_class,
                   r.body_text, r.content_hash, r.payload
              FROM cortex_core.content_revisions AS r
              JOIN cortex_core.content_items AS i
                ON i.scope_id = r.scope_id AND i.content_id = r.content_id
             WHERE r.scope_id = $1 AND r.content_id = $2 AND r.revision = $3
            """,
            context.scope_id,
            context.content_id,
            context.source_revision,
        )
        if row is None:
            return None
        payload = row["payload"]
        return SourceRef(
            scope_id=row["scope_id"],
            content_id=row["content_id"],
            revision=row["revision"],
            content_class=row["content_class"],
            media_type="text/plain",
            body_text=row["body_text"],
            content_hash=row["content_hash"],
            payload=json.loads(payload) if isinstance(payload, str) else dict(payload),
        )

    async def run_blocking(self, function, *args):
        return function(*args)
