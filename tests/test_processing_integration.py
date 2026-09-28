"""Database-backed integration tests for the Processing module (W3).

Skipped unless the environment names a fresh candidate database. Required:

    CORTEX_V2_PROCESSING_DATABASE_URL    postgresql://cortex_v2_app@host:5432/cortex_v2
    CORTEX_V2_PROCESSING_MIGRATOR_URL    postgresql://cortex_v2_migrator@host:5432/cortex_v2

Prerequisites the integration phase provides (not created here):

* PostgreSQL 18 with pgvector installed by the **database owner**
  (``CREATE EXTENSION vector SCHEMA public;``) — pgvector 0.8.6 is not a trusted
  extension and ``cortex_v2_migrator`` is deliberately not a superuser, so 0004
  fails closed with that instruction when the type is missing.
* Migrations applied in ascending order: ``0001_core.sql``,
  ``0002_w1_identity_memory.sql``, optionally ``0003_coordination.sql`` (0004
  does not depend on it), then ``0004_processing.sql`` — and before ``0005``.
* The ``cortex_v2_app`` / ``cortex_v2_migrator`` roles from
  ``deploy/postgres-init`` (LOGIN, NOSUPERUSER, NOBYPASSRLS).
* No doc-role Python packages are required: the suite proves the typed
  ``executor_not_activated`` / ``waiting_for_executor`` behaviour of a deployment
  without the Document Processor, and the pure-stdlib pipeline end to end.

The suite seeds its own installation, principals, credentials, scopes, aliases,
grants, budget policy and content revisions through the migrator role, using a
unique run id, so it never depends on another module's fixtures and never
mutates rows it did not create. Nothing is deleted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import uuid
from typing import Any, Awaitable, Callable

import asyncpg
import pytest

from cortex_v2.processing import runtime as worker_module
from cortex_v2.processing import commands, handlers, keys, parsers, queue, repository
from cortex_v2.processing.contracts import (
    AttemptContext,
    AttemptServices,
    Chunk,
    ChunkingPolicy,
    ExecutionReport,
    Outcome,
    ParseBlock,
    Span,
)
from cortex_v2.processing.registry import JOB_REGISTRY, JobRegistry
from cortex_v2.processing.settings import parse_settings
from cortex_v2.store import ApiProblem, authenticate, resolve_scopes, token_digest

DATABASE_URL = os.environ.get("CORTEX_V2_PROCESSING_DATABASE_URL")
MIGRATOR_URL = os.environ.get("CORTEX_V2_PROCESSING_MIGRATOR_URL")

if not DATABASE_URL or not MIGRATOR_URL:
    pytest.skip(
        "processing integration suite needs CORTEX_V2_PROCESSING_DATABASE_URL and "
        "CORTEX_V2_PROCESSING_MIGRATOR_URL pointing at a fresh candidate database "
        "with 0001, 0002 and 0004 applied",
        allow_module_level=True,
    )

RUN = uuid.uuid4().hex[:12]
PEPPER = hashlib.sha256(b"processing integration pepper").digest()
OWNER_TOKEN = f"proc-owner-token-{RUN}"
WORKER_TOKEN = f"proc-worker-token-{RUN}"

PROJECT_ALIAS = f"proc-project-{RUN}"
FOREIGN_ALIAS = f"proc-foreign-{RUN}"
BUDGET_ALIAS = f"proc-budget-{RUN}"

PROFILE_TEXT = uuid.UUID("c2a00000-0000-4000-8000-000000000001")
PROFILE_DOC_AUTO = uuid.UUID("c2a00000-0000-4000-8000-000000000005")
PROFILE_DISTILL = uuid.UUID("c2a00000-0000-4000-8000-000000000006")

SPACE_DIMENSIONS = 64
SPACE_CHUNKING = {
    "schema_version": 1,
    "policy_id": "chunk.paragraph",
    "version": 1,
    "kind": "structural",
    "target_chars": 1200,
    "max_chars": 2000,
    "overlap_chars": 0,
}

FIXTURE: dict[str, Any] = {}
_SEEDED = False


def run(coroutine: Awaitable[Any]) -> Any:
    return asyncio.run(coroutine)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def _seed() -> None:
    installation = uuid.uuid4()
    owner = uuid.uuid4()
    worker = uuid.uuid4()
    project = uuid.uuid4()
    foreign = uuid.uuid4()
    budget = uuid.uuid4()
    connection = await asyncpg.connect(MIGRATOR_URL)
    try:
        async with connection.transaction():
            await connection.execute(
                "INSERT INTO cortex_auth.installations "
                "(installation_id, display_name, status) VALUES ($1, $2, 'active')",
                installation,
                f"processing-it-{RUN}",
            )
            for principal_id, name in ((owner, "proc-owner"), (worker, "proc-worker")):
                await connection.execute(
                    "INSERT INTO cortex_auth.principals "
                    "(principal_id, installation_id, principal_name, status) "
                    "VALUES ($1, $2, $3, 'active')",
                    principal_id,
                    installation,
                    f"{name}-{RUN}",
                )
            for principal_id, token in ((owner, OWNER_TOKEN), (worker, WORKER_TOKEN)):
                await connection.execute(
                    "INSERT INTO cortex_auth.credentials "
                    "(credential_id, principal_id, token_hash, generation) "
                    "VALUES ($1, $2, $3, 1)",
                    uuid.uuid4(),
                    principal_id,
                    token_digest(token, PEPPER),
                )
            # Space creation and generation activation require installation
            # owner authority (cortex_auth.require_installation_owner).
            await connection.execute(
                "INSERT INTO cortex_auth.installation_owners "
                "(installation_id, principal_id) VALUES ($1, $2)",
                installation,
                owner,
            )
            scopes = {
                PROJECT_ALIAS: project,
                FOREIGN_ALIAS: foreign,
                BUDGET_ALIAS: budget,
            }
            for alias, scope_id in scopes.items():
                await connection.execute(
                    "INSERT INTO cortex_core.scopes "
                    "(scope_id, scope_kind, display_name, is_active) "
                    "VALUES ($1, 'project', $2, true)",
                    scope_id,
                    alias,
                )
                await connection.execute(
                    "INSERT INTO cortex_core.scope_aliases (alias, scope_id, is_primary) "
                    "VALUES ($1, $2, true)",
                    alias,
                    scope_id,
                )
            grants = [
                (owner, project, True, True, True),
                (owner, foreign, True, True, True),
                (owner, budget, True, True, True),
                (worker, project, True, True, False),
                (worker, budget, True, True, False),
            ]
            for principal_id, scope_id, read, write, publish in grants:
                await connection.execute(
                    "INSERT INTO cortex_auth.scope_grants "
                    "(principal_id, scope_id, can_read, can_write, can_publish) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    principal_id,
                    scope_id,
                    read,
                    write,
                    publish,
                )
            # A tight provider-call budget so admission control is observable.
            await connection.execute(
                "INSERT INTO cortex_processing.budget_policies "
                "(scope_id, resource, capacity, interactive_reserve) "
                "VALUES ($1, 'provider_calls', 2, 1)",
                budget,
            )
            await connection.execute(
                "SELECT set_config('cortex.policy_revision', '0', true)"
            )
            content = await _seed_content(connection, project, owner)
            budget_content = await _seed_content(connection, budget, owner)
            await connection.execute(
                "SELECT set_config('cortex.principal_id', $1, true)", str(owner)
            )
            await connection.execute(
                "SELECT set_config('cortex.read_scope_ids', $1, true)", str(foreign)
            )
            await connection.execute(
                "SELECT set_config('cortex.write_scope_id', $1, true)", str(foreign)
            )
            foreign_job = await _seed_foreign_job(
                connection, foreign, owner, installation
            )
    finally:
        await connection.close()
    FIXTURE.update(
        {
            "installation": installation,
            "owner": owner,
            "worker": worker,
            "project": project,
            "foreign": foreign,
            "budget": budget,
            "foreign_job": foreign_job,
            **content,
            "budget_item": budget_content["budget_item"],
        }
    )


async def _seed_content(
    connection: asyncpg.Connection, scope_id: uuid.UUID, author: uuid.UUID
) -> dict[str, Any]:
    async def item(content_class: str, payload: dict[str, Any], body: str, revisions: int = 1):
        content_id = uuid.uuid4()
        await connection.execute(
            "INSERT INTO cortex_core.content_items "
            "(scope_id, content_id, content_class, created_by_principal) "
            "VALUES ($1, $2, $3, $4)",
            scope_id,
            content_id,
            content_class,
            author,
        )
        for revision in range(1, revisions + 1):
            text = body if revision == 1 else f"{body}\n\nrevision {revision} addition"
            await connection.execute(
                "INSERT INTO cortex_core.content_revisions "
                "(scope_id, content_id, revision, payload, body_text, content_hash, "
                " author_principal_id, supersedes_revision) "
                "VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8)",
                scope_id,
                content_id,
                revision,
                json.dumps(payload, sort_keys=True),
                text,
                hashlib.sha256(f"{content_id}{revision}".encode()).digest(),
                author,
                revision - 1 if revision > 1 else None,
            )
        return content_id

    bodies = {
        "knowledge": (
            "Cortex v2 preserves the original revision before any derived "
            "projection is built, and every projection names its source."
        ),
        "decision": (
            "Decision: the vector column carries no fixed dimension; the space "
            "records the geometry and the row trigger enforces it."
        ),
        "short": "tiny",
        "superseded": (
            "Superseded knowledge body that is long enough to be intended by the "
            "default coverage policy for the core text profile."
        ),
    }
    knowledge = await item("knowledge", {"content": bodies["knowledge"]}, bodies["knowledge"])
    decision = await item("decision", {"statement": bodies["decision"]}, bodies["decision"])
    short = await item("decision", {"statement": bodies["short"]}, bodies["short"])
    superseded = await item(
        "knowledge",
        {"content": bodies["superseded"]},
        bodies["superseded"],
        revisions=2,
    )
    digest = hashlib.sha256(b"%PDF-1.7 not really").hexdigest()
    artifact = await item(
        "artifact",
        {
            "media_type": "application/pdf",
            "bytes_sha256": digest,
            "location": f"blobs/{RUN}/spec.pdf",
            "filename": "spec.pdf",
        },
        "Artifact catalog entry for a PDF whose bytes live in the blob adapter.",
    )
    budget_item = await item(
        "knowledge",
        {"content": bodies["knowledge"]},
        "Budget scope knowledge body long enough for the default coverage policy.",
    )
    return {
        "knowledge": knowledge,
        "decision": decision,
        "short": short,
        "superseded": superseded,
        "artifact": artifact,
        "budget_item": budget_item,
        "bodies": bodies,
        "artifact_sha256": digest,
    }


async def _seed_foreign_job(
    connection: asyncpg.Connection, scope_id: uuid.UUID, owner: uuid.UUID,
    installation: uuid.UUID,
) -> uuid.UUID:
    result = await queue.enqueue_jobs(
        connection,
            context=_context(owner, scope_id, installation),
        intents=(
            queue.JobIntent(
                job_kind="graph.memory.extract",
                payload={"pin": {"repository_key": "helix", "commit_sha": "deadbeef"}},
            ),
        ),
    )
    return result.enqueued[0].job_id


def _context(
    principal_id: uuid.UUID,
    scope_id: uuid.UUID,
    installation: uuid.UUID | None = None,
):
    from cortex_v2.store import Principal, Scope, ScopeContext

    scope = Scope(
        alias="", scope_id=scope_id, kind="project",
        can_read=True, can_write=True, can_publish=True,
    )
    return ScopeContext(
        Principal(principal_id, installation or FIXTURE["installation"]), scope, (scope,)
    )


@pytest.fixture(scope="module", autouse=True)
def seeded() -> dict[str, Any]:
    global _SEEDED
    if not _SEEDED:
        run(_seed())
        _SEEDED = True
    return FIXTURE


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


async def _open(token: str, alias: str, *, write: bool):
    connection = await asyncpg.connect(DATABASE_URL)
    principal = await authenticate(connection, token_digest(token, PEPPER))
    async with connection.transaction():
        context = await resolve_scopes(connection, principal, alias, [alias], write=write)
        yield connection, context


class Session:
    """One connection + transaction with resolved scope context."""

    def __init__(self, token: str, alias: str, *, write: bool = True):
        self.token = token
        self.alias = alias
        self.write = write
        self.connection: asyncpg.Connection | None = None
        self.context = None
        self._transaction = None

    async def __aenter__(self):
        self.connection = await asyncpg.connect(DATABASE_URL)
        self._transaction = self.connection.transaction()
        await self._transaction.start()
        principal = await authenticate(
            self.connection, token_digest(self.token, PEPPER)
        )
        self.context = await resolve_scopes(
            self.connection, principal, self.alias, [self.alias], write=self.write
        )
        return self.connection, self.context

    async def __aexit__(self, exc_type, exc, traceback):
        # Commit on success so later observations see the durable state a real
        # command would leave behind; roll back only when the body raised.
        if exc_type is None:
            await self._transaction.commit()
        else:
            await self._transaction.rollback()
        await self.connection.close()


def in_session(token: str, alias: str, body: Callable, *, write: bool = True) -> Any:
    """Run ``body(connection, context)`` inside one authorized transaction."""

    async def runner():
        async with Session(token, alias, write=write) as (connection, context):
            return await body(connection, context)

    return asyncio.run(runner())


def owner_call(body: Callable, alias: str = PROJECT_ALIAS) -> Any:
    return in_session(OWNER_TOKEN, alias, body)


def as_migrator(body: Callable) -> Any:
    async def runner():
        connection = await asyncpg.connect(MIGRATOR_URL)
        try:
            async with connection.transaction():
                return await body(connection)
        finally:
            await connection.close()

    return asyncio.run(runner())


def raw_app_query(sql: str, *args: Any, principal: str | None = None,
                  read_scopes: str | None = None, write_scope: str | None = None) -> Any:
    """App-role query with explicitly chosen (or deliberately unset) context."""

    async def runner():
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            async with connection.transaction():
                if principal:
                    await connection.execute(
                        "SELECT set_config('cortex.principal_id', $1, true)", principal
                    )
                if read_scopes is not None:
                    await connection.execute(
                        "SELECT set_config('cortex.read_scope_ids', $1, true)", read_scopes
                    )
                if write_scope is not None:
                    await connection.execute(
                        "SELECT set_config('cortex.write_scope_id', $1, true)", write_scope
                    )
                return await connection.fetch(sql, *args)
        finally:
            await connection.close()

    return asyncio.run(runner())


def worker_context(scope_id: uuid.UUID, *, principal: uuid.UUID | None = None):
    return _context(principal or FIXTURE["worker"], scope_id)


# ---------------------------------------------------------------------------
# Worker harness
# ---------------------------------------------------------------------------


def worker_env(role: str, **overrides: str) -> dict[str, str]:
    env = {
        # Name-only secret references: the values are never read here because
        # the DSN and identity resolvers are patched to the candidate database.
        "CORTEX_V2_DATABASE_URL_FILE": "/run/secrets/processing-it-database-url",
        "CORTEX_V2_WORKER_PRINCIPAL_ID_FILE": "/run/secrets/processing-it-principal",
        "CORTEX_V2_WORKER_ROLE": role,
        "CORTEX_V2_WORKER_ID": f"it-{role}-{uuid.uuid4().hex[:8]}",
        "CORTEX_V2_WORKER_EXECUTOR_ROLES": "core" if role != "doc" else "core,doc",
        "CORTEX_V2_WORKER_LEASE_SECONDS": "30",
        "CORTEX_V2_WORKER_HEARTBEAT_INTERVAL_SECONDS": "5",
        "CORTEX_V2_WORKER_POLL_INTERVAL_SECONDS": "0.05",
        "CORTEX_V2_WORKER_ATTEMPT_DEADLINE_SECONDS": "60",
        "CORTEX_V2_WORKER_MAX_CONCURRENT_ATTEMPTS": "2",
        "CORTEX_V2_WORKER_THREAD_POOL_SIZE": "2",
        "CORTEX_V2_WORKER_REAP_INTERVAL_SECONDS": "3600",
        "CORTEX_V2_INFERENCE_ROUTING_POLICY": "remote_allowed",
        "CORTEX_V2_PROVIDER_EMBEDDING_ENABLED": "true",
        "CORTEX_V2_PROVIDER_EMBEDDING_KIND": "hash-local",
        "CORTEX_V2_PROVIDER_EMBEDDING_MODEL": "hash-local-1",
    }
    env.update(overrides)
    return env


@pytest.fixture
def run_worker(monkeypatch) -> Callable[..., worker_module.WorkerStats]:
    """Run the real worker process logic against the candidate database."""
    monkeypatch.setattr(
        worker_module, "resolve_database_url", lambda settings: DATABASE_URL
    )
    monkeypatch.setattr(
        worker_module,
        "resolve_principal_identity",
        lambda settings: (FIXTURE["worker"], FIXTURE["installation"]),
    )

    def call(
        role: str = "embed",
        *,
        max_jobs: int | None = 10,
        registry: JobRegistry | None = None,
        env: dict[str, str] | None = None,
    ) -> worker_module.WorkerStats:
        settings = parse_settings(env or worker_env(role))

        async def runner():
            worker = worker_module.Worker(
                settings=settings,
                role=role,
                worker_id=settings.worker.worker_id,
                registry=registry or JOB_REGISTRY,
            )
            await worker.start()
            return await worker.run(max_jobs=max_jobs)

        return asyncio.run(runner())

    return call


def create_space(
    alias: str = PROJECT_ALIAS,
    *,
    name: str | None = None,
    dimensions: int = SPACE_DIMENSIONS,
    provider: str = "hash-local",
    model_id: str = "hash-local-1",
    metric: str = "cosine",
    normalization: str = "l2",
) -> dict[str, Any]:
    payload = {
        "space_name": name or f"it-space-{uuid.uuid4().hex[:8]}",
        "provider": provider,
        "model_id": model_id,
        "dimensions": dimensions,
        "normalization": normalization,
        "metric": metric,
        "chunking": {
            "policy_id": "chunk.paragraph",
            "version": 1,
            "kind": "structural",
            "target_chars": 1200,
            "max_chars": 2000,
            "overlap_chars": 0,
        },
    }

    async def body(connection, context):
        return await commands.create_embedding_space(
            connection, context, str(uuid.uuid4()), payload, None
        )

    status, receipt, _ = owner_call(body, alias)
    assert status == 201, receipt
    return receipt


def sample_chunks(
    *, scope_id: uuid.UUID, content_id: uuid.UUID, revision: int, space_id: uuid.UUID,
    policy_identity: str, texts: tuple[str, ...]
) -> tuple[Chunk, ...]:
    blocks = tuple(
        ParseBlock(
            block_kind="paragraph",
            text=text,
            span=Span(start=0, end=len(text)),
        )
        for text in texts
    )
    from cortex_v2.processing.chunking import chunk_blocks

    return chunk_blocks(
        blocks,
        ChunkingPolicy(
            policy_id=policy_identity.split("@")[0],
            version=int(policy_identity.split("@")[1]),
            kind="structural",
            target_chars=1200,
            max_chars=2000,
            overlap_chars=0,
        ),
    )


def unit_vector(seed: str, dimensions: int) -> tuple[float, ...]:
    values = [
        ((int(hashlib.sha256(f"{seed}{index}".encode()).hexdigest()[:8], 16) % 2000) - 1000)
        / 1000.0
        for index in range(dimensions)
    ]
    norm = math.sqrt(sum(value * value for value in values))
    return tuple(value / norm for value in values)


# ---------------------------------------------------------------------------
# Schema and contract
# ---------------------------------------------------------------------------


def test_migration_is_recorded_with_the_checksum_of_the_file():
    from pathlib import Path

    sql = Path(__file__).resolve().parents[1] / "migrations" / "0004_processing.sql"
    expected = hashlib.sha256(sql.read_text().encode()).hexdigest()

    async def body(connection):
        return await connection.fetchrow(
            "SELECT checksum_sha256 FROM cortex_core.schema_migrations "
            "WHERE migration_id = '0004_processing.sql'"
        )

    row = as_migrator(body)
    assert row is not None, "0004_processing.sql was not applied"
    assert row["checksum_sha256"] == expected


def test_every_processing_table_forces_row_level_security_and_app_cannot_delete():
    async def body(connection):
        tables = await connection.fetch(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
              FROM pg_class AS c
              JOIN pg_namespace AS n ON n.oid = c.relnamespace
             WHERE n.nspname = 'cortex_processing' AND c.relkind = 'r'
            """
        )
        deletes = await connection.fetch(
            """
            SELECT table_name
              FROM information_schema.role_table_grants
             WHERE grantee = 'cortex_v2_app'
               AND table_schema = 'cortex_processing'
               AND privilege_type = 'DELETE'
            """
        )
        return tables, deletes

    tables, deletes = as_migrator(body)
    assert len(tables) >= 17
    for table in tables:
        assert table["relrowsecurity"], table["relname"]
        assert table["relforcerowsecurity"], table["relname"]
    assert deletes == []


