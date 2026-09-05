"""Unit tests for the PersonaPayload Pydantic model contract.

Tests:
- Models import correctly.
- schema_version defaults to 'cortex.persona.v2'.
- PersonaPayload validates with all required fields.
- PersonaPayload round-trips through JSON cleanly.
- Optional fields default correctly (None / empty lists).
- HarnessAdapter validates correctly.
- SkillManifestEntry validates correctly.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODELS_BOOT_PATH = Path(__file__).resolve().parents[1] / "models" / "boot.py"


def _load_models():
    spec = importlib.util.spec_from_file_location("models_boot_unit", MODELS_BOOT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def models():
    return _load_models()


# ---------------------------------------------------------------------------
# SkillManifestEntry
# ---------------------------------------------------------------------------


def test_skill_manifest_entry_minimal(models):
    entry = models.SkillManifestEntry(skill_slug="foo")
    assert entry.skill_slug == "foo"
    assert entry.name is None
    assert entry.scope == "project"
    assert entry.version == "1"
    assert entry.body_ref is None


def test_skill_manifest_entry_full(models):
    entry = models.SkillManifestEntry(
        skill_slug="cortex-search",
        name="Cortex Search",
        description="Search Cortex memory",
        scope="global",
        permission="read",
        version="2",
        body_ref=".agents/skills/cortex-search.md",
    )
    assert entry.scope == "global"
    assert entry.version == "2"


# ---------------------------------------------------------------------------
# HarnessAdapter
# ---------------------------------------------------------------------------


def test_harness_adapter_minimal(models):
    ha = models.HarnessAdapter(harness="claude-code", entry_file="main.py")
    assert ha.harness == "claude-code"
    assert ha.entry_file == "main.py"
    assert ha.notes is None


def test_harness_adapter_with_notes(models):
    ha = models.HarnessAdapter(harness="codex", entry_file="codex.md", notes="default harness")
    assert ha.notes == "default harness"


# ---------------------------------------------------------------------------
# PersonaPayload
# ---------------------------------------------------------------------------


def test_persona_payload_schema_version_default(models):
    p = models.PersonaPayload(
        project="kaidera-os",
        agent="kai",
        agent_identity="kai@kaidera-os",
        identity_text="You are kai@kaidera-os",
    )
    assert p.schema_version == "cortex.persona.v2"


def test_persona_payload_optional_fields_default(models):
    p = models.PersonaPayload(
        project="kaidera-os",
        agent="ren",
        agent_identity="ren@kaidera-os",
        identity_text="You are ren@kaidera-os",
    )
    assert p.role is None
    assert p.skills == []
    assert p.rules == []
    assert p.pending_handoffs == []
    assert p.harness is None
    assert p.metadata == {}


def test_persona_payload_full(models):
    p = models.PersonaPayload(
        schema_version="cortex.persona.v2",
        project="kaidera-os",
        agent="kai",
        agent_identity="kai@kaidera-os",
        role="full-stack-developer",
        identity_text="You are kai@kaidera-os",
        skills=[
            models.SkillManifestEntry(skill_slug="cortex-search", scope="project"),
        ],
        rules=[{"rule_slug": "hex-discipline", "title": "Hex Discipline", "body": "..."}],
        pending_handoffs=[{"id": "abc", "priority": "high", "summary": "test"}],
        harness=models.HarnessAdapter(harness="claude-code", entry_file="main.py"),
        metadata={"build": "phase-1"},
    )
    assert p.project == "kaidera-os"
    assert len(p.skills) == 1
    assert p.skills[0].skill_slug == "cortex-search"
    assert p.harness is not None
    assert p.harness.harness == "claude-code"
    assert p.metadata["build"] == "phase-1"


def test_persona_payload_round_trips_json(models):
    p = models.PersonaPayload(
        project="kaidera-os",
        agent="kai",
        agent_identity="kai@kaidera-os",
        role="full-stack-developer",
        identity_text="You are kai@kaidera-os, full-stack-developer",
        skills=[models.SkillManifestEntry(skill_slug="s1", name="Skill One")],
        rules=[{"rule_slug": "r1", "title": "Rule One", "body": "body"}],
    )
    serialised = p.model_dump_json()
    recovered = models.PersonaPayload.model_validate_json(serialised)

    assert recovered.schema_version == "cortex.persona.v2"
    assert recovered.agent == "kai"
    assert len(recovered.skills) == 1
    assert recovered.skills[0].name == "Skill One"
    assert recovered.rules[0]["rule_slug"] == "r1"


def test_persona_payload_model_dump_is_json_serialisable(models):
    p = models.PersonaPayload(
        project="kaidera-os",
        agent="kai",
        agent_identity="kai@kaidera-os",
        identity_text="You are kai@kaidera-os",
    )
    dumped = p.model_dump()
    # Must be JSON serialisable without error
    roundtripped = json.loads(json.dumps(dumped))
    assert roundtripped["schema_version"] == "cortex.persona.v2"


def test_persona_payload_validate_alias_schema_version(models):
    """Explicitly setting schema_version to a non-default value is preserved."""
    p = models.PersonaPayload.model_validate(
        {
            "schema_version": "cortex.persona.v99",
            "project": "kaidera-os",
            "agent": "kai",
            "agent_identity": "kai@kaidera-os",
            "identity_text": "You are kai@kaidera-os",
        }
    )
    assert p.schema_version == "cortex.persona.v99"
