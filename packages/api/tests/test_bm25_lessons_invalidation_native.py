"""Native lessons companion to the frozen PR54 decisions invalidation oracle."""

from uuid import uuid4

import asyncpg

from test_audit_data_integrity_native import api, run, scratch_conn


def test_bm25_excludes_invalidated_lessons(api, scratch_conn, monkeypatch):
    async def case():
        conn = await asyncpg.connect(**scratch_conn)
        try:
            await conn.execute("CREATE TABLE lessons(id uuid PRIMARY KEY, project text, summary text, category text, agent_name text, search_vector tsvector, invalidated_at timestamptz)")
            stale, current = uuid4(), uuid4()
            await conn.execute("INSERT INTO lessons VALUES($1,'audit','orion stale lesson',NULL,NULL,to_tsvector('english','orion stale lesson'),now()),($2,'audit','orion current lesson',NULL,NULL,to_tsvector('english','orion current lesson'),NULL)", stale, current)

            class ScopedSearch:
                async def fetch(self, statement, *args):
                    if "FROM lessons" in statement and "search_vector @@" in statement:
                        return await conn.fetch(statement, *args)
                    return []

                async def execute(self, *_args):
                    return "SET"

            async def no_embedding(*_args):
                return api.SearchProviderOutcome("skipped")

            monkeypatch.setattr(api, "_embed_query_outcome", no_embedding)
            result = await api.execute_search(ScopedSearch(), "audit", "orion", search_type="lessons", rerank=False, graph=False)
            assert str(current) in {str(row["id"]) for row in result["results"]}
            assert str(stale) not in {str(row["id"]) for row in result["results"]}
        finally:
            await conn.close()
    run(case())