def test_vector_column_has_no_fixed_dimension():
    async def body(connection):
        return await connection.fetchval(
            """
            SELECT a.atttypmod
              FROM pg_attribute AS a
             WHERE a.attrelid = 'cortex_processing.chunk_vectors'::regclass
               AND a.attname = 'embedding'
            """
        )

    assert as_migrator(body) == -1


def test_retrieval_contract_signature_is_exact_and_invoker_rights():
    async def body(connection):
        return await connection.fetchrow(
            """
            SELECT pg_get_function_identity_arguments(p.oid) AS arguments,
                   pg_get_function_result(p.oid) AS result,
                   p.prosecdef AS security_definer,
                   p.provolatile AS volatility,
                   pg_get_functiondef(p.oid) AS definition
              FROM pg_proc AS p
              JOIN pg_namespace AS n ON n.oid = p.pronamespace
             WHERE n.nspname = 'cortex_processing'
               AND p.proname = 'vector_candidates'
            """
        )

    row = as_migrator(body)
    assert row["arguments"] == "p_space_id uuid, p_query vector, p_limit integer"
    assert row["result"] == (
        "TABLE(content_id uuid, revision integer, chunk_id uuid, "
        "distance double precision)"
    )
    # Invoker rights: row level security still filters candidates by read scope.
    assert row["security_definer"] is False
    assert row["volatility"] in ("s", b"s")
    # The compatibility test is the recorded space dimension, never a cast (F07).
    assert "::vector(" not in row["definition"]
    assert "vector(6" not in row["definition"]


