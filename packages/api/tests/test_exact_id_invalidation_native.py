"""Native exact-ID/prefix invalidation policy over the four retained row types."""

from uuid import uuid4

import asyncpg
import pytest

from test_audit_data_integrity_native import api, run, scratch_conn


@pytest.mark.parametrize("table", ["decisions", "lessons", "handoffs", "work_products"])
def test_exact_id_and_prefix_exclude_invalidated_rows(api, scratch_conn, monkeypatch, table):
    async def case():
        conn = await asyncpg.connect(**scratch_conn)
        try:
            await conn.execute(f"CREATE TABLE {table}(id uuid PRIMARY KEY, project text, summary text, created_at timestamptz DEFAULT now(), invalidated_at timestamptz, status text)")
            current, invalidated, non_current, late_invalidated = (uuid4() for _ in range(4))
            status = "current" if table == "work_products" else "pending"
            await conn.execute(f"INSERT INTO {table}(id,project,summary,status) VALUES($1,'audit','current row',$2),($3,'audit','late invalidation row',$2)", current, status, late_invalidated)
            await conn.execute(f"INSERT INTO {table}(id,project,summary,invalidated_at,status) VALUES($1,'audit','invalidated row',now(),$2)", invalidated, status)
            if table == "work_products":
                await conn.execute(f"INSERT INTO {table}(id,project,summary,status) VALUES($1,'audit','non-current row','stale')", non_current)

            class ScopedIDSearch:
                async def fetchrow(self, statement, *args):
                    if f"FROM {table} WHERE" not in statement:
                        return None
                    if statement.startswith("SELECT summary") and args[0] == str(late_invalidated):
                        await conn.execute(f"UPDATE {table} SET invalidated_at=now() WHERE id=$1", late_invalidated)
                    return await conn.fetchrow(statement, *args)

                async def fetch(self, *_args):
                    return []

                async def execute(self, *_args):
                    return "SET"

            async def no_embedding(*_args):
                return api.SearchProviderOutcome("skipped")

            monkeypatch.setattr(api, "_embed_query_outcome", no_embedding)

            async def matches(row_id, prefix):
                query = str(row_id)[:8] if prefix else str(row_id)
                result = await api.execute_search(ScopedIDSearch(), "audit", query, search_type=table, rerank=False, graph=False)
                return {str(row["id"]) for row in result["results"]}

            for prefix in (False, True):
                assert str(current) in await matches(current, prefix), "current row missing from exact-ID/prefix results"
                assert str(invalidated) not in await matches(invalidated, prefix), "invalidated row leaked from exact-ID/prefix results"
                if table == "work_products":
                    assert str(non_current) not in await matches(non_current, prefix), "non-current work_product leaked from exact-ID/prefix results"
                await conn.execute(f"UPDATE {table} SET invalidated_at=NULL WHERE id=$1", late_invalidated)
                assert str(late_invalidated) not in await matches(late_invalidated, prefix), "row invalidated between ID and content reads leaked"
        finally:
            await conn.close()
    run(case())
