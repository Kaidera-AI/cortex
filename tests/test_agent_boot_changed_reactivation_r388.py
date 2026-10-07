"""R388 actual-projector changed-reference vectors; original rollback cases stay.

Old canonical1 remains a stale sentinel. New head3 explicitly pins canonical3,
whose exact identity/body/manifest and hash/reference must be observed.
"""
import copy

import pytest

from boot_r374_fixture import native_expected, required, seed, sha


@pytest.mark.parametrize("stream", ("agent", "entry", "publication"))
def test_actual_reactivation_head3_selects_changed_canonical3_and_never_stale1(stream):
    module = required()
    snapshot, original = seed()
    row_key = {"agent": "agent_rows", "entry": "entry_rows", "publication": "publication_rows"}[stream]
    first = snapshot[row_key][0]
    retired = copy.deepcopy(first)
    retired.update(revision=2, state="retired", source_reference="PUBLIC retired head2")
    latest = copy.deepcopy(first)
    latest.update(revision=3, state="active", source_reference="PUBLIC explicit changed-content head3")
    snapshot[row_key] += [retired, latest]
    expected_original = copy.deepcopy(original)
    selected_rules = list(snapshot["rule_rows"])
    selected_skills = list(snapshot["skill_rows"])
    expected_bindings = list(snapshot["entry_rows"])
    expected_publications = list(snapshot["publication_rows"])
    expected_agent = None

    if stream == "agent":
        old_canonical = snapshot["persona_rows"][0]
        new_canonical = copy.deepcopy(old_canonical)
        changed_role = "lead-v3"
        new_identity = "You are kai@helix, an agent on the helix project. Role: lead-v3."
        snapshot["context"].update(actor_role=changed_role, functional_roles=(changed_role,))
        latest.update(persona_revision=3, functional_roles=[changed_role])
        new_canonical.update(revision=3, body=new_identity)
        new_canonical["boot_manifest"].update(version="3", functional_roles=[changed_role],
            identity_text=new_identity, description="PUBLIC changed canonical persona3")
        snapshot["persona_rows"].append(new_canonical)
        expected_original["persona"].update(role=changed_role, identity_text=new_identity)
        old_identity, tail = original["boot"].split("\n", 1)
        assert old_identity == old_canonical["boot_manifest"]["identity_text"]
        expected_original["boot"] = new_identity + "\n" + tail
        expected_agent = latest
    elif stream == "entry":
        old_canonical = next(r for r in snapshot["rule_rows"] if r["rule_id"] == first["rule_id"])
        new_canonical = copy.deepcopy(old_canonical)
        new_canonical.update(revision=3, body=old_canonical["body"] + "\nPUBLIC changed canonical rule3\n")
        new_canonical["boot_manifest"].update(version="3", title="PUBLIC changed canonical rule title3")
        snapshot["rule_rows"].append(new_canonical)
        latest["bound_revision"] = 3
        selected_rules = [new_canonical if r["rule_id"] == first["rule_id"] else r for r in selected_rules]
        expected_bindings = [b for b in expected_bindings if b["binding_id"] != first["binding_id"]] + [latest]
    else:
        old_canonical = next(r for r in snapshot["skill_rows"] if r["skill_id"] == first["entry_id"])
        new_canonical = copy.deepcopy(old_canonical)
        new_canonical.update(revision=3, body="PUBLIC distinct enacted canonical skill body3\n")
        new_canonical["boot_manifest"].update(version="3", name="PUBLIC changed canonical skill name3",
            description="PUBLIC changed canonical skill description3")
        snapshot["skill_rows"].append(new_canonical)
        latest["entry_revision"] = 3
        selected_skills = [new_canonical if r["skill_id"] == first["entry_id"] else r for r in selected_skills]
        expected_publications = [p for p in expected_publications if p["publication_id"] != first["publication_id"]] + [latest]

    before = copy.deepcopy(snapshot)
    expected = native_expected(snapshot, expected_original, selected_rules=selected_rules,
        selected_skills=selected_skills, agent_binding=expected_agent,
        expected_bindings=expected_bindings, expected_publications=expected_publications)
    result = module.project_boot_response(snapshot, budget=1200, query=None, full=False)
    assert result == expected and snapshot == before
    proof = result["persona"]["metadata"]["boot_validation"]
    # Both records are still available: success cannot be a fixture replacement.
    assert old_canonical["revision"] == 1 and new_canonical["revision"] == 3
    assert old_canonical["body"] != new_canonical["body"]
    if stream == "agent":
        assert proof["agent_binding_revision"] == 3 and proof["persona_ref"]["revision"] == 3
        assert proof["persona_ref"]["persona_id"] == str(new_canonical["persona_id"])
        assert result["persona"]["identity_text"] == new_canonical["boot_manifest"]["identity_text"]
        assert result["persona"]["role"] == "lead-v3" and result["boot"].startswith(new_canonical["body"] + "\n")
        assert result["persona"]["identity_text"] != old_canonical["boot_manifest"]["identity_text"]
        reference = next(s for s in result["persona"]["metadata"]["boot_context"]["sources"] if s["section"] == "persona")
        assert reference["revision"] == 3 and reference["persona_id"] == str(new_canonical["persona_id"])
    else:
        kind = "rule" if stream == "entry" else "skill"
        entity_id = new_canonical[kind + "_id"]
        pin = next(p for p in proof["body_pins"] if p["entry_kind"] == kind and p["entry_id"] == str(entity_id))
        assert pin["revision"] == 3 and pin["body_sha256"] == sha(new_canonical["body"])
        assert pin["body_sha256"] != sha(old_canonical["body"])
        ref_key = "binding_ref" if stream == "entry" else "publication_ref"
        id_key = "binding_id" if stream == "entry" else "publication_id"
        assert pin[ref_key] == {id_key: str(latest[id_key]), "revision": 3}
        if stream == "entry":
            projected = next(r for r in result["persona"]["rules"] if r["rule_slug"] == new_canonical["slug"])
            assert projected["body"] == new_canonical["body"] and projected["body"] != old_canonical["body"]
            assert projected["title"] == "PUBLIC changed canonical rule title3" and projected["version"] == "3"
        else:
            projected = next(s for s in result["persona"]["skills"] if s["skill_slug"] == new_canonical["slug"])
            assert projected["name"] == "PUBLIC changed canonical skill name3"
            assert projected["description"] == "PUBLIC changed canonical skill description3" and projected["version"] == "3"
