"""Job handler registry shared by every worker role.

One application package runs as any role (``python -m cortex_v2.worker --role
{doc,embed,graph}``). Handlers are registered per job kind with the role that
may execute them, so the Retrieval module can add ``graph.code.extract`` and
``graph.memory.extract`` without the Processing module importing anything from
it. Registration is fail-closed: an unknown kind, an unknown role or a
conflicting duplicate registration stops the worker at boot instead of quietly
running the wrong implementation.

A handler executes with no database transaction open and returns an
:class:`~cortex_v2.processing.contracts.ExecutionReport`. Its optional
publisher runs *inside* the fenced publish transaction, after the worker has
re-checked epoch, lease ownership, lease expiry and cancellation, so a stale
attempt can never publish (F06).
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any, Awaitable, Callable, Iterable

import asyncpg

from .contracts import (
    AttemptContext,
    AttemptServices,
    ExecutionReport,
    JOB_KINDS,
    WORKER_ROLES,
)

#: ``async (connection, context, report) -> None`` executed inside the fenced
#: publish transaction. Raise to roll the attempt back.
JobPublisher = Callable[
    [asyncpg.Connection, AttemptContext, ExecutionReport], Awaitable[None]
]


class RegistryError(RuntimeError):
    """The registry could not reach a usable, unambiguous state."""


class RegistryConflict(RegistryError):
    """A job kind is already registered to a different handler or role."""


@dataclass(frozen=True, slots=True)
class JobHandlerSpec:
    """One registered job kind."""

    kind: str
    role: str
    handler: Callable[[AttemptContext, AttemptServices], Awaitable[ExecutionReport]]
    publisher: JobPublisher | None = None
    summary: str = ""
    required_intent: tuple[str, ...] = ()

    def validate_intent(self, intent: dict[str, Any]) -> tuple[str, ...]:
        payload = intent.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        return tuple(key for key in self.required_intent if key not in payload)


class JobRegistry:
    """Mutable registry; the process-wide singleton is :data:`JOB_REGISTRY`."""

    def __init__(self) -> None:
        self._specs: dict[str, JobHandlerSpec] = {}

    def register_job_handler(
        self,
        kind: str,
        role: str,
        handler: Callable[[AttemptContext, AttemptServices], Awaitable[ExecutionReport]],
        *,
        publisher: JobPublisher | None = None,
        summary: str = "",
        required_intent: Iterable[str] = (),
    ) -> JobHandlerSpec:
        if kind not in JOB_KINDS:
            raise RegistryError(
                f"job kind {kind!r} is not part of the versioned kind set; "
                "adding a kind needs a contracts and 0004 migration change"
            )
        if role not in WORKER_ROLES:
            raise RegistryError(f"unknown worker role {role!r}; expected one of {WORKER_ROLES}")
        if not callable(handler):
            raise RegistryError(f"handler for {kind!r} must be callable")
        spec = JobHandlerSpec(
            kind=kind,
            role=role,
            handler=handler,
            publisher=publisher,
            summary=summary,
            required_intent=tuple(required_intent),
        )
        existing = self._specs.get(kind)
        if existing is not None:
            if existing == spec:
                return existing
            raise RegistryConflict(
                f"job kind {kind!r} is already registered "
                f"(role={existing.role!r}, handler={_name(existing.handler)}); "
                f"refusing to replace it with role={role!r}, handler={_name(handler)}"
            )
        self._specs[kind] = spec
        return spec

    def handler_for(self, kind: str) -> JobHandlerSpec | None:
        return self._specs.get(kind)

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def kinds_for_role(self, role: str) -> tuple[str, ...]:
        return tuple(sorted(kind for kind, spec in self._specs.items() if spec.role == role))

    def specs(self) -> tuple[JobHandlerSpec, ...]:
        return tuple(self._specs[kind] for kind in sorted(self._specs))

    def load_modules(self, module_names: Iterable[str]) -> tuple[str, ...]:
        """Import handler modules for their registration side effects.

        A module exposing ``register(registry)`` is called with this registry;
        otherwise the import itself must register. Any failure raises so a
        worker never starts with a silently missing handler.
        """
        loaded: list[str] = []
        for name in module_names:
            candidate = name.strip()
            if not candidate:
                continue
            try:
                module = import_module(candidate)
            except ImportError as exc:
                raise RegistryError(
                    f"handler module {candidate!r} could not be imported: {exc}"
                ) from exc
            register = getattr(module, "register", None)
            if callable(register):
                register(self)
            loaded.append(candidate)
        return tuple(loaded)


def _name(handler: Any) -> str:
    return getattr(handler, "__qualname__", None) or getattr(handler, "__name__", repr(handler))


#: Process-wide registry. ``cortex_v2.processing.handlers`` registers the
#: built-in kinds; Retrieval registers its graph kinds through
#: :meth:`JobRegistry.load_modules`.
JOB_REGISTRY = JobRegistry()


def register_job_handler(
    kind: str,
    role: str,
    handler: Callable[[AttemptContext, AttemptServices], Awaitable[ExecutionReport]],
    *,
    publisher: JobPublisher | None = None,
    summary: str = "",
    required_intent: Iterable[str] = (),
) -> JobHandlerSpec:
    """Module-level convenience delegating to :data:`JOB_REGISTRY`."""
    return JOB_REGISTRY.register_job_handler(
        kind,
        role,
        handler,
        publisher=publisher,
        summary=summary,
        required_intent=required_intent,
    )


__all__ = [
    "JOB_REGISTRY",
    "JobHandlerSpec",
    "JobPublisher",
    "JobRegistry",
    "RegistryConflict",
    "RegistryError",
    "register_job_handler",
]
