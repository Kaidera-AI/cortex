"""Source contracts for the scoped decisions trigram RLS bridge."""

from pathlib import Path


MIGRATION_ROOT = Path(__file__).resolve().parents[2] / "data" / "migrations"
MIGRATION = MIGRATION_ROOT / "2026-08-04-01-decisions-trigram-security-definer.sql"
REPAIR_MIGRATION = (
    MIGRATION_ROOT / "2026-08-31-03-repair-decisions-trigram-security-definer.sql"
)


def test_decisions_trigram_bridge_is_repaired_after_the_appliance_baseline_cutoff():
    original = MIGRATION.read_text(encoding="utf-8").split(
        "CREATE OR REPLACE FUNCTION", 1
    )[1]
    repair = REPAIR_MIGRATION.read_text(encoding="utf-8")

    assert REPAIR_MIGRATION.name > "2026-08-18-02-normalize-scalar-metadata.sql"
    assert repair.split("CREATE OR REPLACE FUNCTION", 1)[1] == original


def test_decisions_trigram_bridge_remains_fixed_and_fail_closed():
    sql = REPAIR_MIGRATION.read_text(encoding="utf-8")
    function_body = sql.lower().split("as $function$", 1)[1].split("$function$;", 1)[0]

    assert "SECURITY DEFINER" in sql
    assert "SET search_path TO pg_catalog, public, pg_temp" in sql
    assert "p_project = current_setting('cortex.project', true)" in sql
    assert "d.project = p_project" in sql
    assert "FROM public.decisions AS d" in sql
    assert "execute " not in function_body
    assert "format(" not in function_body
    assert "quote_ident" not in function_body

    signature = "public.cortex_search_decisions_trigram(text, text, text, integer)"
    assert f"ALTER FUNCTION {signature}\n    OWNER TO postgres" in sql
    assert f"REVOKE ALL ON FUNCTION {signature}\n    FROM PUBLIC" in sql
    assert f"GRANT EXECUTE ON FUNCTION {signature}\n    TO cortex_app" in sql
