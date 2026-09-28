"""Bounded Cortex v2 worker process.

    python -m cortex_v2.worker --role {doc,embed,graph}

One application package runs as any role; each role claims only the job kinds
registered for it and only work whose executor role it actually serves, so the
Document Processor can ship its own dependency set without the API or embed
images carrying native parsers or model weights (N05, R12).

The loop is deliberately boring and bounded:

1. authenticate, resolve the grants this principal holds right now, register a
   heartbeat row so a missing executor role is observable;
2. claim one job in a short transaction with ``FOR UPDATE SKIP LOCKED``,
   advancing its fencing epoch;
3. execute the handler with **no transaction open** and a hard deadline;
4. publish in a second short transaction that re-checks epoch, lease owner,
   lease expiry, cancellation and source currency before any effect is written;
5. renew the lease on a heartbeat while executing, and stop when it is lost or
   cancellation is requested.

Accepted work therefore never lives only in this process: killing the worker at
any point leaves a durable job whose lease expires and is reclaimed, and the
killed attempt can no longer publish.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Sequence
from urllib.parse import unquote, urlsplit

import asyncpg

from .. import __version__
from ..config import (
    EXPECTED_DATABASE,
    EXPECTED_DATABASE_ROLE,
    ConfigurationError,
    active_profile,
    read_secret_path,
)
from ..store import Principal, authenticate, token_digest
from . import handlers, parsers, queue, repository
from .blobs import resolver_from_root
from .contracts import (
    AttemptContext,
    ExecutionReport,
    Outcome,
    WORKER_ROLES,
)
from .registry import JOB_REGISTRY, JobRegistry, RegistryError
from .services import (
    AuthorizedScopes,
    ProcessingServices,
    ScopeUnauthorized,
    WorkerIdentity,
)

logger = logging.getLogger("cortex_v2.processing.runtime")

#: Executor capabilities per worker role. ``doc`` also serves the pure-stdlib
#: core formats, so a Document Processor deployment never needs a second worker
#: for text. ``graph`` claims Retrieval's ``graph.*`` kinds, which pin
#: ``required_role='graph'``, and keeps ``core`` so graph work can also read and
#: chunk plain text. ``embed`` stays core-only: it must never pick up document
#: or graph work it cannot complete.
ROLE_EXECUTOR_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "doc": ("core", "doc"),
    "embed": ("core",),
    "graph": ("core", "graph"),
}

#: Backoff when concurrency admission is refused: the queue is full of in-flight
#: work, so polling harder would only burn connections.
BUDGET_BACKOFF_SECONDS = 5.0
ERROR_BACKOFF_SECONDS = 2.0
POOL_COMMAND_TIMEOUT_SECONDS = 30

#: Environment settings whose VALUE is a path under /run/secrets. They are
#: handed to ``config.read_secret_path`` by NAME, exactly like the
#: settings-derived ``database_url_file`` / ``principal_id_file`` references;
#: passing the resolved path would double-look-up a path as if it were a name.
WORKER_TOKEN_FILE_ENV = "CORTEX_V2_WORKER_TOKEN_FILE"
TOKEN_PEPPER_FILE_ENV = "CORTEX_V2_TOKEN_PEPPER_FILE"
INSTALLATION_ID_FILE_ENV = "CORTEX_V2_WORKER_INSTALLATION_ID_FILE"


@dataclass(slots=True)
class WorkerStats:
    claimed: int = 0
    published: int = 0
    fenced_out: int = 0
    retried: int = 0
    blocked: int = 0
    quarantined: int = 0
    cancelled: int = 0
    failed: int = 0
    empty_polls: int = 0
    budget_deferrals: int = 0
    errors: int = 0
    last_outcome: str | None = None

    def record(self, decision: queue.AttemptDecision) -> None:
        self.last_outcome = decision.outcome_code
        if not decision.published:
            self.fenced_out += 1
            return
        self.published += 1
        status = decision.job_status
        if status == "queued":
            self.retried += 1
        elif status == "blocked":
            self.blocked += 1
        elif status == "quarantined":
            self.quarantined += 1
        elif status == "cancelled":
            self.cancelled += 1
        elif status == "failed":
            self.failed += 1

    def as_dict(self) -> dict[str, int | str]:
        return {
            "claimed": self.claimed,
            "published": self.published,
            "fenced_out": self.fenced_out,
            "retried": self.retried,
            "blocked": self.blocked,
            "quarantined": self.quarantined,
            "cancelled": self.cancelled,
            "failed": self.failed,
            "empty_polls": self.empty_polls,
            "budget_deferrals": self.budget_deferrals,
            "errors": self.errors,
        }


@dataclass(slots=True)
class Worker:
    """One bounded worker process."""

    settings: Any
    role: str
    worker_id: str
    registry: JobRegistry = JOB_REGISTRY
    #: Set False when the composition root supplies its own handler set for a
    #: kind (a host override, or a test proving the no-open-transaction
    #: property). Default True: a worker ships the built-in handlers.
    register_builtins: bool = True
    stats: WorkerStats = field(default_factory=WorkerStats)
    pool: asyncpg.Pool | None = None
    identity: WorkerIdentity | None = None
    scopes: AuthorizedScopes = field(
        default_factory=lambda: AuthorizedScopes((), ())
    )
    services: ProcessingServices | None = None
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    _in_flight: set[asyncio.Task[Any]] = field(default_factory=set)
    _executor: ThreadPoolExecutor | None = None

    @property
    def job_kinds(self) -> tuple[str, ...]:
        return self.registry.kinds_for_role(self.role)

    @property
    def executor_roles(self) -> tuple[str, ...]:
        """Canonical capabilities for the role, extended by configuration.

        The role mapping is authoritative — a graph worker keeps ``graph`` even
        if the environment only lists ``core`` — while
        ``CORTEX_V2_WORKER_EXECUTOR_ROLES`` may extend it for a container that
        really ships another executor. It can never silently drop the role's own
        capability, which would strand its queued work.
        """
        configured = tuple(getattr(self.settings.worker, "executor_roles", ()) or ())
        canonical = ROLE_EXECUTOR_CAPABILITIES.get(self.role, ())
        return tuple(dict.fromkeys((*canonical, *configured)))

    # -- startup ------------------------------------------------------------

    async def start(self) -> None:
        worker = self.settings.worker
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(worker.thread_pool_size)),
            thread_name_prefix=f"cortex-v2-{self.role}",
        )
        self.pool = await asyncpg.create_pool(
            self._database_url(),
            min_size=0,
            max_size=max(2, int(worker.max_concurrent_attempts) * 2),
            command_timeout=POOL_COMMAND_TIMEOUT_SECONDS,
            server_settings={"application_name": f"cortex-v2-worker-{self.role}"},
        )
        try:
            configure_worker(
                self.registry,
                handler_modules=worker.handler_modules,
                register_builtins=self.register_builtins,
            )
        except RegistryError as exc:
            await self._close_pool()
            raise ConfigurationError(str(exc)) from exc
        if not self.job_kinds:
            await self._close_pool()
            raise ConfigurationError(
                f"no job handlers are registered for role {self.role!r}; "
                f"handler modules: {', '.join(worker.handler_modules) or 'none'}"
            )
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                self.identity = await self._resolve_identity(connection)
                await connection.execute(
                    "SELECT set_config('cortex.principal_id', $1, true)",
                    str(self.identity.principal_id),
                )
                self.scopes = await self._resolve_scopes(connection)
        if not self.scopes.readable:
            await self._close_pool()
            raise ConfigurationError(
                "the worker principal holds no active scope grant; refusing to run "
                "with no authorized work"
            )
        self.services = ProcessingServices(
            pool=self.pool,
            identity=self.identity,
            scopes=self.scopes,
            settings=self.settings,
            blobs=resolver_from_root(worker.blob_root),
            thread_executor=self._executor,
        )
        await self._register_worker_row()
        logger.info(
            "worker started role=%s worker_id=%s kinds=%s executor_roles=%s "
            "readable_scopes=%d writable_scopes=%d adapters=%s",
            self.role,
            self.worker_id,
            ",".join(self.job_kinds),
            ",".join(self.executor_roles),
            len(self.scopes.readable),
            len(self.scopes.writable),
            ",".join(parsers.registry_report()["available"]) or "none",
        )

    def _database_url(self) -> str:
        return resolve_database_url(self.settings)

    async def _resolve_identity(self, connection: asyncpg.Connection) -> WorkerIdentity:
        """Authenticate the worker and pin its installation identity.

        The credential path is preferred: it proves the principal is active and
        makes revocation stop the worker. A directly configured principal id is
        still verified against the installation registry rather than trusted.
        """
        if os.environ.get(WORKER_TOKEN_FILE_ENV) and os.environ.get(
            TOKEN_PEPPER_FILE_ENV
        ):
            token = read_secret_path(WORKER_TOKEN_FILE_ENV).decode().strip()
            pepper = bytes.fromhex(
                read_secret_path(TOKEN_PEPPER_FILE_ENV, minimum_bytes=64).decode()
            )
            principal: Principal = await authenticate(connection, token_digest(token, pepper))
            installation_id = principal.installation_id
            principal_id = principal.principal_id
        else:
            principal_id, installation_id = resolve_principal_identity(self.settings)
            # The installation check is an RLS-respecting SECURITY DEFINER
            # helper keyed on the current principal, so the principal must be
            # declared before it is asked whether the pair belongs together.
            await connection.execute(
                "SELECT set_config('cortex.principal_id', $1, true)", str(principal_id)
            )
            matches = await connection.fetchval(
                "SELECT cortex_core.caller_installation_matches($1)", installation_id
            )
            if not matches:
                raise ConfigurationError(
                    "the configured worker principal does not belong to the "
                    "configured installation"
                )
        return WorkerIdentity(
            worker_id=self.worker_id,
            principal_id=principal_id,
            installation_id=installation_id,
            role=self.role,
            executor_roles=self.executor_roles,
            package_version=__version__,
        )

    async def _resolve_scopes(self, connection: asyncpg.Connection) -> AuthorizedScopes:
        rows = await connection.fetch(
            """
            SELECT g.scope_id, g.can_read, g.can_write, g.can_publish, s.scope_kind
              FROM cortex_auth.scope_grants AS g
              JOIN cortex_core.scopes AS s ON s.scope_id = g.scope_id
             WHERE g.principal_id = $1
               AND g.revoked_at IS NULL
               AND s.is_active
            """,
            self.identity.principal_id if self.identity else None,
        )
        readable = tuple(row["scope_id"] for row in rows if row["can_read"])
        writable = tuple(
            row["scope_id"]
            for row in rows
            if row["can_write"]
            and (row["scope_kind"] != "shared" or row["can_publish"])
        )
        return AuthorizedScopes(readable=readable, writable=writable)

    async def _register_worker_row(self) -> None:
        assert self.identity is not None and self.services is not None
        scope = self.scopes.readable[0]
        async with self.services.transaction(scope, write=False) as connection:
            await repository.register_worker(
                connection,
                worker_id=self.worker_id,
                installation_id=self.identity.installation_id,
                principal_id=self.identity.principal_id,
                worker_role=self.role,
                executor_roles=self.executor_roles,
                handler_kinds=self.job_kinds,
                package_version=__version__,
            )

    async def _close_pool(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    # -- loops --------------------------------------------------------------

    async def run(
        self, *, once: bool = False, max_jobs: int | None = None
    ) -> WorkerStats:
        """Claim and execute work until stopped, or until a bound is reached.

        ``max_jobs`` bounds a run for smoke, drain and integration use: the loop
        stops after that many claims, or as soon as nothing is claimable by this
        role. ``once`` is the single-job case.
        """
        assert self.services is not None
        bound = 1 if once else max_jobs
        processed = 0
        heartbeat = asyncio.create_task(self._heartbeat_loop(), name="worker-heartbeat")
        reaper = asyncio.create_task(self._reap_loop(), name="worker-reaper")
        try:
            while not self.stopping.is_set():
                if bound is not None and processed >= bound:
                    break
                claim = await self._claim_once()
                if claim is None:
                    if bound is not None:
                        break
                    await self._sleep(self.settings.worker.poll_interval_seconds)
                    continue
                self.stats.claimed += 1
                processed += 1
                task = asyncio.create_task(
                    self._attempt(claim), name=f"attempt-{claim.job_id}"
                )
                self._in_flight.add(task)
                task.add_done_callback(self._in_flight.discard)
                if bound == 1:
                    await task
                    break
                await self._admit_concurrency()
        finally:
            heartbeat.cancel()
            reaper.cancel()
            await asyncio.gather(heartbeat, reaper, return_exceptions=True)
            await self.shutdown()
        return self.stats

    async def _admit_concurrency(self) -> None:
        limit = max(1, int(self.settings.worker.max_concurrent_attempts))
        while len(self._in_flight) >= limit and not self.stopping.is_set():
            await asyncio.sleep(0.05)

    async def _claim_once(self) -> queue.ClaimedJob | None:
        assert self.services is not None
        if not self.scopes.writable:
            return None
        try:
            async with self.services.claim_transaction() as connection:
                outcome = await queue.claim_job(
                    connection,
                    worker_id=self.worker_id,
                    worker_role=self.role,
                    executor_roles=self.executor_roles,
                    job_kinds=self.job_kinds,
                    writable_scopes=self.scopes.writable,
                    lease_seconds=int(self.settings.worker.lease_seconds),
                    principal_id=self.services.identity.principal_id,
                )
        except queue.LeaseRaceLost:
            return None
        except queue.BudgetExhausted as exc:
            self.stats.budget_deferrals += 1
            logger.info("claim refused by concurrency budget: %s", exc.detail)
            await self._sleep(BUDGET_BACKOFF_SECONDS)
            return None
        except ScopeUnauthorized as exc:
            logger.warning("claim blocked by scope authorization: %s", exc.detail)
            await self._sleep(ERROR_BACKOFF_SECONDS)
            return None
        except asyncpg.PostgresError as exc:
            self.stats.errors += 1
            logger.error(
                "claim failed sqlstate=%s type=%s",
                getattr(exc, "sqlstate", None),
                type(exc).__name__,
            )
            await self._sleep(ERROR_BACKOFF_SECONDS)
            return None
        if outcome.state != queue.ClaimState.CLAIMED or outcome.job is None:
            self.stats.empty_polls += 1
            return None
        return outcome.job

    async def _attempt(self, claim: queue.ClaimedJob) -> None:
        assert self.services is not None
        spec = self.registry.handler_for(claim.job_kind)
        context = self._context(claim)
        if spec is None:
            report = ExecutionReport(
                outcome=Outcome.CONFIGURATION_MISSING,
                detail=f"no handler is registered for {claim.job_kind}",
            )
            await self._publish(claim, context, report, None)
            return
        missing = spec.validate_intent(claim.intent)
        if missing:
            report = ExecutionReport(
                outcome=Outcome.INPUT_REJECTED,
                detail=f"the job intent is missing required keys: {', '.join(missing)}",
            )
            await self._publish(claim, context, report, spec.publisher)
            return

        deadline = float(self.settings.worker.attempt_deadline_seconds)
        cancel_event = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._lease_loop(claim, cancel_event), name=f"lease-{claim.job_id}"
        )
        started = time.monotonic()
        try:
            report = await asyncio.wait_for(
                spec.handler(context, self.services), timeout=deadline
            )
        except asyncio.TimeoutError:
            report = ExecutionReport(
                outcome=Outcome.PROVIDER_TIMEOUT,
                detail=f"the handler exceeded its {deadline:.0f}s attempt deadline",
            )
        except asyncio.CancelledError:
            heartbeat.cancel()
            raise
        except Exception as exc:  # a handler bug must not kill the worker
            self.stats.errors += 1
            logger.exception("handler failed job_id=%s kind=%s", claim.job_id, claim.job_kind)
            report = ExecutionReport(
                outcome=Outcome.INTERNAL_ERROR,
                detail=f"{type(exc).__name__}: {exc}"[:512],
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        if cancel_event.is_set():
            await self._finalize_cancelled(claim)
            return
        await self._publish(claim, context, report, spec.publisher)
        logger.info(
            "attempt finished job_id=%s kind=%s outcome=%s elapsed=%.3fs",
            claim.job_id,
            claim.job_kind,
            report.outcome.value,
            time.monotonic() - started,
        )

    def _context(self, claim: queue.ClaimedJob) -> AttemptContext:
        assert self.identity is not None
        return AttemptContext(
            job_id=claim.job_id,
            job_kind=claim.job_kind,
            scope_id=claim.scope_id,
            installation_id=claim.installation_id,
            principal_id=self.identity.principal_id,
            fencing_epoch=claim.fencing_epoch,
            lease_owner=claim.lease_owner,
            attempt_id=claim.attempt_id,
            role=self.role,
            intent=claim.intent,
            deadline=time.monotonic() + float(self.settings.worker.attempt_deadline_seconds),
            attempt_number=claim.attempt_number,
            content_id=claim.content_id,
            source_revision=claim.source_revision,
            profile_id=claim.profile_id,
            space_id=claim.space_id,
            generation_id=claim.generation_id,
        )

    async def _lease_loop(
        self, claim: queue.ClaimedJob, cancel_event: asyncio.Event
    ) -> None:
        assert self.services is not None
        interval = max(1.0, float(self.settings.worker.heartbeat_interval_seconds))
        while not self.stopping.is_set():
            await asyncio.sleep(interval)
            try:
                async with self.services.transaction(claim.scope_id, write=True) as connection:
                    state = await queue.renew_lease(
                        connection,
                        claim=claim,
                        lease_seconds=int(self.settings.worker.lease_seconds),
                    )
            except (asyncpg.PostgresError, ScopeUnauthorized) as exc:
                logger.warning(
                    "heartbeat failed job_id=%s error=%s", claim.job_id, type(exc).__name__
                )
                continue
            if state == queue.LeaseState.CANCELLED:
                cancel_event.set()
                return
            if state == queue.LeaseState.LOST:
                # The lease is gone, so the publish fence will refuse this
                # attempt. Stop heartbeating and let the refusal be recorded.
                return

    async def _publish(
        self,
        claim: queue.ClaimedJob,
        context: AttemptContext,
        report: ExecutionReport,
        publisher: Any,
    ) -> None:
        assert self.services is not None
        worker = self.settings.worker

        async def publish(connection: asyncpg.Connection) -> None:
            reason = await self.services.verify_source_current(connection, context)
            if reason is not None:
                raise queue.SourceMoved(reason)
            if publisher is not None:
                await publisher(connection, context, report)

        try:
            async with self.services.transaction(claim.scope_id, write=True) as connection:
                decision = await queue.finish_attempt(
                    connection,
                    claim=claim,
                    report=report,
                    publish=publish,
                    backoff_base_seconds=float(worker.backoff_base_seconds),
                    backoff_cap_seconds=float(worker.backoff_cap_seconds),
                )
        except queue.SourceMoved as exc:
            decision = await self._source_moved(claim, exc.reason)
        except ScopeUnauthorized as exc:
            self.stats.errors += 1
            logger.error(
                "publish blocked by scope authorization job_id=%s detail=%s",
                claim.job_id,
                exc.detail,
            )
            return
        except asyncpg.PostgresError as exc:
            self.stats.errors += 1
            logger.error(
                "publish failed job_id=%s sqlstate=%s type=%s",
                claim.job_id,
                getattr(exc, "sqlstate", None),
                type(exc).__name__,
            )
            return
        self.stats.record(decision)
        if not decision.published:
            logger.warning(
                "publishing refused job_id=%s epoch=%d reason=%s",
                claim.job_id,
                claim.fencing_epoch,
                decision.fenced_reason,
            )

    async def _source_moved(self, claim: queue.ClaimedJob, reason: str) -> queue.AttemptDecision:
        assert self.services is not None
        async with self.services.transaction(claim.scope_id, write=True) as connection:
            decision = await queue.finalize_source_moved(
                connection, claim=claim, reason=reason
            )
        logger.info(
            "attempt cancelled: pinned source moved job_id=%s reason=%s",
            claim.job_id,
            reason,
        )
        return decision

    async def _finalize_cancelled(self, claim: queue.ClaimedJob) -> None:
        assert self.services is not None
        try:
            async with self.services.transaction(claim.scope_id, write=True) as connection:
                decision = await queue.finalize_cancelled(
                    connection,
                    claim=claim,
                    reason="cancellation was requested while the attempt was running",
                )
        except (asyncpg.PostgresError, ScopeUnauthorized) as exc:
            self.stats.errors += 1
            logger.error(
                "cancellation finalize failed job_id=%s error=%s",
                claim.job_id,
                type(exc).__name__,
            )
            return
        self.stats.record(decision)

    async def _heartbeat_loop(self) -> None:
        assert self.services is not None
        interval = max(5.0, float(self.settings.worker.heartbeat_interval_seconds) * 3)
        while not self.stopping.is_set():
            await asyncio.sleep(interval)
            try:
                async with self.pool.acquire() as connection:  # type: ignore[union-attr]
                    async with connection.transaction():
                        await connection.execute(
                            "SELECT set_config('cortex.principal_id', $1, true)",
                            str(self.services.identity.principal_id),
                        )
                        await repository.heartbeat_worker(
                            connection,
                            worker_id=self.worker_id,
                            stats={
                                **self.stats.as_dict(),
                                "in_flight": len(self._in_flight),
                            },
                        )
                        self.scopes = await self._resolve_scopes(connection)
            except (asyncpg.PostgresError, OSError) as exc:
                logger.warning("worker heartbeat failed: %s", type(exc).__name__)

    async def _reap_loop(self) -> None:
        assert self.services is not None
        interval = max(10.0, float(self.settings.worker.reap_interval_seconds))
        while not self.stopping.is_set():
            await asyncio.sleep(interval)
            for scope_id in self.scopes.writable:
                if self.stopping.is_set():
                    return
                try:
                    async with self.services.transaction(scope_id, write=True) as connection:
                        reaped = await queue.reap_jobs(connection, scope_id=scope_id)
                except (asyncpg.PostgresError, ScopeUnauthorized) as exc:
                    logger.warning(
                        "reap failed scope_id=%s error=%s", scope_id, type(exc).__name__
                    )
                    continue
                if any(reaped.values()):
                    logger.info("reaped work scope_id=%s %s", scope_id, reaped)

    async def shutdown(self) -> None:
        """Drain in-flight attempts, deregister the worker and release the pool."""
        await self._drain()
        await self._stop_worker_row()
        await self._close_pool()
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None

    async def _drain(self) -> None:
        if not self._in_flight:
            return
        drain_timeout = float(os.environ.get("CORTEX_V2_WORKER_DRAIN_SECONDS", "30"))
        logger.info("draining %d in-flight attempts", len(self._in_flight))
        _, pending = await asyncio.wait(
            set(self._in_flight), timeout=drain_timeout
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if pending:
            logger.warning(
                "%d attempts were cancelled mid-flight; their leases expire and the "
                "jobs are reclaimed",
                len(pending),
            )

    async def _stop_worker_row(self) -> None:
        if self.services is None or self.pool is None or not self.scopes.readable:
            return
        with contextlib.suppress(asyncpg.PostgresError, ScopeUnauthorized, OSError):
            async with self.services.transaction(
                self.scopes.readable[0], write=False
            ) as connection:
                await repository.stop_worker(
                    connection,
                    worker_id=self.worker_id,
                    stats={**self.stats.as_dict(), "in_flight": len(self._in_flight)},
                )

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.stopping.wait(), timeout=max(0.05, seconds))


def resolve_database_url(settings: Any) -> str:
    """Read and validate the worker DSN from its secret-file reference.

    Fails closed exactly like the API's ``active_profile()``: a named instance
    profile is REQUIRED, so an absent or unknown ``CORTEX_V2_SANDBOX_INSTANCE``
    refuses to start rather than falling back to an unpinned host. The DSN must
    then name the unprivileged runtime role, the private v2 database and that
    profile's database host. W1 and other v2 lanes share the runtime role and
    database name, so the host is what keeps a manually started worker out of
    the wrong lane; a bare ``postgresql://`` prefix check is not enough.
    """
    profile = active_profile()
    url = read_secret_path(settings.worker.database_url_file).decode().strip()
    parsed = urlsplit(url)
    if parsed.scheme != "postgresql":
        raise ConfigurationError("the worker database URL is not a postgresql DSN")
    if unquote(parsed.username or "") != EXPECTED_DATABASE_ROLE:
        raise ConfigurationError(
            "the worker must connect as the unprivileged cortex_v2_app runtime role"
        )
    if parsed.path != f"/{EXPECTED_DATABASE}":
        raise ConfigurationError("the worker database URL names the wrong database")
    if parsed.hostname != profile.database_host:
        raise ConfigurationError(
            "the worker database URL must target this instance's database host "
            f"({profile.database_host}); refusing to start against another lane"
        )
    return url


def resolve_principal_identity(settings: Any) -> tuple[uuid.UUID, uuid.UUID]:
    """Read the worker principal and installation ids from secret-file references.

    Both are name-only references resolved through the same ``/run/secrets``
    guard the API uses; neither value is a credential, and neither is logged.
    """
    if not os.environ.get(INSTALLATION_ID_FILE_ENV):
        raise ConfigurationError(
            "configure CORTEX_V2_WORKER_TOKEN_FILE with CORTEX_V2_TOKEN_PEPPER_FILE, "
            "or the worker principal id file with "
            "CORTEX_V2_WORKER_INSTALLATION_ID_FILE"
        )
    try:
        principal_id = uuid.UUID(
            read_secret_path(settings.worker.principal_id_file).decode().strip()
        )
    except ValueError as exc:
        raise ConfigurationError(
            "the configured worker principal id is not a UUID"
        ) from exc
    try:
        installation_id = uuid.UUID(
            read_secret_path(INSTALLATION_ID_FILE_ENV).decode().strip()
        )
    except ValueError as exc:
        raise ConfigurationError(
            "the configured worker installation id is not a UUID"
        ) from exc
    return principal_id, installation_id


def configure_worker(
    registry: JobRegistry | None = None,
    *,
    handler_modules: Sequence[str] = (),
    register_builtins: bool = True,
) -> JobRegistry:
    """Register Processing's job handlers on a registry (default: the singleton).

    This is the public hook a worker entrypoint calls once at boot, before it
    claims anything. It registers the built-in ``doc.extract``,
    ``embed.chunks``, ``transform.distill`` and ``transform.compact`` handlers
    with their fenced publishers, then imports any extra handler modules (for
    example Retrieval's ``cortex_v2.retrieval.jobs``) so their kinds become
    claimable. Registration is idempotent for identical handlers and raises
    ``RegistryConflict`` for a conflicting one, so a worker never starts with an
    ambiguous kind mapping.

    ``register_builtins=False`` is for a composition root that supplies its own
    handler for a built-in kind; extra handler modules are still loaded.
    """
    active = registry if registry is not None else JOB_REGISTRY
    if register_builtins:
        handlers.register_builtin_handlers(active)
    active.load_modules(handler_modules)
    return active


def executor_capabilities(role: str) -> tuple[str, ...]:
    """Executor roles a worker role may claim; unknown roles claim nothing."""
    return ROLE_EXECUTOR_CAPABILITIES.get(role, ())


def default_worker_id(role: str) -> str:
    hostname = socket.gethostname().split(".", 1)[0][:48] or "worker"
    return f"{role}-{hostname}-{os.getpid()}"[:128]


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cortex_v2.worker",
        description="Run the Cortex v2 processing worker for one role.",
    )
    parser.add_argument(
        "--role",
        required=True,
        choices=WORKER_ROLES,
        help="worker role; each role claims only its own job kinds",
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        help="stable lease owner identity (default: role-host-pid)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="process at most one claimed job and exit (smoke and integration runs)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("CORTEX_V2_WORKER_LOG_LEVEL", "INFO"),
        help="logging level (default INFO)",
    )
    return parser


def resolve_settings(role: str) -> Any:
    """Load typed settings and pin the role chosen on the command line.

    The command line role is authoritative and also supplies the default for the
    typed setting, so a deployment does not state the role twice. A configured
    role that contradicts the command line is an error, not a silent override.
    """
    from .settings import ProcessingSettings

    os.environ.setdefault("CORTEX_V2_WORKER_ROLE", role)
    settings = ProcessingSettings.from_env()
    configured = settings.worker.role
    if configured and configured != role:
        raise ConfigurationError(
            f"--role {role!r} contradicts the configured worker role {configured!r}"
        )
    if configured != role:
        settings = replace(settings, worker=replace(settings.worker, role=role))
    return settings


async def run_worker(
    settings: Any, *, role: str, worker_id: str, once: bool = False
) -> WorkerStats:
    worker = Worker(settings=settings, role=role, worker_id=worker_id)
    await worker.start()
    return await worker.run(once=once)


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    worker: Worker | None = None

    def request_stop() -> None:
        logger.info("stop requested; finishing in-flight attempts before exit")
        if worker is not None:
            worker.stopping.set()

    for signal_number in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, ValueError, OSError):
            loop.add_signal_handler(signal_number, request_stop)

    try:
        settings = resolve_settings(args.role)
    except ConfigurationError as exc:
        logger.error("configuration is invalid: %s", exc)
        return 2
    worker_id = args.worker_id or default_worker_id(args.role)
    worker = Worker(settings=settings, role=args.role, worker_id=worker_id)
    try:
        loop.run_until_complete(worker.start())
        loop.run_until_complete(worker.run(once=args.once))
    except ConfigurationError as exc:
        logger.error("configuration is invalid: %s", exc)
        return 2
    except (asyncpg.PostgresError, OSError) as exc:
        logger.error("worker stopped on a storage or network failure: %s", type(exc).__name__)
        return 1
    except KeyboardInterrupt:
        logger.info("interrupted")
    finally:
        with contextlib.suppress(Exception):
            loop.run_until_complete(worker.shutdown())
        loop.close()
    logger.info("worker stopped stats=%s", worker.stats.as_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
