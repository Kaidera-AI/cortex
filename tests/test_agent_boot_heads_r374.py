"""R379 head-history contract; public rows, no database/auth qualification.

Precedence is independently covered by r379's ratified legacy countermodel.
The two internal functions below are the planned pure selection boundary; they
cannot receive a principal's credential or expand its authorized read scopes.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import importlib
import importlib.util
import uuid

import pytest

from cortex_v2.store import ApiProblem


def uid(label):
    return uuid.uuid5(uuid.NAMESPACE_URL, "PUBLIC-r374-" + label)


SCOPE, OTHER_SCOPE = uid("scope"), uid("other-scope")
ACTOR, OTHER_ACTOR = uid("actor"), uid("other-actor")
INSTALL, OTHER_INSTALL = uid("install"), uid("other-install")
OWNER, FOREIGN_OWNER = uid("owner"), uid("foreign-owner")
BINDING, PUBLICATION = uid("binding"), uid("publication")
KEYS = {
    "agent": ("project_scope_id", "actor_id"),
    "entry": ("project_scope_id", "binding_id"),
    "publication": ("installation_id", "publication_id"),
}


def required():
    assert importlib.util.find_spec("cortex_v2.agent_boot") is not None, (
        "R374 native boot head selector missing"
    )
    module = importlib.import_module("cortex_v2.agent_boot")
    for name in ("select_boot_heads", "resolve_boot_bindings"):
        assert callable(getattr(module, name, None)), "R374 native boot head selector missing"
    return module


def fixture():
    context = {
        "project_scope_id": SCOPE,
        "actor_id": ACTOR,
        "installation_id": INSTALL,
        "functional_roles": ("PUBLIC-role",),
        "installation_owner_principal_id": OWNER,
    }
    rows = {
        "agent": [{
            "project_scope_id": SCOPE, "actor_id": ACTOR,
            "revision": 1, "state": "active", "identity_id": uid("identity"),
            "persona_id": uid("persona"), "persona_revision": 1,
            "functional_roles": ["PUBLIC-role"],
            "enacted_by_principal": OWNER,
            "enacted_at": datetime(2026, 10, 7, tzinfo=timezone.utc),
            "source_reference": "PUBLIC agent active1",
        }],
        "entry": [{
            "project_scope_id": SCOPE, "binding_id": BINDING,
            "revision": 1, "state": "active", "subject_kind": "agent",
            "actor_id": ACTOR, "role_slug": None, "entry_kind": "skill",
            "entry_scope_id": SCOPE, "skill_id": uid("skill"), "rule_id": None,
            "bound_revision": 1, "priority": 10,
            "enacted_by_principal": OWNER,
            "enacted_at": datetime(2026, 10, 7, tzinfo=timezone.utc),
            "source_reference": "PUBLIC entry active1",
        }],
        "publication": [{
            "installation_id": INSTALL, "publication_id": PUBLICATION,
            "revision": 1, "state": "active", "catalogue_scope_id": uid("catalogue"),
            "entry_kind": "skill", "entry_id": uid("global-skill"),
            "entry_revision": 1, "published_by_principal": OWNER,
            "published_at": datetime(2026, 10, 7, tzinfo=timezone.utc),
            "source_reference": "PUBLIC publication active1",
        }],
    }
    return context, rows


def resolve(module, context, rows):
    return module.resolve_boot_bindings(
        context,
        agent_rows=rows["agent"], entry_rows=rows["entry"],
        publication_rows=rows["publication"],
    )


def reversion(row, revision, state):
    changed = copy.deepcopy(row)
    changed.update(revision=revision, state=state,
                   source_reference=f"PUBLIC exact head{revision} {state}")
    for key in ("persona_revision", "bound_revision", "entry_revision"):
        if key in changed:
            changed[key] = revision
    return changed


@pytest.mark.parametrize("stream", tuple(KEYS))
@pytest.mark.parametrize("reactivated", (False, True))
@pytest.mark.parametrize("order", ("ascending", "descending", "interleaved"))
def test_highest_enacted_head_precedes_state_and_never_revives_active1(stream, reactivated, order):
    module = required()
    context, rows = fixture()
    first = rows[stream][0]
    rows[stream].append(reversion(first, 2, "retired"))
    if reactivated:
        rows[stream].append(reversion(first, 3, "active"))
    head = rows[stream][-1]
    # Input order and tied timestamps cannot stand in for revision numbers.
    if order == "descending":
        rows[stream].reverse()
    elif order == "interleaved" and reactivated:
        rows[stream] = [rows[stream][1], rows[stream][2], rows[stream][0]]
    before = copy.deepcopy((context, rows))
    key = tuple(first[k] for k in KEYS[stream])
    chosen = module.select_boot_heads(rows[stream], stream=stream)
    assert chosen == {key: head}
    if stream == "agent" and not reactivated:
        with pytest.raises((ApiProblem, ValueError)):
            resolve(module, context, rows)
    else:
        result = resolve(module, context, rows)
        if stream == "agent":
            assert result["agent_binding"] == head
            assert result["agent_binding"]["persona_revision"] == 3
        else:
            selected = result["entry_bindings" if stream == "entry" else "publications"]
            assert selected == ([head] if reactivated else [])
            if selected:
                assert selected[0]["revision"] == 3
                assert selected[0]["bound_revision" if stream == "entry" else "entry_revision"] == 3
        for untouched in {"entry", "publication"} - {stream}:
            assert result["entry_bindings" if untouched == "entry" else "publications"] == rows[untouched]
    assert (context, rows) == before


@pytest.mark.parametrize("stream", tuple(KEYS))
def test_raw_head_selection_keeps_every_logical_key_isolated(stream):
    module = required()
    _, rows = fixture()
    own = rows[stream][0]
    others = []
    for field in KEYS[stream]:
        other = reversion(own, 40, "active")
        other[field] = uid("unrelated-" + field)
        others.append(other)
    retired = reversion(own, 2, "retired")
    history = [*others, retired, own]
    expected = {tuple(r[k] for k in KEYS[stream]): r for r in [*others, retired]}
    before = copy.deepcopy(history)
    assert module.select_boot_heads(history, stream=stream) == expected
    assert history == before


@pytest.mark.parametrize("case", ("entry-actor", "entry-role", "publication-owner"))
def test_eligibility_filter_never_runs_before_head_selection(case):
    module = required()
    context, rows = fixture()
    stream = "publication" if case == "publication-owner" else "entry"
    first = rows[stream][0]
    if case == "entry-role":
        first.update(subject_kind="functional_role", actor_id=None, role_slug="PUBLIC-role")
    second = reversion(first, 2, "active")
    if case == "entry-actor":
        second["actor_id"] = OTHER_ACTOR
    elif case == "entry-role":
        second["role_slug"] = "PUBLIC-other-role"
    else:
        second["published_by_principal"] = FOREIGN_OWNER
    rows[stream].append(second)
    key = tuple(first[k] for k in KEYS[stream])
    assert module.select_boot_heads(rows[stream], stream=stream) == {key: second}
    result = resolve(module, context, rows)
    assert result["entry_bindings" if stream == "entry" else "publications"] == []
    # Still-active actor/owner and a valid historical row cannot justify revival.
    assert rows["agent"][0]["state"] == "active"
    assert context["installation_owner_principal_id"] == OWNER


@pytest.mark.parametrize("retire", (False, True))
def test_retired_publication_does_not_erase_or_revive_another_valid_publication(retire):
    module = required()
    context, rows = fixture()
    original = rows["publication"][0]
    other = copy.deepcopy(original)
    other.update(publication_id=uid("independent-publication"),
                 source_reference="PUBLIC independently enacted visibility")
    rows["publication"].append(other)
    if retire:
        rows["publication"].append(reversion(original, 2, "retired"))
    result = resolve(module, context, rows)
    selected = result["publications"]
    assert {r["publication_id"] for r in selected} == ({other["publication_id"]} if retire else {PUBLICATION, other["publication_id"]})
    if retire:
        assert selected == [other]
        assert selected[0]["source_reference"] == "PUBLIC independently enacted visibility"
    assert original["source_reference"] == "PUBLIC publication active1"


def test_own_scope_actor_and_installation_remain_separate_after_other_key_retirement():
    module = required()
    context, rows = fixture()
    foreign_agent = copy.deepcopy(rows["agent"][0])
    foreign_agent.update(project_scope_id=OTHER_SCOPE, actor_id=OTHER_ACTOR)
    rows["agent"].extend([foreign_agent, reversion(foreign_agent, 2, "retired")])
    foreign_entry = copy.deepcopy(rows["entry"][0])
    foreign_entry.update(project_scope_id=OTHER_SCOPE, actor_id=OTHER_ACTOR)
    rows["entry"].extend([foreign_entry, reversion(foreign_entry, 2, "retired")])
    foreign_publication = copy.deepcopy(rows["publication"][0])
    foreign_publication["installation_id"] = OTHER_INSTALL
    rows["publication"].extend([foreign_publication, reversion(foreign_publication, 2, "retired")])
    result = resolve(module, context, rows)
    assert result["agent_binding"] == rows["agent"][0]
    assert result["entry_bindings"] == [rows["entry"][0]]
    assert result["publications"] == [rows["publication"][0]]


@pytest.mark.parametrize("stream", tuple(KEYS))
def test_ambiguous_same_logical_key_revision_cannot_depend_on_row_order(stream):
    module = required()
    _, rows = fixture()
    one = rows[stream][0]
    two = copy.deepcopy(one)
    two["state"] = "retired"
    for history in ([one, two], [two, one]):
        with pytest.raises((ApiProblem, ValueError)):
            module.select_boot_heads(history, stream=stream)