def test_outbox_matches_the_feed_source_contract():
    async def body(connection):
        processing = await connection.fetch(
            """
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_schema = 'cortex_processing' AND table_name = 'outbox_events'
             ORDER BY ordinal_position
            """
        )
        coordination = await connection.fetch(
            """
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_schema = 'cortex_coord' AND table_name = 'outbox_events'
             ORDER BY ordinal_position
            """
        )
        return processing, coordination

    processing, coordination = as_migrator(body)
    if not coordination:
        pytest.skip("0003 coordination is not applied in this candidate database")
    assert [(r["column_name"], r["data_type"]) for r in processing] == [
        (r["column_name"], r["data_type"]) for r in coordination
    ]


# ---------------------------------------------------------------------------
# Row level security
# ---------------------------------------------------------------------------


def test_unset_context_sees_no_processing_rows():
    for table in (
        "jobs", "attempts", "chunk_revisions", "chunk_vectors", "distillations",
        "quarantine_ledger", "budget_reservations", "processing_profiles",
        "embedding_spaces",
    ):
        rows = raw_app_query(f"SELECT count(*) AS n FROM cortex_processing.{table}")
        assert rows[0]["n"] == 0, table


def test_rows_are_visible_only_in_their_authorized_read_scope():
    project = str(FIXTURE["project"])
    foreign = str(FIXTURE["foreign"])
    principal = str(FIXTURE["owner"])
    only_project = raw_app_query(
        "SELECT scope_id, job_id FROM cortex_processing.jobs",
        principal=principal,
        read_scopes=project,
    )
    project_only = {str(row["scope_id"]) for row in only_project}
    assert project_only <= {project}
    assert foreign not in project_only
    assert FIXTURE["foreign_job"] not in {row["job_id"] for row in only_project}
    both = raw_app_query(
        "SELECT scope_id, job_id FROM cortex_processing.jobs",
        principal=principal,
        read_scopes=",".join(sorted([project, foreign])),
    )
    both_scopes = {str(row["scope_id"]) for row in both}
    assert both_scopes <= {project, foreign}
    assert foreign in both_scopes
    assert FIXTURE["foreign_job"] in {row["job_id"] for row in both}


def test_enqueue_refuses_a_scope_the_caller_cannot_write():
    async def body(connection, context):
        with pytest.raises(ApiProblem) as excinfo:
            await queue.enqueue_jobs(
                connection,
                context=context,
                intents=(
                    queue.JobIntent(
                        job_kind="graph.memory.extract",
                        scope_id=FIXTURE["foreign"],
                        payload={"pin": {"commit_sha": "abc"}},
                    ),
                ),
            )
        assert excinfo.value.status == 403
        assert excinfo.value.code == "scope_write_denied"

    owner_call(body)


# ---------------------------------------------------------------------------
# Spaces, generations and vector geometry
# ---------------------------------------------------------------------------


def test_space_creation_pins_immutable_semantics_with_an_active_generation():
    receipt = create_space()
    assert receipt["state"] == "committed"
    assert receipt["generation"] == 1
    assert receipt["provider_family_known"] is True

    async def body(connection):
        return await connection.fetchrow(
            "SELECT * FROM cortex_processing.space_state($1)",
            uuid.UUID(receipt["space_id"]),
        )

    state = as_migrator(body)
    assert state["state"] == "active"
    assert state["dimensions"] == SPACE_DIMENSIONS
    assert str(state["active_generation_id"]) == receipt["generation_id"]

    # Space semantics are immutable; only the pointer may move.
    async def mutate(connection):
        await connection.execute(
            "UPDATE cortex_processing.embedding_spaces SET dimensions = 128 "
            "WHERE space_id = $1",
            uuid.UUID(receipt["space_id"]),
        )

    with pytest.raises(asyncpg.PostgresError) as excinfo:
        as_migrator(mutate)
    assert excinfo.value.sqlstate == "55000"


def test_a_space_cannot_have_two_active_generations():
    receipt = create_space()
    space_id = uuid.UUID(receipt["space_id"])

    async def body(connection):
        second = await connection.fetchrow(
            "SELECT generation_id, generation FROM "
            "cortex_processing.begin_index_generation($1, $2, NULL)",
            FIXTURE["owner"],
            space_id,
        )
        assert second["generation"] == 2
        with pytest.raises(asyncpg.PostgresError) as excinfo:
            await connection.execute(
                "UPDATE cortex_processing.index_generations SET state = 'active' "
                "WHERE generation_id = $1",
                second["generation_id"],
            )
        # The partial unique index on (space_id) WHERE state='active' is the
        # authority; the deferred consistency trigger is the backstop.
        assert excinfo.value.sqlstate in {"55000", "23514", "23505"}

    as_migrator(body)


def test_activation_switches_routing_and_retires_the_previous_generation():
    receipt = create_space()
    space_id = uuid.UUID(receipt["space_id"])
    first = uuid.UUID(receipt["generation_id"])

    async def begin(connection):
        row = await connection.fetchrow(
            "SELECT generation_id FROM cortex_processing.begin_index_generation($1, $2, NULL)",
            FIXTURE["owner"],
            space_id,
        )
        await connection.execute(
            "UPDATE cortex_processing.index_generations SET state = 'built' "
            "WHERE generation_id = $1",
            row["generation_id"],
        )
        return row["generation_id"]

    second = as_migrator(begin)

    async def body(connection, context):
        return await commands.activate_generation(
            connection, context, str(uuid.uuid4()), None,
            {"space_id": str(space_id), "generation_id": str(second)},
        )

    status, activated, _ = owner_call(body)
    assert status == 200
    assert activated["state"] == "active"

    async def check(connection):
        rows = await connection.fetch(
            "SELECT generation_id, state FROM cortex_processing.index_generations "
            "WHERE space_id = $1 ORDER BY generation",
            space_id,
        )
        return {str(row["generation_id"]): row["state"] for row in rows}

    states = as_migrator(check)
    assert states[str(first)] == "retired"
    assert states[str(second)] == "active"


def test_vector_rows_enforce_space_geometry_per_row():
    receipt = create_space(dimensions=4)
    space_id = uuid.UUID(receipt["space_id"])
    generation_id = uuid.UUID(receipt["generation_id"])
    content_id = FIXTURE["knowledge"]

    async def body(connection):
        chunk_id = uuid.uuid4()
        await connection.execute(
            """
            INSERT INTO cortex_processing.chunk_revisions
                (scope_id, content_id, revision, space_id, chunk_ordinal, chunk_id,
                 chunking_policy, text_sha256, chunk_text, span)
            VALUES ($1, $2, 1, $3, 0, $4, 'chunk.paragraph@1', $5, 'text',
                    '{"kind":"text_offset","start":0,"end":4}'::jsonb)
            """,
            FIXTURE["project"],
            content_id,
            space_id,
            chunk_id,
            hashlib.sha256(b"text").digest(),
        )

        async def vector(values: str, **overrides: Any) -> str | None:
            row = {
                "chunk_id": chunk_id,
                "generation_id": generation_id,
                "scope_id": FIXTURE["project"],
                "space_id": space_id,
                "content_id": content_id,
                "revision": 1,
                "provider": "hash-local",
                "model_id": "hash-local-1",
                "normalization": "l2",
                "attempt_epoch": 1,
            }
            row.update(overrides)
            try:
                async with connection.transaction():
                    await connection.execute(
                    """
                        INSERT INTO cortex_processing.chunk_vectors
                            (chunk_id, generation_id, scope_id, space_id, content_id,
                             revision, embedding, provider, model_id, normalization,
                             attempt_epoch)
                        VALUES ($1, $2, $3, $4, $5, $6, $7::public.vector, $8, $9, $10, $11)
                        """,
                        row["chunk_id"], row["generation_id"], row["scope_id"],
                        row["space_id"], row["content_id"], row["revision"], values,
                        row["provider"], row["model_id"], row["normalization"],
                        row["attempt_epoch"],
                    )
            except asyncpg.PostgresError as exc:
                return exc.sqlstate
            return None

        results = {
            "correct": await vector("[0.5,0.5,0.5,0.5]"),
            "wrong_dimension": await vector("[0.5,0.5,0.5]"),
            "not_normalized": await vector("[1,1,1,1]"),
            "wrong_provider": await vector(
                "[0.5,0.5,0.5,0.5]", provider="some-other-provider"
            ),
            "wrong_model": await vector("[0.5,0.5,0.5,0.5]", model_id="other-model"),
        }
        assert results["correct"] is None
        assert results["wrong_dimension"] == "23514"
        assert results["not_normalized"] == "23514"
        assert results["wrong_provider"] == "23514"
        assert results["wrong_model"] == "23514"
        # A retired generation accepts no vectors. Retire it the way routing
        # does - by activating a successor - because the pointer and the
        # generation state must stay consistent (deferred constraint trigger).
        second = await connection.fetchval(
            "SELECT generation_id FROM "
            "cortex_processing.begin_index_generation($1, $2, NULL)",
            FIXTURE["owner"], space_id,
        )
        await connection.execute(
            "UPDATE cortex_processing.index_generations SET state = 'built' "
            "WHERE generation_id = $1",
            second,
        )
        await connection.fetchrow(
            "SELECT * FROM cortex_processing.activate_index_generation($1, $2, $3)",
            FIXTURE["owner"], space_id, second,
        )
        assert await vector("[0.5,0.5,0.5,0.5]", chunk_id=uuid.uuid4()) in {
            "55000",
            "23503",
        }

    as_migrator(body)


