"""Operational metrics without content or secret leakage."""

from __future__ import annotations

from typing import Any

import asyncpg

from ..store import ScopeContext


async def run_metrics(
    connection: asyncpg.Connection,
    context: ScopeContext,
    payload: Any,
    path_params: dict[str, Any],
) -> dict[str, Any]:
    receipts = await connection.fetch(
        """
        SELECT operation, receipt_kind, count(*) AS total
          FROM cortex_core.command_receipts
         GROUP BY operation, receipt_kind
         ORDER BY operation, receipt_kind
        """
    )
    content_classes = await connection.fetch(
        """
        SELECT content_class, count(*) AS total
          FROM cortex_core.content_items
         GROUP BY content_class
         ORDER BY content_class
        """
    )
    content_statuses = await connection.fetch(
        """
        SELECT cortex_core.content_current_status(scope_id, content_id) AS status,
               count(*) AS total
          FROM cortex_core.content_items
         GROUP BY 1
         ORDER BY 1
        """
    )
    content_outbox = await connection.fetchrow(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE delivered_at IS NULL) AS undelivered,
               COALESCE(
                   max(extract(epoch FROM now() - created_at))
                       FILTER (WHERE delivered_at IS NULL),
                   0
               ) AS oldest_undelivered_seconds
          FROM cortex_core.content_outbox_events
        """
    )
    memory_outbox = await connection.fetchrow(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE delivered_at IS NULL) AS undelivered
          FROM cortex_core.outbox_events
        """
    )
    memory_records = await connection.fetchval(
        "SELECT count(*) FROM cortex_core.memory_records"
    )
    retention = await connection.fetchrow(
        """
        SELECT count(*) FILTER (WHERE action = 'archive') AS archived,
               count(*) FILTER (WHERE action = 'restore') AS restored
          FROM cortex_core.retention_ledger
        """
    )
    repairs = await connection.fetch(
        """
        SELECT outcome, count(*) AS total
          FROM cortex_core.repairs
         GROUP BY outcome
         ORDER BY outcome
        """
    )
    return {
        "commands": {
            "receipts_by_operation": [
                {
                    "operation": row["operation"],
                    "receipt_kind": row["receipt_kind"],
                    "total": int(row["total"]),
                }
                for row in receipts
            ],
            "coverage": "rls-view: calling principal and readable scopes",
        },
        "content": {
            "memory_records": int(memory_records),
            "items_by_class": [
                {"content_class": row["content_class"], "total": int(row["total"])}
                for row in content_classes
            ],
            "items_by_status": [
                {"status": row["status"], "total": int(row["total"])}
                for row in content_statuses
            ],
        },
        "outbox": {
            "content": {
                "total": int(content_outbox["total"]),
                "undelivered": int(content_outbox["undelivered"]),
                "oldest_undelivered_seconds": int(
                    content_outbox["oldest_undelivered_seconds"]
                ),
            },
            "memory": {
                "total": int(memory_outbox["total"]),
                "undelivered": int(memory_outbox["undelivered"]),
            },
        },
        "retention": {
            "archived_entries": int(retention["archived"]),
            "restored_entries": int(retention["restored"]),
        },
        "repairs_by_outcome": [
            {"outcome": row["outcome"], "total": int(row["total"])} for row in repairs
        ],
        "unavailable": [
            "denial_counters_not_persisted",
            "queue_metrics_not_built",
            "vector_index_metrics_not_built",
            "retrieval_latency_not_measured",
        ],
        "coverage": {
            "selected_scope": context.selected.alias,
            "read_scopes": [scope.alias for scope in context.read_scopes],
        },
    }
