from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi import HTTPException
from pydantic import ValidationError


API_ROOT = Path(__file__).resolve().parents[1]
API_MAIN_PATH = API_ROOT / "main.py"
REPO_ROOT = API_ROOT.parents[1]


def load_api_module():
    spec = importlib.util.spec_from_file_location(
        "cortex_api_main_release_blocker_test",
        API_MAIN_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.path.insert(0, str(API_ROOT))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(API_ROOT))
    return module


def test_handoff_priority_is_validated_before_database_insert():
    api = load_api_module()

    with pytest.raises(ValidationError):
        api.HandoffCreate(
            to_role="lead",
            summary="Invalid priority must never reach the DB check constraint.",
            priority="critical",
        )


def test_project_root_must_be_inside_configured_worker_mount(monkeypatch):
    api = load_api_module()
    monkeypatch.delenv("REGISTERED_PROJECTS_ROOT", raising=False)
    monkeypatch.setenv("HOST_PROJECTS_ROOT", "/srv/projects")

    assert api.validate_visible_project_root("/srv/projects/acme") == "/srv/projects/acme"
    with pytest.raises(HTTPException) as exc:
        api.validate_visible_project_root("/home/operator/acme")

    assert exc.value.status_code == 400
    assert "registered projects root" in str(exc.value.detail)


def test_project_root_accepts_one_normalized_appliance_registry_prefix(monkeypatch):
    api = load_api_module()
    monkeypatch.setenv("HOST_PROJECTS_ROOT", "/projects/")
    monkeypatch.setenv("REGISTERED_PROJECTS_ROOT", "/projects")

    assert api.validate_visible_project_root("/projects/acme") == "/projects/acme"

    monkeypatch.setenv("HOST_PROJECTS_ROOT", "/host/projects")
    with pytest.raises(HTTPException) as exc:
        api.validate_visible_project_root("/projects/acme")
    assert exc.value.status_code == 500
    assert "conflicting" in str(exc.value.detail)


def test_project_root_validation_preserves_unconfigured_legacy_mode(monkeypatch):
    api = load_api_module()
    monkeypatch.delenv("HOST_PROJECTS_ROOT", raising=False)
    monkeypatch.delenv("REGISTERED_PROJECTS_ROOT", raising=False)

    assert api.validate_visible_project_root("/offline/cloud/project") == "/offline/cloud/project"


def test_fresh_schema_and_migrations_cover_runtime_created_graph_table_and_query_indexes():
    schema = (REPO_ROOT / "packages/schema/cortex-schema-full.sql").read_text()
    migrations = REPO_ROOT / "packages/schema/migrations"

    assert "CREATE TABLE public.graph_build_jobs" in schema
    for index in (
        "idx_decisions_project",
        "idx_decisions_agent_id",
        "idx_lessons_project",
        "idx_lessons_agent_id",
        "idx_agent_sessions_project",
    ):
        assert f"CREATE INDEX {index}" in schema
        matching = list(migrations.glob(f"2026-07-19-*-{index.removeprefix('idx_').replace('_', '-')}.sql"))
        if not matching:
            matching = [path for path in migrations.glob("2026-07-19-*.sql") if index in path.read_text()]
        assert len(matching) == 1
        assert "CREATE INDEX CONCURRENTLY" in matching[0].read_text()


def test_project_transfer_remaps_scope_and_uses_named_destination_columns():
    api = load_api_module()
    mapped = api.remap_project_transfer_row(
        {
            "id": "row-1",
            "project": "source",
            "project_id": "00000000-0000-0000-0000-000000000001",
            "legacy_only": "drop-me",
        },
        target_columns={"id", "project", "project_id", "new_defaulted_column"},
        target_project_key="target",
        target_project_id="00000000-0000-0000-0000-000000000002",
    )

    assert mapped == {
        "id": "row-1",
        "project": "target",
        "project_id": "00000000-0000-0000-0000-000000000002",
    }
    sql = api.project_transfer_insert_sql(
        "public", "handoffs", ["id", "project", "project_id"]
    )
    assert 'INSERT INTO "public"."handoffs" ("id", "project", "project_id")' in sql
    assert "jsonb_populate_record" in sql
    assert "COPY" not in sql


