"""Behavioral source-to-target proof against synthetic production-shaped v1 DDL."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid

import asyncpg
import pytest
from test_converter_ledger_db import (
    SOURCE_URL,
    TARGET_URL,
    prepare_synthetic_databases,
)

from cortex_v2.converter import ConversionPolicy
from cortex_v2.converter_run import convert_snapshot

pytestmark = pytest.mark.skipif(
    os.environ.get("CORTEX_V2_CONVERTER_SYNTHETIC_TEST") != "1",
    reason="requires the isolated synthetic converter database",
)


def synthetic_policy(fixture: dict[str, object]) -> ConversionPolicy:
    return ConversionPolicy.from_bytes(json.dumps({
        "version": "synthetic-v1-policy",
        "installation_id": fixture["installation_id"],
        "projects": {"fixture-project": {
            "scope_id": fixture["project_scope_id"],
            "principal_id": fixture["worker_principal_id"],
        }},
        "handoff_status": {"pending": "open"},
    }, sort_keys=True).encode())


def test_source_rows_reconcile_to_real_target_rows_and_reasoned_quarantine() -> None:
    async def check() -> None:
        fixture = await prepare_synthetic_databases()
        source = await asyncpg.connect(SOURCE_URL)
        target = await asyncpg.connect(TARGET_URL)
        handoff_ids: list[uuid.UUID] = []
        decision_ids: list[uuid.UUID] = []
        try:
            for status in ("pending", "claimed"):
                handoff_ids.append(await source.fetchval(
                    """INSERT INTO public.handoffs
                       (project, from_agent, to_role, summary, status)
                       VALUES ('fixture-project','synthetic-agent','implementer',
                               'Synthetic handoff', $1) RETURNING id""",
                    status,
                ))
            for project in ("fixture-project", "unmapped-fixture"):
                decision_ids.append(await source.fetchval(
                    """INSERT INTO public.decisions(project, summary, agent_name)
                       VALUES ($1,'Synthetic decision','synthetic-agent')
                       RETURNING id""",
                    project,
                ))
            result = await convert_snapshot(
                source, target, synthetic_policy(fixture),
                snapshot_sha256=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                converter_revision="synthetic-converter-r3",
            )
            scope = uuid.UUID(str(fixture["project_scope_id"]))
            handoff = await target.fetchrow(
                """SELECT status, brief FROM cortex_coord.handoffs
                   WHERE scope_id=$1 AND handoff_id=$2""",
                scope, handoff_ids[0],
            )
            assert (handoff["status"], handoff["brief"]) == (
                "open", "Synthetic handoff"
            )
            memory = await target.fetchrow(
                """SELECT record_type, body FROM cortex_core.memory_records
                   WHERE scope_id=$1 AND record_id=$2""",
                scope, decision_ids[0],
            )
            assert (memory["record_type"], memory["body"]) == (
                "decision", "Synthetic decision"
            )
            reconciliation = await target.fetch(
                """SELECT source_table, source_scope, source_count, target_count,
                          quarantined_count, skipped_count
                     FROM cortex_conversion.reconciliation
                    WHERE run_id=$1 ORDER BY source_table, source_scope""",
                result.run_id,
            )
            actual = {
                (row["source_table"], row["source_scope"]): tuple(row[key] for key in (
                    "source_count", "target_count", "quarantined_count", "skipped_count"
                ))
                for row in reconciliation
            }
            assert actual == {
                ("decisions", "fixture-project"): (1, 1, 0, 0),
                ("decisions", "unmapped-fixture"): (1, 0, 1, 0),
                ("handoffs", "fixture-project"): (2, 1, 1, 0),
            }
            reasons = await target.fetch(
                """SELECT reason, count(*)::int AS total
                     FROM cortex_conversion.outcomes
                    WHERE run_id=$1 AND outcome='quarantined'
                    GROUP BY reason ORDER BY reason""",
                result.run_id,
            )
            assert {row["reason"]: row["total"] for row in reasons} == {
                "ambiguous_lifecycle": 1, "unmapped_scope": 1,
            }
        finally:
            if handoff_ids:
                await source.execute(
                    "DELETE FROM public.handoffs WHERE id = ANY($1::uuid[])",
                    handoff_ids,
                )
            if decision_ids:
                await source.execute(
                    "DELETE FROM public.decisions WHERE id = ANY($1::uuid[])",
                    decision_ids,
                )
            await source.close()
            await target.close()

    asyncio.run(check())