def test_vector_candidates_serves_the_active_generation_and_validates_inputs():
    receipt = create_space(dimensions=4)
    space_id = uuid.UUID(receipt["space_id"])
    active = uuid.UUID(receipt["generation_id"])
    content_id = FIXTURE["knowledge"]

    async def body(connection):
        building = await connection.fetchval(
            "SELECT generation_id FROM cortex_processing.begin_index_generation($1, $2, NULL)",
            FIXTURE["owner"],
            space_id,
        )
        for ordinal, (generation, values) in enumerate(
            ((active, "[1,0,0,0]"), (active, "[0,1,0,0]"), (building, "[0,0,0,1]"))
        ):
            chunk_id = uuid.uuid4()
            await connection.execute(
                """
                INSERT INTO cortex_processing.chunk_revisions
                    (scope_id, content_id, revision, space_id, chunk_ordinal,
                     chunk_id, chunking_policy, text_sha256, chunk_text, span)
                VALUES ($1, $2, 1, $3, $4, $5, 'chunk.paragraph@1', $6, $7,
                        '{"kind":"text_offset","start":0,"end":1}'::jsonb)
                """,
                FIXTURE["project"], content_id, space_id, ordinal, chunk_id,
                hashlib.sha256(str(ordinal).encode()).digest(), f"chunk {ordinal}",
            )
            await connection.execute(
                """
                INSERT INTO cortex_processing.chunk_vectors
                    (chunk_id, generation_id, scope_id, space_id, content_id,
                     revision, embedding, provider, model_id, normalization,
                     attempt_epoch)
                VALUES ($1, $2, $3, $4, $5, 1, $6::public.vector, 'hash-local',
                        'hash-local-1', 'l2', 1)
                """,
                chunk_id, generation, FIXTURE["project"], space_id, content_id, values,
            )
        return building

    as_migrator(body)

    async def query(connection, context):
        return await repository.vector_candidates(
            connection, space_id=space_id, query=(1.0, 0.0, 0.0, 0.0), limit=10
        )

    candidates = owner_call(query)
    assert len(candidates) == 2, "only the active generation may be returned"
    assert candidates[0]["distance"] <= candidates[1]["distance"]
    assert set(candidates[0]) == {"content_id", "revision", "chunk_id", "distance"}
    assert candidates[0]["distance"] == pytest.approx(0.0)

    async def bad_limit(connection, context):
        async def rejected(target, query, expected):
            # A savepoint per probe, with the exception propagating out of it so
            # the rollback happens before the assert: otherwise the aborted
            # subtransaction surfaces as 25P02 and masks the real SQLSTATE.
            with pytest.raises(asyncpg.PostgresError) as excinfo:
                async with connection.transaction():
                    await connection.fetch(query, *target)
            assert excinfo.value.sqlstate == expected

        sql = "SELECT * FROM cortex_processing.vector_candidates($1, $2::public.vector, $3)"
        await rejected((space_id, "[1,0,0,0]", 0), sql, "22023")
        await rejected((space_id, "[1,0,0,0]", 1001), sql, "22023")
        await rejected((space_id, "[1,0,0]", 10), sql, "23514")
        await rejected((uuid.uuid4(), "[1,0,0,0]", 10), sql, "22023")

    owner_call(bad_limit)

    # Without a read scope the same call returns nothing: RLS is the boundary.
    rows = raw_app_query(
        "SELECT * FROM cortex_processing.vector_candidates($1, $2::public.vector, $3)",
        space_id, "[1,0,0,0]", 10,
        principal=str(FIXTURE["owner"]),
        read_scopes="",
    )
    assert rows == []


# ---------------------------------------------------------------------------
# Durable queue semantics
# ---------------------------------------------------------------------------


def enqueue_intent(
    alias: str = PROJECT_ALIAS,
    *,
    job_kind: str = "embed.chunks",
    content_id: uuid.UUID | None = None,
    revision: int = 1,
    profile_id: uuid.UUID | None = PROFILE_TEXT,
    space_id: uuid.UUID | None = None,
    generation_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    required_role: str | None = None,
    budget: dict[str, int] | None = None,
) -> queue.EnqueueResult:
    if job_kind == "embed.chunks" and space_id is None:
        # Queue-mechanics tests need a pinned space/generation but do not care
        # which; one shared space keeps them cheap and deterministic.
        shared = _default_space()
        space_id = uuid.UUID(shared["space_id"])
        generation_id = uuid.UUID(shared["generation_id"])
    graph = job_kind.startswith("graph.")
    intent = queue.JobIntent(
        job_kind=job_kind,
        content_id=None if graph else (
            content_id if content_id is not None else FIXTURE["knowledge"]
        ),
        source_revision=None if graph else revision,
        profile_id=None if graph else profile_id,
        space_id=space_id,
        generation_id=generation_id,
        payload=(
            payload if payload is not None
            else {"pin": {"commit_sha": "abc"}} if graph
            else {}
        ),
        dedupe_key=dedupe_key,
        required_role=required_role,
        budget=budget,
        # High priority so a claim-targeted test gets its own job rather than a
        # leftover from another test in the same scope.
        priority=1000,
    )
    if job_kind == "doc.extract" and space_id is None:
        raise AssertionError("doc.extract tests must pin a space")

    async def body(connection, context):
        return await queue.enqueue_jobs(connection, context=context, intents=(intent,))

    result = owner_call(body, alias)
    global LAST_JOB
    if result.enqueued:
        LAST_JOB = result.enqueued[0].job_id
    return result


def test_enqueue_is_durable_and_identical_intents_collapse():
    receipt = create_space()
    space_id = uuid.UUID(receipt["space_id"])
    generation_id = uuid.UUID(receipt["generation_id"])
    dedupe = f"it-dedupe-{uuid.uuid4().hex[:12]}"
    first = enqueue_intent(
        space_id=space_id, generation_id=generation_id, dedupe_key=dedupe
    )
    assert first.accepted == 1
    job_id = first.enqueued[0].job_id

    second = enqueue_intent(
        space_id=space_id, generation_id=generation_id, dedupe_key=dedupe
    )
    assert second.accepted == 0
    assert second.enqueued[0].job_id == job_id
    assert second.enqueued[0].created is False

    async def body(connection):
        return await connection.fetchrow(
            "SELECT status, fencing_epoch, attempt_count, required_role, intent "
            "FROM cortex_processing.jobs WHERE job_id = $1",
            job_id,
        )

    row = as_migrator(body)
    assert row["status"] == "queued"
    assert row["fencing_epoch"] == 0
    assert row["attempt_count"] == 0
    assert row["required_role"] == "core"
    assert json.loads(row["intent"])["schema_version"] == 1


def test_claim_advances_the_epoch_and_a_stale_attempt_cannot_publish():
    receipt = create_space()
    space_id = uuid.UUID(receipt["space_id"])
    generation_id = uuid.UUID(receipt["generation_id"])
    result = enqueue_intent(
        space_id=space_id, generation_id=generation_id,
        dedupe_key=f"it-fence-{uuid.uuid4().hex[:12]}",
    )
    job_id = result.enqueued[0].job_id

    first = claim_one()
    assert first.job is not None and first.job.job_id == job_id
    assert first.job.fencing_epoch == 1

    # The lease expires (worker killed); a second worker reclaims the work.
    async def expire(connection):
        await connection.execute(
            "UPDATE cortex_processing.jobs SET lease_expires_at = now() - interval '1 second' "
            "WHERE job_id = $1",
            job_id,
        )

    as_migrator(expire)
    second = claim_one(worker_id="it-claimer-2")
    assert second.job is not None and second.job.job_id == job_id
    assert second.job.fencing_epoch == 2

    stale_report = ExecutionReport(
        outcome=Outcome.OK,
        chunks=sample_chunks(
            scope_id=FIXTURE["project"], content_id=FIXTURE["knowledge"], revision=1,
            space_id=space_id, policy_identity="chunk.paragraph@1",
            texts=("stale attempt text that must never be published",),
        ),
        vectors=(unit_vector("stale", SPACE_DIMENSIONS),),
        vector_chunk_ids=(
            keys.chunk_key(
                scope_id=FIXTURE["project"], content_id=FIXTURE["knowledge"], revision=1,
                space_id=space_id, ordinal=0, policy_identity="chunk.paragraph@1",
            ),
        ),
    )
    stale = finish_attempt(first.job, stale_report, publisher=handlers.publish_vectors)
    assert stale.published is False
    assert stale.fenced_reason in {"epoch_superseded", "lease_expired", "lease_taken"}

    async def counts(connection):
        return await connection.fetchrow(
            "SELECT (SELECT count(*) FROM cortex_processing.chunk_vectors "
            "        WHERE job_id = $1) AS vectors, "
            "(SELECT count(*) FROM cortex_processing.chunk_revisions WHERE job_id = $1) AS chunks",
            job_id,
        )

    row = as_migrator(counts)
    assert row["vectors"] == 0
    assert row["chunks"] == 0

    live = finish_attempt(second.job, stale_report, publisher=handlers.publish_vectors)
    assert live.published is True
    assert live.job_status == "succeeded"
    row = as_migrator(counts)
    assert row["vectors"] == 1
    assert row["chunks"] == 1


LAST_JOB: uuid.UUID | None = None


