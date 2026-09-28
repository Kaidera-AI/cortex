"""Unit tests for deterministic harness mirror generation, drift detection and
rollback planning (R06)."""

from __future__ import annotations

import hashlib
import json
import uuid

from cortex_v2.interface.harness import (
    HARNESS_GENERATOR_VERSION,
    MirrorInputs,
    PersonaInput,
    RuleInput,
    SkillInput,
    classify_drift,
    generate_mirror,
    rollback_plan,
    slugify,
)

PERSONA_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
RULE_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
SKILL_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


def inputs(**overrides) -> MirrorInputs:
    base = dict(
        persona=PersonaInput(
            persona_id=PERSONA_ID,
            revision=2,
            template_version="persona.v1",
            body="You are a Cortex worker.\n",
        ),
        rules=(
            RuleInput(
                rule_id=RULE_ID,
                revision=3,
                slug="no-force-push",
                obligation="mandatory",
                body="Never force-push shared branches.\n",
            ),
            RuleInput(
                rule_id=uuid.UUID("22222222-2222-4222-8222-222222222223"),
                revision=1,
                slug="style-notes",
                obligation="optional",
                body="Prefer small commits.\n",
            ),
        ),
        skills=(
            SkillInput(
                skill_id=SKILL_ID,
                revision=4,
                slug="debugging",
                when_to_use="Use when a test fails unexpectedly.",
                body="# Debugging\nSteps...\n",
                precedence=10,
            ),
        ),
    )
    base.update(overrides)
    return MirrorInputs(**base)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_generation_is_deterministic():
    first = generate_mirror(inputs())
    second = generate_mirror(inputs())
    assert first.manifest_sha256 == second.manifest_sha256
    assert [(f.path, f.body) for f in first.files] == [
        (f.path, f.body) for f in second.files
    ]
    assert first.generator_version == HARNESS_GENERATOR_VERSION


def test_generation_changes_with_input_revisions():
    base = generate_mirror(inputs())
    changed_rule = RuleInput(
        rule_id=RULE_ID,
        revision=4,
        slug="no-force-push",
        obligation="mandatory",
        body="Never force-push. Ever.\n",
    )
    changed = generate_mirror(
        inputs(rules=(changed_rule, *inputs().rules[1:]))
    )
    assert changed.manifest_sha256 != base.manifest_sha256
    assert changed.input_provenance != base.input_provenance


def test_generated_file_layout_and_provenance():
    mirror = generate_mirror(inputs())
    paths = {file.path for file in mirror.files}
    assert "CONTEXT_MANIFEST.json" in paths
    assert "persona.md" in paths
    assert "rules/mandatory/no-force-push.md" in paths
    assert "rules/optional/style-notes.md" in paths
    assert "skills/INDEX.json" in paths
    # On-demand skills: index only, no skill bodies in the mirror.
    assert not any(p.startswith("skills/") and p.endswith(".md") for p in paths)
    for file in mirror.files:
        assert file.content_sha256 == sha(file.body)
    manifest = next(f for f in mirror.files if f.path == "CONTEXT_MANIFEST.json")
    data = json.loads(manifest.body)
    assert data["generator_version"] == HARNESS_GENERATOR_VERSION
    assert "files_sha256" in data
    provenance = mirror.input_provenance
    assert provenance["persona"]["revision"] == 2
    assert {r["slug"]: r["revision"] for r in provenance["rules"]} == {
        "no-force-push": 3,
        "style-notes": 1,
    }
    assert provenance["skills"][0]["revision"] == 4
    index = json.loads(
        next(f for f in mirror.files if f.path == "skills/INDEX.json").body
    )
    assert index["skills"][0]["slug"] == "debugging"
    assert index["skills"][0]["fetch"] == {
        "operation_id": "skill.get",
        "arguments_hint": {"skill_id": str(SKILL_ID), "revision": 4},
    }


def test_manifest_hash_covers_every_file():
    mirror = generate_mirror(inputs())
    listing = {f.path: f.content_sha256 for f in mirror.files}
    payload = json.dumps(
        sorted(
            (path, digest)
            for path, digest in listing.items()
            if path != "CONTEXT_MANIFEST.json"
        ),
        separators=(",", ":"),
    ).encode("utf-8")
    manifest = next(f for f in mirror.files if f.path == "CONTEXT_MANIFEST.json")
    body = json.loads(manifest.body)
    assert body["files_sha256"] == hashlib.sha256(payload).hexdigest()
    assert mirror.manifest_sha256 == sha(
        "".join(f"{p}:{listing[p]}\n" for p in sorted(listing))
    )


def test_empty_inputs_still_generate_manifest():
    mirror = generate_mirror(inputs(persona=None, rules=(), skills=()))
    paths = {f.path for f in mirror.files}
    assert paths == {"CONTEXT_MANIFEST.json"}


def test_classify_drift_states():
    expected = {"a.md": sha("a"), "b.md": sha("b"), "c.md": sha("c")}
    observed = {"a.md": sha("a"), "b.md": sha("hand edit"), "d.md": sha("d")}
    report = classify_drift(expected, observed)
    assert report["status"] == "drifted"
    states = {f["path"]: f["state"] for f in report["files"]}
    assert states == {
        "a.md": "match",
        "b.md": "hand_edited",
        "c.md": "missing",
        "d.md": "untracked",
    }
    clean = classify_drift(expected, dict(expected))
    assert clean["status"] == "clean"
    assert clean["files"] == []


def test_rollback_plan_restores_and_removes_introduced_files():
    target = {
        "persona.md": ("body-old", sha("body-old")),
        "shared.md": ("same", sha("same")),
    }
    active = {
        "persona.md": ("body-new", sha("body-new")),
        "shared.md": ("same", sha("same")),
        "introduced.md": ("fresh", sha("fresh")),
    }
    plan = rollback_plan(active, target)
    assert [item["path"] for item in plan["restore"]] == ["persona.md"]
    assert plan["restore"][0]["body"] == "body-old"
    assert plan["remove"] == ["introduced.md"]


def test_slugify_is_deterministic_and_safe():
    assert slugify("My Rule!") == "my-rule"
    assert slugify("  spaced   name ") == "spaced-name"
    assert slugify("Ünïcode") == "unicode"
    assert slugify("9lives") == "9lives"
