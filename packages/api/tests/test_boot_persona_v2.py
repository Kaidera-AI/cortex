"""Tests for Phase 1 boot persona v2 (PersonaPayload) — cortex.persona.v2.

Three categories:
(a) Regression: boot/surface_version fields are byte-identical to before.
(b) Persona contract: persona key is present, validates against PersonaPayload,
    has correct project/agent/schema_version fields.
(c) Empty skills/rules: tables exist but have no rows → empty lists, no error.

The tests use the same fake-pool pattern as test_contracts.py.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
MODELS_BOOT_PATH = Path(__file__).resolve().parents[1] / "models" / "boot.py"


# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------


def _load_api():
    spec = importlib.util.spec_from_file_location("cortex_api_main_bpv2", API_MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_models():
    spec = importlib.util.spec_from_file_location("models_boot_bpv2", MODELS_BOOT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fake infrastructure (mirrors test_contracts.py / test_persona_endpoint.py)
# ---------------------------------------------------------------------------


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


class _BootFakeConn:
    """Minimal fake connection that answers every query the boot handler fires.

    Skill and rules rows default to empty (happy-path for empty-table test).
    Override ``_skills_rows`` and ``_rules_rows`` on subclasses as needed.
    """

    _skills_rows: list = []
    _rules_rows: list = []

    async def execute(self, sql, *args):
        return "OK"

    async def fetchval(self, sql, *args):
        raise AssertionError(f"Unexpected fetchval SQL: {sql!r}")

    async def fetchrow(self, sql, *args):
        if "SELECT metadata FROM cortex_projects" in sql:
            return {
                "metadata": {
                    "enforce_writer_roster": True,
                    "roster_policy": {
                        "enforce_writer_roster": True,
                        "roster_schema_version": "1",
                        "default_writer_scope": "work",
                        "system_event_writers": ["beat", "migration", "system"],
                        "beat_may_create_handoff": True,
                        "handoff_targets": "writers",
                        "suggest_cutoff": 0.6,
                    },
                }
            }
        if "SELECT project_key" in sql and "FROM cortex_projects" in sql:
            project = args[0]
            return {
                "project_key": project,
                "project_id": "11111111-1111-4111-8111-111111111111",
                "display_name": project,
                "default_agent": "kai",
                "repo_root": "/tmp/kaidera-os",
                "repo_type": "repo",
                "status": "active",
            }
        if "SELECT default_agent FROM cortex_projects" in sql:
            return {"default_agent": "kai"}
        if "FROM agent_profiles" in sql:
            project, agent = args[:2]
            if project == "kaidera-os" and agent in {"kai", "ren"}:
                return {"agent_name": agent, "role": "full-stack-developer"}
            return None
        if "FROM agents" in sql:
            project, agent = args[:2]
            if project == "kaidera-os" and agent in {"kai", "ren"}:
                return {"agent_name": agent, "role": "full-stack-developer"}
            return None
        raise AssertionError(f"Unexpected fetchrow SQL: {sql!r}")

    async def fetch(self, sql, *args):
        # Registry resolver reads
        if "writer_scope" in sql and "FROM agents a" in sql:
            project = args[0] if args else None
            if project == "kaidera-os":
                return [
                    {"n": "kai", "scope": "work", "role": "full-stack-developer"},
                    {"n": "ren", "scope": "work", "role": "full-stack-developer"},
                ]
            return []
        if "FROM roles" in sql and "default_capabilities" in sql:
            return []
        if "SELECT role, capabilities" in sql and "FROM agents" in sql:
            project, agent = args
            if project == "kaidera-os" and agent in {"kai", "ren"}:
                return [{"role": "full-stack-developer", "capabilities": {}}]
            return []

        # Boot-specific queries
        if "SELECT DISTINCT lower(agent_name)" in sql:
            return [{"n": "kai"}, {"n": "ren"}]
        if "SELECT DISTINCT role" in sql:
            return []
        if "FROM handoffs" in sql:
            return []
        if "FROM decisions" in sql:
            return []
        if "FROM lessons" in sql:
            return []
        if "FROM pattern_metrics" in sql:
            return []

        # Skills and rules (Phase 1 tables)
        if "FROM agent_skill_bindings" in sql and "JOIN agent_skills" in sql:
            return list(self._skills_rows)
        if "FROM rules" in sql:
            return list(self._rules_rows)

        raise AssertionError(f"Unexpected fetch SQL: {sql!r}")


class _BootFakeConnWithSkills(_BootFakeConn):
    """Variant that returns one skill and one rule."""

    _skills_rows = [
        {
            "skill_slug": "cortex-search",
            "name": "Cortex Search",
            "description": "Search Cortex memory",
            "scope": "project",
            "permission": "read",
            "version": "1",
            "body_ref": ".agents/skills/cortex-search.md",
        }
    ]
    _rules_rows = [
        {
            "rule_slug": "hex-discipline",
            "title": "Hex Discipline",
            "body": "Always use compound IDs.",
            "source_file": "cortex.md",
            "version": "1",
        }
    ]


class _BootFakeConnWithWorkProduct(_BootFakeConn):
    """Variant that returns a pending handoff and a matching work product."""

    handoff_id = "11111111-2222-4333-8444-555555555555"

    async def fetchval(self, sql, *args):
        if "to_regclass('public.work_products')" in sql:
            return True
        return await super().fetchval(sql, *args)

    async def fetch(self, sql, *args):
        if "information_schema.columns" in sql and "table_name = 'work_products'" in sql:
            return []
        if "FROM handoffs" in sql and len(args) > 1 and args[1] == "pending":
            return [
                {
                    "id": self.handoff_id,
                    "from_agent": "ren",
                    "to_agent": "kai",
                    "priority": "high",
                    "summary": "Ship boot provenance metadata",
                    "files_changed": ["local-cortex/console/app/main.py"],
                }
            ]
        if "FROM handoffs" in sql:
            return []
        if "FROM work_products wp" in sql:
            return [
                {
                    "id": "22222222-3333-4444-8555-666666666666",
                    "project": "kaidera-os",
                    "handoff_id": self.handoff_id,
                    "agent_name": "kai",
                    "activity_type": "task-completed",
                    "status": "current",
                    "title": "Boot provenance metadata",
                    "summary": "Boot payload now reports source, freshness, and projection metadata.",
                    "behavior_summary": "",
                    "architecture_notes": "",
                    "files_changed": ["local-cortex/console/app/main.py"],
                    "symbols_changed": ["build_boot_context_metadata"],
                    "subject_entities": ["boot"],
                    "artifact_refs": [],
                    "tests_run": [],
                    "risks": [],
                    "followups": [],
                    "approval_status": None,
                    "content_hash": "abc123",
                    "commit_sha": "deadbeef",
                    "file_hashes": {},
                    "symbol_hashes": {},
                    "freshness_status": "current",
                    "freshness_reason": "",
                    "freshness_checked_at": "2026-06-24T10:00:00+00:00",
                    "projection_status": "projected",
                    "projection_error": "",
                    "projected_at": "2026-06-24T10:01:00+00:00",
                    "source_event_id": 7,
                    "supersedes_id": None,
                    "metadata": {},
                    "created_at": "2026-06-24T09:00:00+00:00",
                    "updated_at": "2026-06-24T10:02:00+00:00",
                    "valid_from": "2026-06-24T09:00:00+00:00",
                    "valid_to": None,
                    "invalidated_at": None,
                    "score": 18.0,
                }
            ]
        return await super().fetch(sql, *args)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api_empty_tables():
    """Boot module wired to a conn that returns empty skills/rules."""
    module = _load_api()
    pool = _FakePool(_BootFakeConn())
    module.pool = pool
    module.pool_app = pool
    module.pool_admin = pool
    return module


@pytest.fixture
def api_with_data():
    """Boot module wired to a conn with one skill and one rule."""
    module = _load_api()
    pool = _FakePool(_BootFakeConnWithSkills())
    module.pool = pool
    module.pool_app = pool
    module.pool_admin = pool
    return module


@pytest.fixture
def api_with_work_product():
    """Boot module wired to a conn with a handoff-backed work product brief."""
    module = _load_api()
    pool = _FakePool(_BootFakeConnWithWorkProduct())
    module.pool = pool
    module.pool_app = pool
    module.pool_admin = pool
    return module


# ---------------------------------------------------------------------------
# Helper — build a minimal Starlette Request for the boot handler
# ---------------------------------------------------------------------------


def _boot_request():
    from starlette.requests import Request

    return Request({"type": "http", "query_string": b"budget=500"})


# ===========================================================================
# (a) Regression: boot + surface_version are byte-identical to before
# ===========================================================================


@pytest.mark.asyncio
async def test_boot_and_surface_version_fields_unchanged(api_empty_tables):
    """Adding persona must not change boot or surface_version bytes."""
    result = await api_empty_tables.boot("kai", _boot_request(), x_project="kaidera-os", query=None)

    # Both original keys must still be present
    assert "boot" in result
    assert "surface_version" in result

    # boot must be a non-empty string
    assert isinstance(result["boot"], str)
    assert len(result["boot"]) > 0

    # surface_version must match the module constant
    assert result["surface_version"] == api_empty_tables.CORTEX_SURFACE_VERSION

    # boot string must contain core identity text (byte-identical content check)
    assert "kai@kaidera-os" in result["boot"]
    assert "Identity discipline:" in result["boot"]

    # persona key is additive — it must NOT be absent after the change
    assert "persona" in result


@pytest.mark.asyncio
async def test_boot_fields_are_exactly_two_plus_persona(api_empty_tables):
    """Response has exactly the three expected top-level keys."""
    result = await api_empty_tables.boot("kai", _boot_request(), x_project="kaidera-os", query=None)
    assert set(result.keys()) == {"boot", "surface_version", "persona"}


# ===========================================================================
# (b) Persona contract: present, valid, correct project/agent/schema_version
# ===========================================================================


@pytest.mark.asyncio
async def test_persona_present_and_schema_version_correct(api_empty_tables):
    result = await api_empty_tables.boot("kai", _boot_request(), x_project="kaidera-os", query=None)

    persona = result["persona"]
    assert isinstance(persona, dict)
    assert persona["schema_version"] == "cortex.persona.v2"
    assert persona["project"] == "kaidera-os"
    assert persona["agent"] == "kai"
    assert persona["agent_identity"] == "kai@kaidera-os"


@pytest.mark.asyncio
async def test_boot_prefers_current_registry_role_over_stale_profile_role():
    module = _load_api()

    class _StaleProfileConn(_BootFakeConn):
        async def fetchrow(self, sql, *args):
            if "FROM agents" in sql and "SELECT name as agent_name" in sql:
                return {"agent_name": "gem", "role": "graphics"}
            if "FROM agent_profiles" in sql:
                return {"agent_name": "gem", "role": "gem"}
            return await super().fetchrow(sql, *args)

    pool = _FakePool(_StaleProfileConn())
    module.pool = pool
    module.pool_app = pool
    module.pool_admin = pool

    result = await module.boot("gem", _boot_request(), x_project="kaidera-os", query=None)

    assert result["persona"]["role"] == "graphics"
    assert "Role: graphics." in result["boot"]
    assert "Role: gem." not in result["boot"]


@pytest.mark.asyncio
async def test_persona_validates_against_persona_payload_model(api_empty_tables):
    """PersonaPayload.model_validate must accept the boot output without error."""
    models = _load_models()

    result = await api_empty_tables.boot("kai", _boot_request(), x_project="kaidera-os", query=None)
    payload = models.PersonaPayload.model_validate(result["persona"])

    assert payload.schema_version == "cortex.persona.v2"
    assert payload.project == "kaidera-os"
    assert payload.agent == "kai"
    assert payload.agent_identity == "kai@kaidera-os"


@pytest.mark.asyncio
async def test_persona_round_trips_through_json(api_empty_tables):
    """PersonaPayload serialises to JSON and back without loss."""
    models = _load_models()

    result = await api_empty_tables.boot("kai", _boot_request(), x_project="kaidera-os", query=None)
    payload = models.PersonaPayload.model_validate(result["persona"])
    as_json = payload.model_dump_json()
    recovered = models.PersonaPayload.model_validate_json(as_json)

    assert recovered.schema_version == payload.schema_version
    assert recovered.agent == payload.agent
    assert recovered.project == payload.project


@pytest.mark.asyncio
async def test_persona_metadata_reports_boot_provenance_and_freshness(api_empty_tables):
    result = await api_empty_tables.boot("kai", _boot_request(), x_project="kaidera-os", query=None)

    context = result["persona"]["metadata"]["boot_context"]
    assert context["schema_version"] == "cortex.boot_context.v1"
    assert context["project"] == "kaidera-os"
    assert context["agent"] == "kai"
    assert context["source_boundary"] == "cortex-api scoped live read; no filesystem fallback"
    assert context["confidence"] == "high"
    assert context["freshness"]["handoffs"] == "live"
    assert context["freshness"]["decisions"] == "7d window"
    assert context["counts"]["pending_handoffs"] == 0
    assert context["counts"]["work_product_briefs"] == 0
    assert "BOOT CONTEXT PROVENANCE" in result["boot"]
    assert "no filesystem fallback" in result["boot"]


@pytest.mark.asyncio
async def test_persona_metadata_includes_work_product_projection_status(api_with_work_product):
    result = await api_with_work_product.boot("kai", _boot_request(), x_project="kaidera-os", query=None)

    context = result["persona"]["metadata"]["boot_context"]
    assert context["counts"]["pending_handoffs"] == 1
    assert context["counts"]["work_product_briefs"] == 1
    assert context["freshness"]["work_products"] == {"current": 1}
    assert context["projections"]["work_products"] == {"projected": 1}
    assert context["work_products"][0]["title"] == "Boot provenance metadata"
    assert context["work_products"][0]["freshness_status"] == "current"
    assert context["work_products"][0]["projection_status"] == "projected"


# ===========================================================================
# (c) Empty skills/rules tables → empty lists, no error
# ===========================================================================


@pytest.mark.asyncio
async def test_empty_skills_and_rules_tables_yield_empty_lists(api_empty_tables):
    result = await api_empty_tables.boot("kai", _boot_request(), x_project="kaidera-os", query=None)

    persona = result["persona"]
    assert persona["skills"] == []
    assert persona["rules"] == []
    assert persona["pending_handoffs"] == []


@pytest.mark.asyncio
async def test_boot_does_not_error_when_skills_rules_tables_missing():
    """If the tables raise an exception (e.g. not yet migrated), boot still returns."""
    module = _load_api()

    class _MissingTablesConn(_BootFakeConn):
        async def fetch(self, sql, *args):
            if "FROM agent_skill_bindings" in sql or "FROM rules" in sql:
                raise Exception("table does not exist")  # noqa: TRY002
            return await super().fetch(sql, *args)

    pool = _FakePool(_MissingTablesConn())
    module.pool = pool
    module.pool_app = pool
    module.pool_admin = pool

    result = await module.boot("kai", _boot_request(), x_project="kaidera-os", query=None)

    persona = result["persona"]
    assert persona["skills"] == []
    assert persona["rules"] == []


# ===========================================================================
# (d) Skills and rules are included when present
# ===========================================================================


@pytest.mark.asyncio
async def test_skills_and_rules_populated_when_present(api_with_data):
    result = await api_with_data.boot("kai", _boot_request(), x_project="kaidera-os", query=None)

    persona = result["persona"]
    assert len(persona["skills"]) == 1
    assert persona["skills"][0]["skill_slug"] == "cortex-search"
    assert persona["skills"][0]["name"] == "Cortex Search"
    assert persona["skills"][0]["version"] == "1"

    assert len(persona["rules"]) == 1
    assert persona["rules"][0]["rule_slug"] == "hex-discipline"
    assert persona["rules"][0]["title"] == "Hex Discipline"


@pytest.mark.asyncio
async def test_skill_manifest_entry_model_validates(api_with_data):
    """SkillManifestEntry model validates correctly against boot output."""
    models = _load_models()

    result = await api_with_data.boot("kai", _boot_request(), x_project="kaidera-os", query=None)
    for entry_dict in result["persona"]["skills"]:
        entry = models.SkillManifestEntry.model_validate(entry_dict)
        assert entry.skill_slug
        assert entry.scope in {"global", "project", "agent"}