def _release_claim(claim: queue.ClaimedJob) -> None:
    """Put an unintentionally claimed job back, parked an hour ahead."""

    async def runner():
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('cortex.principal_id', $1, true)",
                    str(FIXTURE["worker"]),
                )
                await connection.execute(
                    "SELECT set_config('cortex.read_scope_ids', $1, true)",
                    str(claim.scope_id),
                )
                await connection.execute(
                    "SELECT set_config('cortex.write_scope_id', $1, true)",
                    str(claim.scope_id),
                )
                await connection.execute(
                    "UPDATE cortex_processing.jobs SET status = 'queued', "
                    "lease_owner = NULL, lease_expires_at = NULL, "
                    "available_at = now() + interval '1 hour', "
                    "error_code = NULL, error_message = NULL "
                    "WHERE job_id = $1 AND fencing_epoch = $2",
                    claim.job_id,
                    claim.fencing_epoch,
                )
                await queue.release_reservation(
                    connection, job_id=claim.job_id, resource=queue.BUDGET_CONCURRENCY
                )
        finally:
            await connection.close()

    asyncio.run(runner())


def claim_one(
    *,
    want_job_id: uuid.UUID | None = None,
    worker_id: str = "it-claimer-1",
    role: str = "embed",
    kinds: tuple[str, ...] = ("embed.chunks", "doc.extract"),
    executor_roles: tuple[str, ...] = ("core", "doc"),
    alias: str = PROJECT_ALIAS,
    lease_seconds: int = 30,
) -> queue.ClaimOutcome:
    """Claim the intended job, releasing anything else this pass picks up."""
    target = want_job_id if want_job_id is not None else LAST_JOB
    outcome: queue.ClaimOutcome | None = None
    for _ in range(40):
        outcome = _claim_once_raw(
            worker_id=worker_id,
            role=role,
            kinds=kinds,
            executor_roles=executor_roles,
            alias=alias,
            lease_seconds=lease_seconds,
        )
        if outcome.job is None:
            return outcome
        if target is None or outcome.job.job_id == target:
            return outcome
        _release_claim(outcome.job)
    raise AssertionError(f"never claimed the intended job {target}")


def _claim_once_raw(*, worker_id: str = "it-claimer-1", role: str = "embed",
              kinds: tuple[str, ...] = ("embed.chunks", "doc.extract"),
              executor_roles: tuple[str, ...] = ("core", "doc"),
              alias: str = PROJECT_ALIAS, lease_seconds: int = 30) -> queue.ClaimOutcome:
    async def runner():
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            async with connection.transaction():
                principal = await authenticate(
                    connection, token_digest(WORKER_TOKEN, PEPPER)
                )
                context = await resolve_scopes(
                    connection, principal, alias, [alias], write=True
                )
                return await queue.claim_job(
                    connection,
                    worker_id=worker_id,
                    worker_role=role,
                    executor_roles=executor_roles,
                    job_kinds=kinds,
                    writable_scopes=[context.selected.scope_id],
                    lease_seconds=lease_seconds,
                    principal_id=principal.principal_id,
                )
        finally:
            await connection.close()

    return asyncio.run(runner())


def finish_attempt(
    claim: queue.ClaimedJob,
    report: ExecutionReport,
    *,
    publisher: Callable | None = None,
    verify_source: bool = True,
) -> queue.AttemptDecision:
    async def runner():
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('cortex.principal_id', $1, true)",
                    str(FIXTURE["worker"]),
                )
                await connection.execute(
                    "SELECT set_config('cortex.read_scope_ids', $1, true)",
                    str(claim.scope_id),
                )
                await connection.execute(
                    "SELECT set_config('cortex.write_scope_id', $1, true)",
                    str(claim.scope_id),
                )

                async def publish(inner: asyncpg.Connection) -> None:
                    if verify_source:
                        reason = await repository.verify_source_current(
                            inner,
                            scope_id=claim.scope_id,
                            content_id=claim.content_id,
                            revision=claim.source_revision,
                        )
                        if reason is not None:
                            raise queue.SourceMoved(reason)
                    if publisher is not None:
                        await publisher(inner, _context_of(claim), report)

                return await queue.finish_attempt(
                    connection, claim=claim, report=report, publish=publish
                )
        finally:
            await connection.close()

    return asyncio.run(runner())


def _context_of(claim: queue.ClaimedJob) -> AttemptContext:
    return AttemptContext(
        job_id=claim.job_id,
        job_kind=claim.job_kind,
        scope_id=claim.scope_id,
        installation_id=claim.installation_id,
        principal_id=FIXTURE["worker"],
        fencing_epoch=claim.fencing_epoch,
        lease_owner=claim.lease_owner,
        attempt_id=claim.attempt_id,
        role="embed",
        intent=claim.intent,
        deadline=0.0,
        attempt_number=claim.attempt_number,
        content_id=claim.content_id,
        source_revision=claim.source_revision,
        profile_id=claim.profile_id,
        space_id=claim.space_id,
        generation_id=claim.generation_id,
    )


def test_heartbeat_renews_the_lease_and_reports_cancellation():
    result = enqueue_intent(dedupe_key=f"it-hb-{uuid.uuid4().hex[:12]}",
                            space_id=None, generation_id=None,
                            job_kind="transform.distill", profile_id=PROFILE_DISTILL,
                            payload={"transform": {"kind": "distill", "name": "distill.extractive"}})
    claim = claim_one(kinds=("transform.distill",)).job
    assert claim is not None

    async def renew():
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('cortex.principal_id', $1, true)",
                    str(FIXTURE["worker"]),
                )
                await connection.execute(
                    "SELECT set_config('cortex.read_scope_ids', $1, true)", str(claim.scope_id)
                )
                await connection.execute(
                    "SELECT set_config('cortex.write_scope_id', $1, true)", str(claim.scope_id)
                )
                state = await queue.renew_lease(connection, claim=claim, lease_seconds=120)
                expires = await connection.fetchval(
                    "SELECT lease_expires_at FROM cortex_processing.jobs WHERE job_id = $1",
                    claim.job_id,
                )
                await connection.execute(
                    "UPDATE cortex_processing.jobs SET cancel_requested = true "
                    "WHERE job_id = $1",
                    claim.job_id,
                )
                cancelled = await queue.renew_lease(
                    connection, claim=claim, lease_seconds=120
                )
                return state, expires, cancelled
        finally:
            await connection.close()

    state, expires, cancelled = asyncio.run(renew())
    assert state == queue.LeaseState.LIVE
    assert expires is not None
    assert cancelled == queue.LeaseState.CANCELLED
    assert result.accepted == 1


def test_cancellation_of_a_leased_job_fences_its_publish():
    enqueue_intent(dedupe_key=f"it-cancel-{uuid.uuid4().hex[:12]}")
    claim = claim_one().job
    assert claim is not None

    async def body(connection, context):
        return await queue.cancel_job(
            connection, scope_id=context.selected.scope_id, job_id=claim.job_id,
            reason="operator stopped the backfill",
        )

    result = owner_call(body)
    assert result["state"] == "cancellation_requested"
    assert result["status"] == "leased"

    decision = finish_attempt(claim, ExecutionReport(outcome=Outcome.OK))
    assert decision.published is False
    assert decision.fenced_reason == "cancel_requested"

    async def finalize():
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('cortex.principal_id', $1, true)",
                    str(FIXTURE["worker"]),
                )
                await connection.execute(
                    "SELECT set_config('cortex.read_scope_ids', $1, true)", str(claim.scope_id)
                )
                await connection.execute(
                    "SELECT set_config('cortex.write_scope_id', $1, true)", str(claim.scope_id)
                )
                return await queue.finalize_cancelled(
                    connection, claim=claim, reason="cancellation observed"
                )
        finally:
            await connection.close()

    finalized = asyncio.run(finalize())
    assert finalized.job_status == "cancelled"

    async def check(connection):
        return await connection.fetchrow(
            "SELECT status, cancel_requested, error_code, lease_owner "
            "FROM cortex_processing.jobs WHERE job_id = $1",
            claim.job_id,
        )

    row = as_migrator(check)
    assert row["status"] == "cancelled"
    assert row["cancel_requested"] is True
    assert row["lease_owner"] is None


def test_cancelling_a_terminal_job_is_a_typed_conflict():
    result = enqueue_intent(dedupe_key=f"it-terminal-{uuid.uuid4().hex[:12]}")
    job_id = result.enqueued[0].job_id
    claim = claim_one().job
    finish_attempt(claim, ExecutionReport(outcome=Outcome.OK))

    async def body(connection, context):
        with pytest.raises(ApiProblem) as excinfo:
            await queue.cancel_job(
                connection, scope_id=context.selected.scope_id, job_id=job_id
            )
        assert excinfo.value.status == 409
        assert excinfo.value.code == "job_terminal"

    owner_call(body)


def test_a_superseded_source_refuses_to_publish_and_cancels_the_job():
    result = enqueue_intent(
        content_id=FIXTURE["superseded"], revision=1,
        dedupe_key=f"it-superseded-{uuid.uuid4().hex[:12]}",
    )
    claim = claim_one().job
    assert claim is not None

    # Revision 2 already exists from seeding, so the pinned revision 1 is stale.
    async def body(connection):
        return await repository.verify_source_current(
            connection,
            scope_id=FIXTURE["project"],
            content_id=FIXTURE["superseded"],
            revision=1,
        )

    assert as_migrator(body) == "source_superseded"

    async def finalize():
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('cortex.principal_id', $1, true)",
                    str(FIXTURE["worker"]),
                )
                await connection.execute(
                    "SELECT set_config('cortex.read_scope_ids', $1, true)", str(claim.scope_id)
                )
                await connection.execute(
                    "SELECT set_config('cortex.write_scope_id', $1, true)", str(claim.scope_id)
                )
                return await queue.finalize_source_moved(
                    connection, claim=claim, reason="source_superseded"
                )
        finally:
            await connection.close()

    decision = asyncio.run(finalize())
    assert decision.published is True
    assert decision.job_status == "cancelled"
    assert decision.outcome_code == "source_superseded"
    assert result.accepted == 1


