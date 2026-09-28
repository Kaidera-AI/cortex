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
        "entities": {
            "public.decisions": "convert",
            "public.handoffs": "convert",
        },
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


def test_partial_run_resumes_without_duplicate_canonical_rows() -> None:
    async def check() -> None:
        from unittest.mock import patch

        from cortex_v2 import converter_run

        fixture = await prepare_synthetic_databases()
        source = await asyncpg.connect(SOURCE_URL)
        target = await asyncpg.connect(TARGET_URL)
        decision_id = None
        handoff_ids: list[uuid.UUID] = []
        try:
            decision_id = await source.fetchval(
                """INSERT INTO public.decisions(project, summary, agent_name)
                   VALUES ('fixture-project','Synthetic resume decision',
                           'synthetic-agent') RETURNING id"""
            )
            for suffix in ("A", "B"):
                handoff_ids.append(await source.fetchval(
                    """INSERT INTO public.handoffs
                       (project, from_agent, to_role, summary, status)
                       VALUES ('fixture-project','synthetic-agent','implementer',
                               $1,'pending') RETURNING id""",
                    f"Synthetic resume handoff {suffix}",
                ))
            snapshot = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
            policy = synthetic_policy(fixture)
            original_write = converter_run._write_row
            calls = 0

            async def interrupt_after_first_table(*args: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise RuntimeError("synthetic interruption")
                await original_write(*args)

            with patch.object(
                converter_run, "_write_row", interrupt_after_first_table
            ), pytest.raises(RuntimeError, match="synthetic interruption"):
                await convert_snapshot(
                    source, target, policy, snapshot_sha256=snapshot,
                    converter_revision="synthetic-restart-r4",
                )
            assert await target.fetchval(
                "SELECT count(*) FROM cortex_core.memory_records "
                "WHERE record_id=$1", decision_id,
            ) == 1
            assert await target.fetchval(
                "SELECT count(*) FROM cortex_coord.handoffs "
                "WHERE handoff_id=ANY($1::uuid[])", handoff_ids,
            ) == 0
            result = await convert_snapshot(
                source, target, policy, snapshot_sha256=snapshot,
                converter_revision="synthetic-restart-r4",
            )
            assert (result.source_count, result.target_count) == (3, 3)
            assert await target.fetchval(
                "SELECT count(*) FROM cortex_core.memory_records "
                "WHERE record_id=$1", decision_id,
            ) == 1
            assert await target.fetchval(
                "SELECT count(*) FROM cortex_coord.handoffs "
                "WHERE handoff_id=ANY($1::uuid[])", handoff_ids,
            ) == 2
        finally:
            if handoff_ids:
                await source.execute(
                    "DELETE FROM public.handoffs WHERE id=ANY($1::uuid[])",
                    handoff_ids,
                )
            if decision_id is not None:
                await source.execute(
                    "DELETE FROM public.decisions WHERE id=$1", decision_id
                )
            await source.close()
            await target.close()

    asyncio.run(check())


def test_completed_second_run_is_write_free_and_rejects_source_drift() -> None:
    class NoWrites:
        def __init__(self, connection: asyncpg.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str) -> object:
            if name == "execute":
                raise AssertionError("completed run issued a target write")
            return getattr(self.connection, name)

    async def check() -> None:
        fixture = await prepare_synthetic_databases()
        source = await asyncpg.connect(SOURCE_URL)
        target = await asyncpg.connect(TARGET_URL)
        decision_id = handoff_id = None
        try:
            decision_id = await source.fetchval(
                """INSERT INTO public.decisions(project, summary, agent_name)
                   VALUES ('fixture-project','Synthetic stable decision',
                           'synthetic-agent') RETURNING id"""
            )
            handoff_id = await source.fetchval(
                """INSERT INTO public.handoffs
                   (project, from_agent, to_role, summary, status)
                   VALUES ('fixture-project','synthetic-agent','implementer',
                           'Synthetic quarantined handoff','claimed') RETURNING id"""
            )
            policy = synthetic_policy(fixture)
            snapshot = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
            result = await convert_snapshot(
                source, target, policy, snapshot_sha256=snapshot,
                converter_revision="synthetic-idempotent-r4",
            )
            tables = (
                "cortex_conversion.runs", "cortex_conversion.outcomes",
                "cortex_conversion.source_counts",
                "cortex_conversion.reconciliation",
                "cortex_conversion.schema_versions",
                "cortex_core.source_connectors", "cortex_core.content_items",
                "cortex_core.content_revisions",
                "cortex_core.content_lexical_documents",
                "cortex_core.memory_records", "cortex_core.lexical_documents",
                "cortex_coord.handoffs",
                "cortex_core.conversion_quarantine",
            )

            async def fingerprint() -> tuple[str, ...]:
                return tuple([
                    await target.fetchval(
                        "SELECT md5(coalesce(string_agg(to_jsonb(t)::text, '|' "
                        "ORDER BY to_jsonb(t)::text),'')) "
                        f"FROM {table} t"
                    )
                    for table in tables
                ])

            before = await fingerprint()
            second = await convert_snapshot(
                source, NoWrites(target), policy, snapshot_sha256=snapshot,
                converter_revision="synthetic-idempotent-r4",
            )
            assert second == result
            assert await fingerprint() == before
            quarantine = await target.fetchrow(
                """SELECT q.quarantine_id, q.original_payload
                     FROM cortex_core.conversion_quarantine q
                     JOIN cortex_conversion.outcomes o
                       ON o.quarantine_id=q.quarantine_id
                    WHERE o.run_id=$1""",
                result.run_id,
            )
            assert quarantine is not None
            await target.execute(
                """UPDATE cortex_core.conversion_quarantine
                      SET original_payload='{"unapproved":"synthetic"}'::jsonb
                    WHERE quarantine_id=$1""",
                quarantine["quarantine_id"],
            )
            try:
                with pytest.raises(
                    RuntimeError, match="quarantine reference diverged"
                ):
                    await convert_snapshot(
                        source, NoWrites(target), policy, snapshot_sha256=snapshot,
                        converter_revision="synthetic-idempotent-r4",
                    )
            finally:
                await target.execute(
                    """UPDATE cortex_core.conversion_quarantine
                          SET original_payload=$1::jsonb
                        WHERE quarantine_id=$2""",
                    quarantine["original_payload"], quarantine["quarantine_id"],
                )
            assert await fingerprint() == before
            await source.execute(
                "UPDATE public.decisions SET summary='Synthetic changed decision' "
                "WHERE id=$1", decision_id,
            )
            with pytest.raises(RuntimeError, match="source .*diverged"):
                await convert_snapshot(
                    source, NoWrites(target), policy, snapshot_sha256=snapshot,
                    converter_revision="synthetic-idempotent-r4",
                )
            assert await fingerprint() == before
        finally:
            if handoff_id is not None:
                await source.execute(
                    "DELETE FROM public.handoffs WHERE id=$1", handoff_id
                )
            if decision_id is not None:
                await source.execute(
                    "DELETE FROM public.decisions WHERE id=$1", decision_id
                )
            await source.close()
            await target.close()

    asyncio.run(check())


def test_new_source_table_requires_policy_before_manifest_write() -> None:
    async def check() -> None:
        fixture = await prepare_synthetic_databases()
        source = await asyncpg.connect(SOURCE_URL)
        target = await asyncpg.connect(TARGET_URL)
        snapshot = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        await source.execute(
            "CREATE TABLE public.synthetic_unapproved_source (id uuid PRIMARY KEY)"
        )
        try:
            with pytest.raises(ValueError, match="missing or extra source entity"):
                await convert_snapshot(
                    source, target, synthetic_policy(fixture),
                    snapshot_sha256=snapshot,
                    converter_revision="synthetic-policy-gate-r4",
                )
            assert await target.fetchval(
                "SELECT count(*) FROM cortex_conversion.runs "
                "WHERE snapshot_sha256=$1", snapshot,
            ) == 0
        finally:
            await source.execute("DROP TABLE public.synthetic_unapproved_source")
            await source.close()
            await target.close()

    asyncio.run(check())
