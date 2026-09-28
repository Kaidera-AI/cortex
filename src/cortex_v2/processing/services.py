"""Worker-side implementation of the handler service ports.

Handlers never receive a connection. Every service method opens its own short
transaction, sets transaction-local Cortex context (principal, read scopes,
write scope) and returns plain data, so "no database transaction is held across
model, network or filesystem execution" is a structural property of the worker
rather than a review convention (F08).
"""

from __future__ import annotations

import asyncio
import functools
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

import asyncpg

from .. import __version__
from ..store import Principal, Scope, ScopeContext
from . import parsers, repository
from .blobs import BlobResolver, BlobResult, expected_digest
from .contracts import (
    AttemptContext,
    ChunkingPolicy,
    EmbeddingProvider,
    Outcome,
    ParseResult,
    Parser,
    ParserLimits,
    SourceRef,
    SpaceContract,
    StoredChunk,
)
from .formats import required_role_for


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    """Who this worker process is, from its own credential and configuration."""

    worker_id: str
    principal_id: uuid.UUID
    installation_id: uuid.UUID
    role: str
    executor_roles: tuple[str, ...]
    package_version: str = __version__


@dataclass(frozen=True, slots=True)
class AuthorizedScopes:
    """Grants this worker's principal actually holds right now."""

    readable: tuple[uuid.UUID, ...]
    writable: tuple[uuid.UUID, ...]

    def covers(self, scope_id: uuid.UUID) -> bool:
        return scope_id in self.readable

    def can_write(self, scope_id: uuid.UUID) -> bool:
        return scope_id in self.writable


class ScopeUnauthorized(RuntimeError):
    """The worker lost (or never had) authorization for a job's scope."""

    def __init__(self, scope_id: uuid.UUID, detail: str):
        super().__init__(detail)
        self.scope_id = scope_id
        self.detail = detail