def test_transient_failure_requeues_with_backoff_and_permanent_failure_quarantines():
    enqueue_intent(dedupe_key=f"it-retry-{uuid.uuid4().hex[:12]}")
    claim = claim_one().job
    decision = finish_attempt(
        claim,
        ExecutionReport(
            outcome=Outcome.PROVIDER_TIMEOUT, detail="provider timed out",
            retry_after_seconds=None,
        ),
    )
    assert decision.job_status == "queued"
    assert decision.retry_after_seconds and decision.retry_after_seconds > 0

    async def check(connection):
        job = await connection.fetchrow(
            "SELECT status, error_code, error_retryable, attempt_count, available_at, "
            "created_at FROM cortex_processing.jobs WHERE job_id = $1",
            claim.job_id,
        )
        reservations = await connection.fetch(
            "SELECT resource, released_at FROM cortex_processing.budget_reservations "
            "WHERE job_id = $1",
            claim.job_id,
        )
        return job, reservations

    job, reservations = as_migrator(check)
    assert job["status"] == "queued"
    assert job["error_code"] == "provider_timeout"
    assert job["error_retryable"] is True
    assert job["available_at"] > job["created_at"]
    released = {row["resource"]: row["released_at"] for row in reservations}
    assert released["concurrency"] is not None, "the in-flight slot is freed on retry"
    assert released["provider_calls"] is None, "admission stays reserved until terminal"

    # Now force the lease to be claimable and drive it to a permanent failure.
    async def ready(connection):
        await connection.execute(
            "UPDATE cortex_processing.jobs SET available_at = now() WHERE job_id = $1",
            claim.job_id,
        )

    as_migrator(ready)
    second = claim_one().job
    assert second is not None
    quarantined = finish_attempt(
        second,
        ExecutionReport(
            outcome=Outcome.INPUT_CORRUPT, detail="container is damaged",
            format_id="pdf", executor="parser.pdf@1",
        ),
    )
    assert quarantined.job_status == "quarantined"

    async def ledger(connection):
        rows = await connection.fetch(
            "SELECT reason_code, reason_detail, preserved_payload, payload_sha256, "
            "disposition FROM cortex_processing.quarantine_ledger WHERE job_id = $1",
            claim.job_id,
        )
        job = await connection.fetchrow(
            "SELECT status, error_code FROM cortex_processing.jobs WHERE job_id = $1",
            claim.job_id,
        )
        released = await connection.fetch(
            "SELECT resource, released_at FROM cortex_processing.budget_reservations "
            "WHERE job_id = $1",
            claim.job_id,
        )
        return rows, job, released

    rows, job, released = as_migrator(ledger)
    assert job["status"] == "quarantined"
    assert job["error_code"] == "input_corrupt"
    assert len(rows) == 1
    entry = rows[0]
    assert entry["reason_code"] == "input_corrupt"
    assert entry["disposition"] == "review"
    preserved = json.loads(entry["preserved_payload"])
    assert preserved["job_kind"] == "embed.chunks"
    assert preserved["intent"]["schema_version"] == 1
    assert entry["payload_sha256"] == hashlib.sha256(
        json.dumps(preserved, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode()
    ).digest()
    assert all(row["released_at"] is not None for row in released)


def test_budget_admission_defers_work_and_protects_the_interactive_reserve():
    alias = BUDGET_ALIAS
    scope_id = FIXTURE["budget"]
    space = create_space(alias=alias)
    space_id = uuid.UUID(space["space_id"])
    generation_id = uuid.UUID(space["generation_id"])
    intents = [
        queue.JobIntent(
            job_kind="embed.chunks",
            scope_id=scope_id,
            content_id=FIXTURE["budget_item"],
            source_revision=1,
            profile_id=PROFILE_TEXT,
            space_id=space_id,
            generation_id=generation_id,
            dedupe_key=f"it-budget-{index}-{uuid.uuid4().hex[:8]}",
        )
        for index in range(4)
    ]

    async def body(connection, context):
        return await queue.enqueue_jobs(connection, context=context, intents=intents)

    result = in_session(OWNER_TOKEN, alias, body)
    # capacity 2, interactive_reserve 1 -> bulk ceiling is 1: the reserve is
    # held back for interactive retrieval so a backfill cannot starve it.
    assert result.accepted == 1
    assert len(result.deferred) == 3
    assert {item.reason for item in result.deferred} == {"budget_exhausted"}
    assert all("1 of 1 in use" in item.detail or "budget exhausted" in item.detail
               for item in result.deferred)

    # Interactive work may use the reserved part of the same capacity.
    async def interactive(connection, context):
        return await queue.enqueue_jobs(
            connection,
            context=context,
            intents=(
                queue.JobIntent(
                    job_kind="embed.chunks",
                    scope_id=scope_id,
                    content_id=FIXTURE["budget_item"],
                    source_revision=1,
                    profile_id=PROFILE_TEXT,
                    space_id=uuid.UUID(space["space_id"]),
                    generation_id=uuid.UUID(space["generation_id"]),
                    is_interactive=True,
                    budget={"provider_calls": 1},
                    dedupe_key=f"it-interactive-{uuid.uuid4().hex[:8]}",
                ),
            ),
        )

    interactive_result = in_session(OWNER_TOKEN, alias, interactive)
    assert interactive_result.accepted == 1


def test_reap_makes_abandoned_work_visible_and_releases_reservations():
    result = enqueue_intent(dedupe_key=f"it-reap-{uuid.uuid4().hex[:12]}")
    job_id = result.enqueued[0].job_id
    claim_one()

    async def expire(connection):
        await connection.execute(
            "UPDATE cortex_processing.jobs SET lease_expires_at = now() - interval '1 hour', "
            "attempt_count = max_attempts, cancel_requested = true WHERE job_id = $1",
            job_id,
        )

    as_migrator(expire)

    async def reap():
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('cortex.principal_id', $1, true)",
                    str(FIXTURE["worker"]),
                )
                await connection.execute(
                    "SELECT set_config('cortex.read_scope_ids', $1, true)",
                    str(FIXTURE["project"]),
                )
                await connection.execute(
                    "SELECT set_config('cortex.write_scope_id', $1, true)",
                    str(FIXTURE["project"]),
                )
                return await queue.reap_jobs(
                    connection, scope_id=FIXTURE["project"], limit=50
                )
        finally:
            await connection.close()

    reaped = asyncio.run(reap())
    assert reaped["cancelled"] >= 1
    assert reaped["reservations_released"] >= 1

    async def check(connection):
        return await connection.fetchrow(
            "SELECT status, error_code FROM cortex_processing.jobs WHERE job_id = $1",
            job_id,
        )

    assert as_migrator(check)["status"] == "cancelled"


# ---------------------------------------------------------------------------
# End-to-end pipeline through the real worker
# ---------------------------------------------------------------------------


def test_backfill_then_worker_publishes_chunks_vectors_and_coverage():
    receipt = create_space()
    space_id = uuid.UUID(receipt["space_id"])
    generation_id = uuid.UUID(receipt["generation_id"])

    async def body(connection, context):
        return await commands.backfill_jobs(
            connection,
            context,
            f"it-backfill-{uuid.uuid4().hex[:8]}",
            {
                "profile_id": str(PROFILE_TEXT),
                "space_id": str(space_id),
                "generation": "active",
                "stage": "embed.chunks",
                "limit": 20,
            },
            None,
        )

    status, data, replayed = owner_call(body)
    assert status == 202
    assert replayed is False
    assert data["accepted"] >= 2, data
    assert data["generation_id"] == str(generation_id)
    assert data["deferred"] == []

    stats = run_worker_with_space(role="embed")
    assert stats.claimed >= 2
    assert stats.failed == 0 and stats.quarantined == 0 and stats.blocked == 0

    async def effects(connection, context):
        chunks = await connection.fetchval(
            "SELECT count(*) FROM cortex_processing.chunk_revisions WHERE space_id = $1",
            space_id,
        )
        vectors = await connection.fetchval(
            "SELECT count(*) FROM cortex_processing.chunk_vectors WHERE generation_id = $1",
            generation_id,
        )
        dimensions = await connection.fetch(
            "SELECT DISTINCT public.vector_dims(embedding) AS dims "
            "FROM cortex_processing.chunk_vectors WHERE generation_id = $1",
            generation_id,
        )
        coverage = await commands.coverage_report(
            connection,
            context,
            {
                "profile_id": str(PROFILE_TEXT),
                "space_id": str(space_id),
                "generation_id": str(generation_id),
            },
            None,
        )
        jobs = await queue.list_jobs(
            connection, scope_ids=[context.selected.scope_id], limit=100
        )
        return chunks, vectors, dimensions, coverage, jobs

    def call(connection, context):
        return effects(connection, context)

    chunks, vectors, dimensions, coverage, jobs = owner_call(call)
    assert chunks >= 2
    assert vectors == chunks
    assert [row["dims"] for row in dimensions] == [SPACE_DIMENSIONS]
    assert coverage["embedded_revisions"] >= 2
    assert coverage["gap"]["revisions"] == 0, coverage["gap"]
    assert not [
        item
        for item in coverage["degraded"]
        if item.startswith(("missing_", "empty_sources", "no_active_generation"))
    ], coverage["degraded"]
    assert coverage["coverage_ratio"] == 1.0
    statuses = {job["job_id"]: job["status"] for job in jobs[0]}
    assert set(statuses.values()) >= {"succeeded"}

    # Retrieval sees the published vectors through the contract function.
    from cortex_v2.processing.embedding import HashEmbeddingProvider
    from cortex_v2.processing.contracts import SpaceContract

    provider = HashEmbeddingProvider()
    contract = SpaceContract(
        space_id=space_id, space_name="it", provider="hash-local",
        model_id="hash-local-1", model_revision=None, dimensions=SPACE_DIMENSIONS,
        normalization="l2", metric="cosine",
    )
    outcome = run(provider.embed_query(contract, "preserved original revision", deadline=0.0))
    assert outcome.ok

    async def candidates(connection, context):
        return await repository.vector_candidates(
            connection, space_id=space_id, query=outcome.vectors[0], limit=5
        )

    hits = owner_call(candidates)
    assert hits, "the active generation must be retrievable"
    assert all(hit["revision"] >= 1 for hit in hits)
    assert hits[0]["distance"] <= hits[-1]["distance"]


def run_worker_with_space(role: str = "embed", **kwargs: Any):
    """Run the worker with the module-scoped monkeypatches applied manually."""
    original_url = worker_module.resolve_database_url
    original_identity = worker_module.resolve_principal_identity
    worker_module.resolve_database_url = lambda settings: DATABASE_URL
    worker_module.resolve_principal_identity = lambda settings: (
        FIXTURE["worker"], FIXTURE["installation"]
    )
    try:
        settings = parse_settings(worker_env(role))

        async def runner():
            worker = worker_module.Worker(
                settings=settings,
                role=role,
                worker_id=settings.worker.worker_id,
                registry=kwargs.pop("registry", JOB_REGISTRY),
                register_builtins=kwargs.pop("register_builtins", True),
            )
            await worker.start()
            return await worker.run(max_jobs=kwargs.pop("max_jobs", 25))

        return asyncio.run(runner())
    finally:
        worker_module.resolve_database_url = original_url
        worker_module.resolve_principal_identity = original_identity


