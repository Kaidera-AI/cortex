"""Nine original packet oracles plus only r379's explicitly ratified deltas."""
import copy

import pytest

from cortex_v2.store import ApiProblem
from boot_r374_fixture import CASES, native_expected, packet, required, seed, uid


@pytest.mark.parametrize("agent,variant,options", CASES)
def test_all_nine_frozen_persona_fields_and_ratified_native_envelope(agent, variant, options):
    module = required()
    snapshot, original = seed(agent, variant)
    before = copy.deepcopy(snapshot)
    budget = int(options[options.index("--budget") + 1]) if "--budget" in options else 1200
    query = "release" if "--query" in options else None
    expected = native_expected(snapshot, original)
    result = module.project_boot_response(snapshot, budget=budget, query=query, full="--full" in options)
    assert result == expected
    assert snapshot == before
    # No normalization/discarding of the old operational values or timestamp.
    for key in ("counts", "freshness", "projections", "work_products", "generated_at"):
        assert result["persona"]["metadata"]["boot_context"][key] == original["persona"]["metadata"]["boot_context"][key]
    assert result["surface_version"] == original["surface_version"]


@pytest.mark.parametrize("agent_priority,role_priority,winner", [(1, 99, "role"), (199, 99, "agent"), (10, 10, "role")])
def test_legacy_project_priority_version_countermodel_has_no_subject_specificity(agent_priority, role_priority, winner):
    module = required()
    snapshot, original = seed()
    global_row = snapshot["skill_rows"][0]
    project_id = uid("PUBLIC project duplicate skill")
    project_rows = []
    bindings = []
    for subject, version, priority in (("agent", 1, agent_priority), ("functional_role", 2, role_priority)):
        row = copy.deepcopy(global_row)
        row.update(scope_id=snapshot["context"]["project_scope_id"], skill_id=project_id,
                   revision=version, body=f"PUBLIC exact project canonical version{version}\n")
        row["boot_manifest"].update(scope="project", name="PUBLIC project " + subject, version=str(version))
        project_rows.append(row)
        bindings.append({"project_scope_id": row["scope_id"], "binding_id": uid("project-" + subject),
            "revision": 1, "state": "active", "subject_kind": subject,
            "actor_id": snapshot["context"]["actor_id"] if subject == "agent" else None,
            "role_slug": snapshot["context"]["actor_role"] if subject != "agent" else None,
            "entry_kind": "skill", "entry_scope_id": row["scope_id"], "skill_id": project_id,
            "rule_id": None, "bound_revision": version, "priority": priority,
            "source_reference": "PUBLIC actual legacy ordering countermodel"})
    snapshot["skill_rows"] += project_rows
    snapshot["entry_rows"] += bindings
    chosen = project_rows[1 if winner == "role" else 0]
    expected_skills = [chosen, *snapshot["skill_rows"][1:-2]]
    expected = native_expected(snapshot, original, selected_skills=expected_skills)
    result = module.project_boot_response(snapshot, budget=1200, query=None, full=False)
    assert result == expected
    selected = next(s for s in result["persona"]["skills"] if s["skill_slug"] == global_row["slug"])
    assert selected["scope"] == "project" and selected["version"] == ("2" if winner == "role" else "1")
    pin = next(p for p in result["persona"]["metadata"]["boot_validation"]["body_pins"]
               if p["entry_kind"] == "skill" and p["slug"] == global_row["slug"])
    assert pin["publication_ref"] is None
    assert pin["binding_ref"]["binding_id"] == str(bindings[1 if winner == "role" else 0]["binding_id"])


@pytest.mark.parametrize("kind", ("persona", "skill", "rule"))
def test_required_missing_manifest_never_fabricates_metadata_or_empty_success(kind):
    module = required()
    snapshot, _ = seed()
    snapshot[kind + "_rows"][0]["boot_manifest"] = None
    with pytest.raises((ApiProblem, ValueError)):
        module.project_boot_response(snapshot, budget=1200, query=None, full=False)


@pytest.mark.parametrize("case", ("actor", "scope", "registered-name"))
def test_trusted_binding_scope_actor_name_cannot_be_borrowed(case):
    module = required()
    snapshot, _ = seed()
    snapshot["context"][{"actor": "actor_id", "scope": "project_scope_id", "registered-name": "actor_name"}[case]] = (
        "PUBLIC-other-name" if case == "registered-name" else uid("other-" + case)
    )
    with pytest.raises((ApiProblem, ValueError)):
        module.project_boot_response(snapshot, budget=1200, query=None, full=False)


def test_global_metadata_label_without_owner_publication_never_grants_visibility():
    module = required()
    snapshot, original = seed()
    snapshot["publication_rows"] = []
    for row in snapshot["skill_rows"]:
        row["boot_manifest"]["permission"] = "PUBLIC metadata cannot grant global read"
    expected = native_expected(snapshot, original, selected_skills=[], expected_publications=[])
    assert module.project_boot_response(snapshot, budget=1200, query=None, full=False) == expected


