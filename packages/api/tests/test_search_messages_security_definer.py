"""Source contracts for the scoped messages BM25 RLS bridge."""

from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "migrations"
    / "2026-08-16-02-messages-bm25-security-definer.sql"
)
REPAIR_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "migrations"
    / "2026-08-31-02-repair-messages-bm25-security-definer.sql"
)


def migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_messages_bm25_bridge_is_fixed_and_fail_closed():
    sql = migration_sql()
    function_body = sql.lower().split("as $function$", 1)[1].split("$function$;", 1)[0]

    assert "SECURITY DEFINER" in sql
    assert "SET search_path TO pg_catalog, pg_temp" in sql
    assert "p_project = NULLIF(" in sql
    assert "pg_catalog.current_setting('cortex.project', true)" in sql
    assert "m.project = p_project" in sql
    assert "FROM public.messages AS m" in sql
    assert "execute " not in function_body
    assert "format(" not in function_body
    assert "quote_ident" not in function_body

    signature = "public.cortex_search_messages_bm25(text, text, text)"
    assert f"ALTER FUNCTION {signature}\n    OWNER TO postgres" in sql
    assert f"REVOKE ALL ON FUNCTION {signature}\n    FROM PUBLIC" in sql
    assert f"GRANT EXECUTE ON FUNCTION {signature}\n    TO cortex_app" in sql


def test_messages_bm25_bridge_preserves_the_existing_retrieval_contract():
    sql = migration_sql()
    ranked_sql, content_fetch_sql = sql.split(
        "    SELECT\n        ranked.id::text AS id,", 1
    )

    assert "WITH ranked_ids AS MATERIALIZED" in ranked_sql
    assert "m.search_vector @@ pg_catalog.plainto_tsquery(" in ranked_sql
    assert "'pg_catalog.english'::pg_catalog.regconfig" in ranked_sql
    assert "pg_catalog.ts_rank_cd(" in ranked_sql
    assert "pg_catalog.left(m.content, 1000) ILIKE '%' || p_room || '%'" in ranked_sql
    assert "ORDER BY score DESC" in ranked_sql
    assert "LIMIT 10" in ranked_sql
    assert "pg_catalog.left(m.content, 300)" not in ranked_sql

    assert "pg_catalog.left(m.content, 300) AS display_text" in content_fetch_sql
    assert "JOIN public.messages AS m ON m.id = ranked.id" in content_fetch_sql
    assert "ORDER BY ranked.score DESC" in content_fetch_sql


def test_messages_bm25_bridge_is_repaired_after_the_appliance_baseline_cutoff():
    original = migration_sql().split("CREATE OR REPLACE FUNCTION", 1)[1]
    repair = REPAIR_MIGRATION.read_text(encoding="utf-8")

    assert REPAIR_MIGRATION.name > "2026-08-18-02-normalize-scalar-metadata.sql"
    assert repair.split("CREATE OR REPLACE FUNCTION", 1)[1] == original