def test_backfill_is_idempotent_by_key_and_resumable_without_duplicates():
    receipt = create_space()
    space_id = uuid.UUID(receipt["space_id"])
    generation_id = uuid.UUID(receipt["generation_id"])
    key = f"it-idem-{uuid.uuid4().hex[:8]}"
    payload = {
        "profile_id": str(PROFILE_TEXT),
        "space_id": str(space_id),
        "generation": "active",
        "stage": "embed.chunks",
        "limit": 20,
    }

    async def body(connection, context):
        return await commands.backfill_jobs(connection, context, key, payload, None)

    first_status, first, _ = owner_call(body)
    second_status, second, second_replayed = owner_call(body)
    assert first_status == 202
    assert second_status == 200
    assert second_replayed is True
    assert second == first

    # A new key over the same window collapses onto the existing durable jobs.
    async def resume(connection, context):
        return await commands.backfill_jobs(
            connection, context, f"it-idem2-{uuid.uuid4().hex[:8]}", payload, None
        )

    _, resumed, _ = owner_call(resume)
    assert resumed["accepted"] == 0
    assert resumed["already_durable"] == first["accepted"]

    run_worker_with_space(role="embed")
    run_worker_with_space(role="embed")

    async def counts(connection):
        return await connection.fetchrow(
            "SELECT (SELECT count(*) FROM cortex_processing.chunk_revisions "
            " WHERE space_id = $1) AS chunks, "
            "(SELECT count(*) FROM cortex_processing.chunk_vectors "
            " WHERE generation_id = $2) AS vectors",
            space_id,
            generation_id,
        )

    row = as_migrator(counts)
    assert row["chunks"] >= 2
    assert row["vectors"] == row["chunks"], "re-running must not duplicate vectors"


def test_doc_format_work_waits_visibly_for_the_document_processor():
    receipt = create_space()
    space_id = uuid.UUID(receipt["space_id"])

    async def body(connection, context):
        return await commands.backfill_jobs(
            connection,
            context,
            f"it-doc-{uuid.uuid4().hex[:8]}",
            {
                "profile_id": str(PROFILE_DOC_AUTO),
                "space_id": str(space_id),
                "generation": "active",
                "stage": "auto",
                "limit": 20,
            },
            None,
        )

    _, data, _ = owner_call(body)
    assert data["accepted"] >= 1

    async def queued_doc_jobs(connection):
        return await connection.fetch(
            "SELECT job_id, job_kind, required_role, status FROM cortex_processing.jobs "
            "WHERE scope_id = $1 AND required_role = 'doc'",
            FIXTURE["project"],
        )

    doc_jobs = as_migrator(queued_doc_jobs)
    assert doc_jobs, "the PDF artifact must be routed to the doc executor"
    assert all(row["job_kind"] == "doc.extract" for row in doc_jobs)

    # An embed-role worker must not claim doc work, and the gap stays visible.
    stats = run_worker_with_space(role="embed", max_jobs=25)
    async def still_queued(connection):
        return await connection.fetch(
            "SELECT status FROM cortex_processing.jobs WHERE job_id = ANY($1::uuid[])",
            [row["job_id"] for row in doc_jobs],
        )

    assert all(row["status"] == "queued" for row in as_migrator(still_queued))

    async def summary(connection, context):
        return await queue.queue_summary(connection, scope_id=context.selected.scope_id)

    summary = owner_call(summary)
    assert summary["waiting_for_executor"].get("doc", 0) >= 1
    assert "doc" not in summary["live_executor_roles"]

    async def formats(connection, context):
        return await commands.doc_formats(connection, context, None, None)

    discovery = owner_call(formats)
    assert "pdf" in discovery["formats_waiting_for_executor"]
    assert "text" in discovery["formats_available_now"] or not discovery[
        "live_executor_roles"
    ]
    assert any(
        entry["format_id"] == "pdf" and entry["dependencies"] == ["pypdf"]
        for entry in discovery["formats"]
    )
    assert stats.claimed >= 0


def test_doc_role_worker_claims_doc_work_and_reports_a_typed_blocked_outcome():
    receipt = create_space()
    space_id = uuid.UUID(receipt["space_id"])

    async def body(connection, context):
        return await commands.backfill_jobs(
            connection, context, f"it-docworker-{uuid.uuid4().hex[:8]}",
            {
                "profile_id": str(PROFILE_DOC_AUTO),
                "space_id": str(space_id),
                "generation": "active",
                "stage": "doc.extract",
                "limit": 20,
            },
            None,
        )

    owner_call(body)
    stats = run_worker_with_space(role="doc", max_jobs=25)
    assert stats.claimed >= 1

    async def blocked(connection):
        return await connection.fetch(
            "SELECT status, error_code, error_message FROM cortex_processing.jobs "
            "WHERE scope_id = $1 AND job_kind = 'doc.extract' "
            "ORDER BY created_at DESC LIMIT 5",
            FIXTURE["project"],
        )

    rows = as_migrator(blocked)
    codes = {row["error_code"] for row in rows}
    # Which typed outcome appears depends on the image and the blob adapter, and
    # every branch is honest, visible and resumable:
    #   executor_not_activated   the doc dependencies are not in this image
    #   source_bytes_unavailable deps present, but no blob resolver is configured
    #                            for the referenced original bytes
    #   unsupported_format       the format has no executor at all
    #   input_corrupt            bytes resolved but the document is damaged
    # What must never happen is a silent success with no extracted text.
    assert codes & {
        "executor_not_activated",
        "source_bytes_unavailable",
        "unsupported_format",
        "input_corrupt",
    }, rows
    assert all(row["status"] in {"blocked", "quarantined"} for row in rows), rows
    published = as_migrator(
        lambda connection: connection.fetchval(
            "SELECT count(*) FROM cortex_processing.chunk_revisions "
            "WHERE scope_id = $1 AND format_id = 'pdf'",
            FIXTURE["project"],
        )
    )
    assert published == 0, "a blocked document must not publish empty chunks"


def test_transform_is_disabled_by_default_and_never_alters_originals():
    async def before(connection):
        return await connection.fetchrow(
            "SELECT body_text, content_hash FROM cortex_core.content_revisions "
            "WHERE scope_id = $1 AND content_id = $2 AND revision = 1",
            FIXTURE["project"], FIXTURE["knowledge"],
        )

    original = as_migrator(before)

    result = enqueue_intent(
        job_kind="transform.distill",
        profile_id=PROFILE_DISTILL,
        dedupe_key=f"it-distill-off-{uuid.uuid4().hex[:12]}",
        payload={"transform": {"kind": "distill", "name": "distill.extractive"}},
        content_id=FIXTURE["knowledge"],
    )
    assert result.accepted == 1
    stats = run_worker_with_space(role="embed", max_jobs=25)
    assert stats.blocked >= 1

    async def blocked_job(connection):
        return await connection.fetchrow(
            "SELECT status, error_code, error_message FROM cortex_processing.jobs "
            "WHERE job_id = $1",
            result.enqueued[0].job_id,
        )

    job = as_migrator(blocked_job)
    assert job["status"] == "blocked"
    assert job["error_code"] == "configuration_missing"
    assert "transform_disabled" in job["error_message"]

    async def distillations(connection):
        return await connection.fetchval(
            "SELECT count(*) FROM cortex_processing.distillations WHERE scope_id = $1",
            FIXTURE["project"],
        )

    assert as_migrator(distillations) == 0
    assert as_migrator(before)["body_text"] == original["body_text"]

    # An operator enables a versioned transform; originals still never change.
    enabled_profile = uuid.uuid4()
    async def enable(connection):
        await connection.execute(
            """
            INSERT INTO cortex_processing.processing_profiles
                (profile_id, installation_id, profile_key, profile_version, parser,
                 chunking, embedder, transforms, coverage, status)
            SELECT $1, $2, profile_key, 2, parser, chunking, embedder,
                   jsonb_set(
                       jsonb_set(transforms, '{0,enabled}', 'true'::jsonb),
                       '{0,params,ratio}', '1.0'
                   ),
                   jsonb_set(coverage, '{min_text_length}', '1'),
                   'active'
              FROM cortex_processing.processing_profiles
             WHERE profile_id = $3
            """,
            enabled_profile, FIXTURE["installation"], PROFILE_DISTILL,
        )

    as_migrator(enable)
    enabled = enqueue_intent(
        job_kind="transform.distill",
        profile_id=enabled_profile,
        dedupe_key=f"it-distill-on-{uuid.uuid4().hex[:12]}",
        payload={"transform": {"kind": "distill", "name": "distill.extractive"}},
        content_id=FIXTURE["knowledge"],
    )
    assert enabled.accepted == 1
    run_worker_with_space(role="embed", max_jobs=25)

    async def derived(connection):
        row = await connection.fetchrow(
            "SELECT transform_kind, transform_identity, output_text, commitments, "
            "coverage FROM cortex_processing.distillations WHERE scope_id = $1",
            FIXTURE["project"],
        )
        original_now = await connection.fetchrow(
            "SELECT body_text, content_hash FROM cortex_core.content_revisions "
            "WHERE scope_id = $1 AND content_id = $2 AND revision = 1",
            FIXTURE["project"], FIXTURE["knowledge"],
        )
        job_row = await connection.fetchrow(
            "SELECT status, error_code, error_message "
            "FROM cortex_processing.jobs WHERE job_id = $1",
            enabled.enqueued[0].job_id,
        )
        return row, original_now, dict(job_row) if job_row else None

    row, original_now, job = as_migrator(derived)
    assert row is not None, f"an enabled transform publishes a derived row; job={job}"
    assert row["transform_kind"] == "distill"
    assert row["output_text"]
    assert json.loads(row["coverage"])["source_sentences"] >= 1
    assert original_now["body_text"] == original["body_text"]
    assert original_now["content_hash"] == original["content_hash"]