def test_body_proof_is_not_copied_from_operational_input_or_local_files():
    module = required()
    snapshot, original = seed()
    snapshot["operational"]["boot_validation"] = {"workspace_root": "/PUBLIC/foreign-root", "body_pins": []}
    try:
        result = module.project_boot_response(snapshot, budget=1200, query=None, full=False)
    except (ApiProblem, ValueError):
        return  # Strict rejection of unexpected provider input is also safe.
    assert result == native_expected(snapshot, original)
    assert result["persona"]["metadata"]["boot_validation"]["body_pins"]


@pytest.mark.parametrize("stream", ("agent", "entry", "publication"))
@pytest.mark.parametrize("reactivated", (False, True))
def test_actual_projection_uses_the_same_retired_or_reactivated_heads(stream, reactivated):
    module = required()
    snapshot, original = seed()
    key = {"agent": "agent_rows", "entry": "entry_rows", "publication": "publication_rows"}[stream]
    first = snapshot[key][0]
    retired = copy.deepcopy(first)
    retired.update(revision=2, state="retired")
    snapshot[key].append(retired)
    latest = None
    if reactivated:
        latest = copy.deepcopy(first)
        latest.update(revision=3, state="active", source_reference="PUBLIC explicit reactivation3")
        snapshot[key].append(latest)
    if stream == "agent" and not reactivated:
        with pytest.raises((ApiProblem, ValueError)):
            module.project_boot_response(snapshot, budget=1200, query=None, full=False)
        return
    rules = snapshot["rule_rows"]
    skills = snapshot["skill_rows"]
    binds = [b for b in snapshot["entry_rows"] if b["revision"] == 1]
    pubs = [p for p in snapshot["publication_rows"] if p["revision"] == 1]
    if stream == "entry":
        binds = [b for b in binds if b["binding_id"] != first["binding_id"]]
        if latest is None:
            rules = [r for r in rules if r["rule_id"] != first["rule_id"]]
        else:
            binds.append(latest)
    elif stream == "publication":
        pubs = [p for p in pubs if p["publication_id"] != first["publication_id"]]
        if latest is None:
            skills = [r for r in skills if r["skill_id"] != first["entry_id"]]
        else:
            pubs.append(latest)
    expected = native_expected(snapshot, original, selected_skills=skills, selected_rules=rules,
        agent_binding=latest if stream == "agent" else None,
        expected_bindings=binds, expected_publications=pubs)
    assert module.project_boot_response(snapshot, budget=1200, query=None, full=False) == expected


def test_actual_projection_names_distinct_publication_not_retired_historical_source():
    module = required()
    snapshot, original = seed()
    first = snapshot["publication_rows"][0]
    retired = copy.deepcopy(first)
    retired.update(revision=2, state="retired")
    replacement = copy.deepcopy(first)
    replacement.update(publication_id=uid("PUBLIC replacement publication"), source_reference="PUBLIC independent owner publication")
    snapshot["publication_rows"] += [retired, replacement]
    expected_pubs = [p for p in snapshot["publication_rows"][:-2] if p["publication_id"] != first["publication_id"]] + [replacement]
    expected = native_expected(snapshot, original, expected_publications=expected_pubs)
    result = module.project_boot_response(snapshot, budget=1200, query=None, full=False)
    assert result == expected
    pin = next(p for p in result["persona"]["metadata"]["boot_validation"]["body_pins"] if p["entry_id"] == str(first["entry_id"]))
    assert pin["publication_ref"] == {"publication_id": str(replacement["publication_id"]), "revision": 1}


def test_unavailable_optional_projection_is_explicit_not_fabricated_live_rows():
    module = required()
    snapshot, _ = seed()
    snapshot["operational"]["projection_available"] = False
    result = module.project_boot_response(snapshot, budget=1200, query="release", full=False)
    metadata = result["persona"]["metadata"]["boot_context"]
    assert metadata["availability"]["operational"] == {"state": "unavailable", "reason": "projection_unavailable"}
    assert all(value is None for value in metadata["counts"].values())
    assert metadata["work_products"] == []
    assert "unavailable" in result["boot"].lower() and "No pending handoffs." not in result["boot"]


def test_frozen_raw_oracles_retain_the_canonical_skill_slug_field():
    # Positive oracle control is deliberately independent of missing product.
    for agent, variant, _ in CASES:
        original = packet(agent, variant)
        snapshot, repeated = seed(agent, variant)
        assert original == repeated
        assert len(snapshot["skill_rows"]) == len(original["persona"]["skills"]) == 17
        assert [r["slug"] for r in snapshot["skill_rows"]] == [s["skill_slug"] for s in original["persona"]["skills"]]
        expected = native_expected(snapshot, original)
        for key in ("schema_version", "project", "agent", "agent_identity", "role", "identity_text", "skills", "rules", "pending_handoffs", "harness"):
            assert expected["persona"][key] == original["persona"][key]
