"""Cortex v2 Coordination module (W2, R07-R10).

Owns handoffs, claims, leases, returns, reviews, approvals, human gates,
cross-project relays, epics/waves/boards/tasks with deterministic dispatch
eligibility, and work-product receipts with freshness semantics.

Domain code in this package never imports FastAPI, MCP or CLI packages; the
integrator mounts :data:`cortex_v2.coordination.operations.OPERATIONS`
generically onto a transport.
"""

from __future__ import annotations

__all__ = ["OPERATIONS"]


def __getattr__(name: str):
    if name == "OPERATIONS":
        from .operations import OPERATIONS

        return OPERATIONS
    raise AttributeError(name)