def test_no_transaction_is_open_while_a_handler_executes():
    """F08: the attempt must not hold a connection or lock during execution."""
    receipt = create_space()
    space_id = uuid.UUID(receipt["space_id"])
    generation_id = uuid.UUID(receipt["generation_id"])
    result = enqueue_intent(
        space_id=space_id, generation_id=generation_id,
        dedupe_key=f"it-notx-{uuid.uuid4().hex[:12]}",
    )
    probe: dict[str, Any] = {}
    registry = JobRegistry()
    handlers.register_builtin_handlers(registry)
    inner = registry.handler_for("embed.chunks")

    async def probing_handler(context: AttemptContext, services: AttemptServices):
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('cortex.principal_id', $1, true)",
                    str(context.principal_id),
                )
                await connection.execute(
                    "SELECT set_config('cortex.read_scope_ids', $1, true)",
                    str(context.scope_id),
                )
                await connection.execute(
                    "SELECT set_config('cortex.write_scope_id', $1, true)",
                    str(context.scope_id),
                )
                # NOWAIT fails immediately if the executing attempt held a lock.
                row = await connection.fetchrow(
                    "SELECT job_id, status FROM cortex_processing.jobs "
                    "WHERE job_id = $1 FOR UPDATE NOWAIT",
                    context.job_id,
                )
                probe["locked_row"] = row
                probe["services_has_connection"] = hasattr(services, "connection")
                probe["deadline_remaining"] = context.deadline > 0
        finally:
            await connection.close()
        return await inner.handler(context, services)

    # A private registry holding only the probing handler: the worker must not
    # re-register the builtins over it, which is what register_builtins=False
    # exists for (a composition root supplying its own handler for a kind).
    private = JobRegistry()
    private.register_job_handler(
        "embed.chunks", "embed", probing_handler, publisher=inner.publisher
    )
    stats = run_worker_with_space(
        role="embed", registry=private, max_jobs=5, register_builtins=False
    )
    assert stats.claimed >= 1
    assert probe.get("locked_row") is not None, "the job row must be lockable: no held transaction"
    assert probe["locked_row"]["status"] == "leased"
    assert probe["services_has_connection"] is False
    assert probe["deadline_remaining"] is True
    assert result.accepted == 1


def test_worker_roles_only_claim_their_own_kinds():
    registry = JobRegistry()
    handlers.register_builtin_handlers(registry)
    seen: list[str] = []

    async def graph_handler(context: AttemptContext, services: AttemptServices):
        seen.append(context.job_kind)
        return ExecutionReport(
            outcome=Outcome.OK, handler_output={"assertions": []}, stats={"assertions": 0}
        )

    async def graph_publisher(connection, context, report):
        await connection.execute(
            "INSERT INTO cortex_processing.outbox_events "
            "(event_id, scope_id, aggregate_kind, aggregate_id, event_type, payload) "
            "VALUES ($1, $2, 'processing_job', $3, 'processing.graph.published', $4::jsonb)",
            uuid.uuid4(), context.scope_id, context.job_id,
            json.dumps({"schema_version": 1,
                        "aggregate_version": context.fencing_epoch,
                        "output": report.handler_output}),
        )

    registry.register_job_handler(
        "graph.memory.extract", "graph", graph_handler, publisher=graph_publisher
    )
    graph_result = enqueue_intent(
        job_kind="graph.memory.extract",
        profile_id=None,
        content_id=None,
        revision=0,
        dedupe_key=f"it-graph-{uuid.uuid4().hex[:12]}",
        payload={"pin": {"repository_key": "helix", "commit_sha": "cafebabe"}},
    )
    assert graph_result.accepted == 1

    embed_stats = run_worker_with_space(role="embed", registry=registry, max_jobs=10)
    assert seen == [], "an embed worker must not run graph handlers"

    async def still_queued(connection):
        return await connection.fetchval(
            "SELECT status FROM cortex_processing.jobs WHERE job_id = $1",
            graph_result.enqueued[0].job_id,
        )

    assert as_migrator(still_queued) == "queued"

    graph_stats = run_worker_with_space(role="graph", registry=registry, max_jobs=10)
    assert graph_stats.claimed >= 1
    assert seen == ["graph.memory.extract"]
    assert as_migrator(still_queued) == "succeeded"
    assert embed_stats.blocked + embed_stats.failed == 0


def test_outbox_events_carry_the_versions_the_feed_dispatcher_orders_by():
    receipt = create_space()
    space_id = uuid.UUID(receipt["space_id"])
    result = enqueue_intent(
        space_id=space_id, generation_id=uuid.UUID(receipt["generation_id"]),
        dedupe_key=f"it-outbox-{uuid.uuid4().hex[:12]}",
    )
    claim = claim_one().job
    finish_attempt(claim, ExecutionReport(outcome=Outcome.OK))

    async def events(connection):
        return await connection.fetch(
            "SELECT event_id, event_type, aggregate_kind, aggregate_id, payload, "
            "delivered_at "
            "FROM cortex_processing.outbox_events WHERE scope_id = $1 "
            "ORDER BY created_at, event_id",
            FIXTURE["project"],
        )

    rows = as_migrator(events)
    types = [row["event_type"] for row in rows]
    assert "processing.job.accepted" in types
    assert "processing.space.created" in types
    assert "processing.job.succeeded" in types
    job_events = [
        row for row in rows
        if row["aggregate_id"] == result.enqueued[0].job_id
    ]
    versions = [json.loads(row["payload"])["aggregate_version"] for row in job_events]
    assert versions == sorted(versions), "per-aggregate versions must be monotonic"
    assert versions[0] == 0
    assert all(row["delivered_at"] is None for row in rows)
    assert all(json.loads(row["payload"])["schema_version"] == 1 for row in rows)

    # Delivery is marked exactly once; a second marking or an edit is refused.
    async def deliver(connection):
        target = job_events[0]["event_id"] if job_events else rows[0]["event_id"]
        await connection.execute(
            "UPDATE cortex_processing.outbox_events SET delivered_at = now() "
            "WHERE event_id = $1",
            target,
        )
        with pytest.raises(asyncpg.PostgresError) as excinfo:
            await connection.execute(
                "UPDATE cortex_processing.outbox_events SET delivered_at = now() "
                "WHERE event_id = $1",
                target,
            )
        assert excinfo.value.sqlstate == "55000"
        with pytest.raises(asyncpg.PostgresError):
            await connection.execute(
                "DELETE FROM cortex_processing.outbox_events WHERE event_id = $1", target
            )

    as_migrator(deliver)


def test_worker_registration_makes_executor_roles_observable():
    receipt = create_space()
    enqueue_intent(
        space_id=uuid.UUID(receipt["space_id"]),
        generation_id=uuid.UUID(receipt["generation_id"]),
        dedupe_key=f"it-workerreg-{uuid.uuid4().hex[:12]}",
    )
    stats = run_worker_with_space(role="embed", max_jobs=1)
    assert stats.claimed >= 1

    async def rows(connection):
        return await connection.fetch(
            "SELECT worker_id, worker_role, executor_roles, handler_kinds, "
            "package_version, started_at, last_heartbeat_at, stopped_at, stats "
            "FROM cortex_processing.workers ORDER BY started_at DESC LIMIT 5"
        )

    registered = as_migrator(rows)
    assert registered, "the worker must register a heartbeat row"
    embed = [row for row in registered if row["worker_role"] == "embed"]
    assert embed
    row = embed[0]
    assert "embed.chunks" in row["handler_kinds"]
    assert list(row["executor_roles"]) == ["core"]
    assert row["package_version"]
    assert row["last_heartbeat_at"] >= row["started_at"]
    # A graceful stop deregisters, so a missing executor role stops being
    # advertised instead of looking alive forever.
    assert row["stopped_at"] is not None
    assert json.loads(row["stats"])["claimed"] >= 1


_DEFAULT_SPACE: dict[str, Any] = {}


def _default_space() -> dict[str, Any]:
    """One shared embedding space for queue-mechanics tests."""
    if not _DEFAULT_SPACE:
        _DEFAULT_SPACE.update(create_space(name=f"it-queue-space-{RUN}"))
    return _DEFAULT_SPACE


def test_code_publish_index_enqueues_full_length_pins_without_collision():
    from cortex_v2.retrieval.models import CodeFileInput, CodePublishIndexRequest
    from cortex_v2.retrieval.operations import code_publish_index

    repository_key = f"r3-{RUN}-" + "r" * (128 - len(RUN) - 4)
    full_path = "src/" + "f" * 508

    async def publish(connection, context, commit_sha, source, path):
        request = CodePublishIndexRequest(
            repository_key=repository_key,
            commit_sha=commit_sha,
            files=[CodeFileInput(path=path, source=source)],
        )
        return await code_publish_index(
            connection, context, str(uuid.uuid4()), request, {}
        )

    async def scenario(connection, context):
        first_status, first, first_replayed = await publish(
            connection, context, "a" * 40, "def first():\n    pass\n", "src/first.py"
        )
        assert (first_status, first_replayed, first["job_created"]) == (
            202,
            False,
            True,
        )
        same_status, same, same_replayed = await publish(
            connection, context, "a" * 40, "def first():\n    pass\n", "src/first.py"
        )
        assert (same_status, same_replayed, same["job_created"]) == (202, False, False)
        assert (same["job_id"], same["dedupe_key"]) == (
            first["job_id"],
            first["dedupe_key"],
        )

        _, second, _ = await publish(
            connection, context, "b" * 64, "def second():\n    pass\n", full_path
        )
        _, third, _ = await publish(
            connection, context, "a" * 40, "def changed():\n    pass\n", "src/first.py"
        )
        assert (
            len({first["dedupe_key"], second["dedupe_key"], third["dedupe_key"]}) == 3
        )
        for receipt, commit_sha, path in (
            (first, "a" * 40, "src/first.py"),
            (second, "b" * 64, full_path),
            (third, "a" * 40, "src/first.py"),
        ):
            assert len(receipt["dedupe_key"]) <= 128
            row = await connection.fetchrow(
                "SELECT dedupe_key, intent FROM cortex_processing.jobs "
                "WHERE job_id = $1 AND scope_id = $2 "
                "AND job_kind = 'graph.code.extract'",
                uuid.UUID(receipt["job_id"]),
                context.selected.scope_id,
            )
            assert row["dedupe_key"] == receipt["dedupe_key"]
            assert json.loads(row["intent"])["payload"]["pin"] == {
                "repository_key": repository_key,
                "commit_sha": commit_sha,
                "snapshot_id": receipt["snapshot_id"],
            }
            assert json.loads(row["intent"])["payload"]["files"][0]["path"] == path

    owner_call(scenario)