def test_project_transfer_orders_fk_parents_before_children():
    api = load_api_module()
    actor = ("public", "cortex_actors")
    alias = ("public", "cortex_actor_aliases")
    unrelated = ("public", "lessons")

    order = api.project_transfer_order(
        {alias, actor, unrelated},
        [(alias, actor)],
    )

    assert order.index(actor) < order.index(alias)


def test_project_transfer_cli_is_api_only_and_integrity_checked(monkeypatch, tmp_path):
    cli = REPO_ROOT / "packages/cli"
    wrapper = (cli / "cortex-export-project").read_text()
    assert any(line.startswith('exec python3 -B ') and '_cortex_export.py' in line
               for line in wrapper.splitlines())
    spec = importlib.util.spec_from_file_location("ledger_export_helper", cli / "_cortex_export.py")
    helper = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(helper)
    payload = json.dumps({"format": "kaidera.cortex-project.v1",
        "source_project": {"project_key": "fixture"}, "table_count": 1, "row_count": 1,
        "tables": [{"schema_name": "public", "table_name": "knowledge", "rows": [{"id": "synthetic"}]}]}).encode()
    observed = []

    def transport(command, _env):
        observed.append(command[-3])
        Path(command[-2]).write_bytes(payload)

    monkeypatch.setattr(helper, "download", transport)
    monkeypatch.setenv("CORTEX_AGENT", "fixture-agent")
    directory = tmp_path.resolve()
    output = directory / "export.json"
    helper.export("fixture", str(output), [])
    assert observed == ["/admin/projects/fixture/export"]
    assert output.read_bytes() == payload
    assert output.with_suffix(".json.sha256").read_text() == hashlib.sha256(payload).hexdigest() + "  export.json\n"

    # Execute exact import bytes with only the API transport isolated. The real
    # sidecar validation, body preparation and endpoint construction still run.
    script = directory / "cortex-import-project"
    script.write_bytes((cli / "cortex-import-project").read_bytes())
    assert script.read_bytes() == (cli / "cortex-import-project").read_bytes()
    (directory / "_cortex_api.sh").write_text('''cortex_api_urlencode_strict() { printf '%s' "$1"; }
cortex_api_call_admin() {
python3 - "$1" "$2" "$CORTEX_API_PAYLOAD_FILE" <<'PY'
import json,sys
from pathlib import Path
print('LEDGER_API='+json.dumps({'method':sys.argv[1],'endpoint':sys.argv[2],'body':json.loads(Path(sys.argv[3]).read_text())}))
PY
}
''')
    env = {"PATH": os.environ["PATH"], "TMPDIR": str(directory), "CORTEX_AGENT": "fixture-agent"}
    result = subprocess.run(["/bin/bash", str(script), "destination", str(output)], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line.removeprefix("LEDGER_API=")) for line in result.stdout.splitlines() if line.startswith("LEDGER_API=")]
    assert len(rows) == 1
    assert rows[0] == {"method": "POST", "endpoint": "/admin/projects/destination/import",
                       "body": {**json.loads(payload), "allow_existing": False}}
    output.write_bytes(payload + b" ")
    refused = subprocess.run(["/bin/bash", str(script), "destination", str(output)], env=env,
                             capture_output=True, text=True, timeout=15)
    assert refused.returncode != 0 and "LEDGER_API=" not in refused.stdout
    for path in (cli / "_cortex_export.py", cli / "cortex-import-project"):
        executable_lines = [line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")]
        for forbidden in ("docker exec", "podman exec", "psql"):
            assert not any(forbidden in line for line in executable_lines)