class ProcessingServices:
    """Narrow service bundle handed to a job handler for one attempt."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        identity: WorkerIdentity,
        scopes: AuthorizedScopes,
        settings: Any,
        blobs: BlobResolver,
        thread_executor: Any = None,
        embedder_factory: Callable[[SpaceContract], tuple[EmbeddingProvider | None, str | None]]
        | None = None,
    ):
        self._pool = pool
        self.identity = identity
        self.scopes = scopes
        self.settings = settings
        self.blobs = blobs
        self._thread_executor = thread_executor
        self._embedder_factory = embedder_factory
        self.role = identity.role

    @property
    def parser_limits(self) -> ParserLimits:
        limits = getattr(self.settings, "parser_limits", None)
        return limits if isinstance(limits, ParserLimits) else ParserLimits()

    def scope_context(self, scope_id: uuid.UUID) -> ScopeContext:
        """A write-capable context for chained, in-transaction work intents."""
        if not self.scopes.can_write(scope_id):
            raise ScopeUnauthorized(scope_id, "the worker cannot write to this scope")
        scope = Scope(
            alias="",
            scope_id=scope_id,
            kind="project",
            can_read=True,
            can_write=True,
            can_publish=True,
        )
        principal = Principal(self.identity.principal_id, self.identity.installation_id)
        return ScopeContext(principal, scope, (scope,))

    @asynccontextmanager
    async def transaction(
        self, scope_id: uuid.UUID, *, write: bool
    ) -> AsyncIterator[asyncpg.Connection]:
        """One short transaction with transaction-local scope context."""
        if not self.scopes.covers(scope_id):
            raise ScopeUnauthorized(scope_id, "the worker cannot read this scope")
        if write and not self.scopes.can_write(scope_id):
            raise ScopeUnauthorized(scope_id, "the worker cannot write to this scope")
        # Least privilege: the transaction sees exactly the job's scope.
        read_ids = str(scope_id)
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('cortex.principal_id', $1, true)",
                    str(self.identity.principal_id),
                )
                await connection.execute(
                    "SELECT set_config('cortex.read_scope_ids', $1, true)", read_ids
                )
                await connection.execute(
                    "SELECT set_config('cortex.write_scope_id', $1, true)",
                    str(scope_id) if write else "",
                )
                yield connection

    @asynccontextmanager
    async def claim_transaction(self) -> AsyncIterator[asyncpg.Connection]:
        """Short transaction for claiming work.

        Every readable scope is visible so one statement can pick a candidate
        with ``FOR UPDATE SKIP LOCKED``; the write scope stays unset until the
        candidate's scope is known, then the claim sets it transaction-locally.
        """
        if not self.scopes.readable:
            raise ScopeUnauthorized(
                uuid.UUID(int=0), "the worker holds no readable scope grant"
            )
        read_ids = ",".join(sorted(str(item) for item in self.scopes.readable))
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('cortex.principal_id', $1, true)",
                    str(self.identity.principal_id),
                )
                await connection.execute(
                    "SELECT set_config('cortex.read_scope_ids', $1, true)", read_ids
                )
                await connection.execute(
                    "SELECT set_config('cortex.write_scope_id', $1, true)", ""
                )
                yield connection

    async def run_blocking(
        self, function: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Run bounded CPU work off the event loop, keyword arguments included.

        ``loop.run_in_executor`` accepts positional arguments only, so the call
        is bound with ``functools.partial``; dropping keyword arguments here
        would silently change what a parser or transform receives.
        """
        call = functools.partial(function, *args, **kwargs)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._thread_executor, call)

    # -- AttemptServices ----------------------------------------------------

    async def read_source(self, context: AttemptContext) -> SourceRef | None:
        if context.content_id is None or context.source_revision is None:
            return None
        async with self.transaction(context.scope_id, write=False) as connection:
            row = await repository.read_source_revision(
                connection,
                scope_id=context.scope_id,
                content_id=context.content_id,
                revision=context.source_revision,
            )
        if row is None:
            return None
        return SourceRef(
            scope_id=context.scope_id,
            content_id=context.content_id,
            revision=context.source_revision,
            content_class=row["content_class"],
            media_type=row["media_type"],
            body_text=row["body_text"],
            content_hash=row["content_hash"],
            payload=row["payload"],
            filename=row["filename"],
        )

    async def read_bytes(self, source: SourceRef) -> BlobResult:
        location = source.payload.get("location")
        if not isinstance(location, str) or not location:
            return BlobResult(
                outcome=Outcome.SOURCE_BYTES_UNAVAILABLE,
                detail="the revision records no blob location reference",
            )
        return await self.blobs.read(
            location,
            expected_sha256=expected_digest(source.payload),
            max_bytes=self.parser_limits.max_input_bytes,
        )

    async def read_space(self, space_id: uuid.UUID) -> SpaceContract | None:
        async with self.transaction(
            self._space_scope(space_id), write=False
        ) as connection:
            loaded = await repository.read_space_contract(connection, space_id)
        return loaded[0] if loaded else None

    async def read_space_generation(
        self, space_id: uuid.UUID, generation_id: uuid.UUID | None
    ) -> tuple[SpaceContract, uuid.UUID | None, str | None] | None:
        async with self.transaction(
            self._space_scope(space_id), write=False
        ) as connection:
            return await repository.read_space_contract(
                connection, space_id, generation_id=generation_id
            )

    def _space_scope(self, space_id: uuid.UUID) -> uuid.UUID:
        """Spaces are installation registries; any readable scope authorizes the read.

        The row level policy on ``embedding_spaces`` is installation-based, so
        the transaction only needs a readable scope to satisfy the helpers.
        """
        if self.scopes.readable:
            return self.scopes.readable[0]
        raise ScopeUnauthorized(space_id, "the worker has no readable scope")

    async def read_chunks(
        self, context: AttemptContext, space_id: uuid.UUID
    ) -> tuple[StoredChunk, ...]:
        if context.content_id is None or context.source_revision is None:
            return ()
        async with self.transaction(context.scope_id, write=False) as connection:
            return await repository.read_chunks(
                connection,
                scope_id=context.scope_id,
                content_id=context.content_id,
                revision=context.source_revision,
                space_id=space_id,
            )

    async def read_profile(self, profile_id: uuid.UUID) -> dict[str, Any] | None:
        async with self.transaction(
            self._space_scope(profile_id), write=False
        ) as connection:
            return await repository.get_profile(connection, profile_id)

    async def read_chunking_policy(self, space_id: uuid.UUID) -> ChunkingPolicy | None:
        async with self.transaction(
            self._space_scope(space_id), write=False
        ) as connection:
            chunking = await repository.read_space_chunking(connection, space_id)
        if not chunking:
            return None
        return ChunkingPolicy(
            policy_id=str(chunking["policy_id"]),
            version=int(chunking["version"]),
            kind=str(chunking["kind"]),
            target_chars=int(chunking["target_chars"]),
            max_chars=int(chunking["max_chars"]),
            overlap_chars=int(chunking["overlap_chars"]),
        )

    def parser_for(self, format_id: str) -> Parser | None:
        """A parser this worker role is allowed and able to execute."""
        parser = parsers.parser_for(format_id)
        if parser is None:
            return None
        required = required_role_for(format_id)
        if required not in self.identity.executor_roles:
            return None
        return parser

    async def parse(
        self, source: SourceRef, *, head: bytes | None = None
    ) -> ParseResult:
        """Detect, dispatch and bound a parse in the thread executor."""
        return await self.run_blocking(
            parsers.dispatch, source, limits=self.parser_limits, head=head
        )

    def embedder_for(self, space: SpaceContract) -> tuple[EmbeddingProvider | None, str | None]:
        if self._embedder_factory is not None:
            return self._embedder_factory(space)
        from .embedding import select_provider  # local import: optional adapter set

        return select_provider(self.settings, space)

    async def verify_source_current(
        self, connection: asyncpg.Connection, context: AttemptContext
    ) -> str | None:
        """Completion-time source recheck, called inside the publish transaction."""
        if context.content_id is None or context.source_revision is None:
            return None
        return await repository.verify_source_current(
            connection,
            scope_id=context.scope_id,
            content_id=context.content_id,
            revision=context.source_revision,
        )


__all__ = [
    "AuthorizedScopes",
    "ProcessingServices",
    "ScopeUnauthorized",
    "WorkerIdentity",
]
