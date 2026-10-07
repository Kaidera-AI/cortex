"""Own-agent boot selection over canonical, authorized revision snapshots.

These pure functions select revision heads before eligibility. They do not load
credentials, query storage, grant scope access, or materialize local bodies.
The repository and enactment boundaries supply their trusted context and rows.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from cortex_v2.store import ApiProblem


_STREAM_KEYS = {
    "agent": ("project_scope_id", "actor_id"),
    "entry": ("project_scope_id", "binding_id"),
    "publication": ("installation_id", "publication_id"),
}


def select_boot_heads(
    rows: Iterable[Mapping[str, Any]], *, stream: str,
) -> dict[tuple[Any, ...], Mapping[str, Any]]:
    """Choose the unique maximum enacted revision for every complete stream.

    No state, actor, role, owner or timestamp filter may hide a newer head.
    Duplicate stream/revision records are ambiguous even if input order differs.
    """
    if stream not in _STREAM_KEYS:
        raise ValueError("Unknown boot revision stream")
    heads: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    revisions: set[tuple[Any, ...]] = set()
    for row in rows:
        try:
            key = tuple(row[field] for field in _STREAM_KEYS[stream])
            revision = row["revision"]
            if any(value is None for value in key) or type(revision) is not int or revision < 1:
                raise ValueError("Invalid boot revision identity")
            history_key = (*key, revision)
            if history_key in revisions:
                raise ValueError("Ambiguous boot stream revision")
            revisions.add(history_key)
            if key not in heads or revision > heads[key]["revision"]:
                heads[key] = row
        except (KeyError, TypeError):
            raise ValueError("Invalid boot revision identity") from None
    return heads


def _reference_revision(value):
    if type(value) is not int or value < 1:
        raise ValueError("Boot canonical reference revision must be an exact positive integer")
    return value


def resolve_boot_bindings(
    context: Mapping[str, Any], *, agent_rows, entry_rows, publication_rows,
) -> dict[str, Any]:
    """Filter only the already selected heads against trusted own-member facts.

    Catalogue visibility requires an active exact installation-owner publication;
    descriptive metadata and historical publication rows cannot grant visibility.
    This selection does not expand the member's read-scope context.
    """
    agents = select_boot_heads(agent_rows, stream="agent")
    entries = select_boot_heads(entry_rows, stream="entry")
    publications = select_boot_heads(publication_rows, stream="publication")
    scope, actor = context["project_scope_id"], context["actor_id"]
    agent = agents.get((scope, actor))
    if agent is None or agent.get("state") != "active":
        raise ApiProblem(409, "boot_binding_unavailable", "An active own-agent boot binding is required.")
    _reference_revision(agent["persona_revision"])
    roles = set(context["functional_roles"])
    if set(agent.get("functional_roles", ())) != roles:
        raise ApiProblem(409, "boot_binding_unavailable", "The boot binding does not match registered roles.")
    visible = [row for row in publications.values()
               if row.get("state") == "active"
               and row.get("installation_id") == context["installation_id"]
               and ((row.get("installation_id"), row.get("publication_id"), row.get("revision"))
                    in context["eligible_publication_heads"] if "eligible_publication_heads" in context
                    else row.get("published_by_principal") == context["installation_owner_principal_id"])]

    for publication in visible:
        _reference_revision(publication["entry_revision"])

    def eligible(row):
        if row.get("state") != "active" or row.get("project_scope_id") != scope:
            return False
        subject = row.get("subject_kind")
        if subject == "agent":
            if row.get("actor_id") != actor or row.get("role_slug") is not None:
                return False
        elif subject == "functional_role":
            if row.get("role_slug") not in roles or row.get("actor_id") is not None:
                return False
        elif subject == "project":
            if row.get("actor_id") is not None or row.get("role_slug") is not None:
                return False
        else:
            return False
        kind = row.get("entry_kind")
        if kind not in {"skill", "rule"} or row.get(kind + "_id") is None:
            return False
        _reference_revision(row["bound_revision"])
        if row.get(("rule" if kind == "skill" else "skill") + "_id") is not None:
            raise ValueError("Boot entry must name exactly one canonical identifier")
        if type(row.get("priority")) is not int:
            raise ValueError("Boot binding priority must be an exact integer")
        if row.get("entry_scope_id") == scope:
            return True
        return any(publication.get("catalogue_scope_id") == row.get("entry_scope_id")
                   and publication.get("entry_kind") == kind
                   and publication.get("entry_id") == row.get(kind + "_id")
                   and publication.get("entry_revision") == row.get("bound_revision")
                   for publication in visible)

    return {"agent_binding": agent,
            "entry_bindings": [row for row in entries.values() if eligible(row)],
            "publications": visible}


def _manifest(row, kind):
    """Check descriptive metadata without treating any field as authority."""
    from pathlib import PurePosixPath

    value = row.get("boot_manifest")
    common = {"schema_version", "name", "description", "scope", "permission", "body_ref", "version"}
    extra = ({"functional_roles", "identity_text", "lane", "not_lane", "reports_to"}
             if kind == "persona" else {"title", "source_file"} if kind == "rule" else set())
    required = common | ({"functional_roles", "identity_text"} if kind == "persona"
                         else {"title", "source_file"} if kind == "rule" else set())
    if (not isinstance(value, dict) or not required <= value.keys()
            or not value.keys() <= common | extra
            or value.get("schema_version") != f"cortex.boot-{kind}-manifest.v1"
            or value.get("scope") not in {"project", "global"}):
        raise ValueError("Required canonical boot manifest is unavailable")
    for field, text in value.items():
        if field == "functional_roles":
            if (not isinstance(text, list) or not text
                    or any(not isinstance(role, str) or not role or "\x00" in role for role in text)
                    or len(text) != len(set(text))):
                raise ValueError("Invalid boot functional roles")
        elif text is not None and (not isinstance(text, str) or "\x00" in text):
            raise ValueError("Invalid boot manifest text")
    body_ref = value["body_ref"]
    if body_ref is not None:
        path = PurePosixPath(body_ref)
        if (not body_ref or path.is_absolute() or str(path) != body_ref
                or ".." in path.parts or "\\" in body_ref
                or any(ord(char) < 32 or ord(char) == 127 for char in body_ref)):
            raise ValueError("Invalid canonical boot body reference")
    if not isinstance(row.get("body"), str) or "\x00" in row["body"]:
        raise ValueError("Required canonical boot body is unavailable")
    return value


def _truncate_optional(text, maximum):
    if maximum <= 0: return ""
    if len(text) <= maximum: return text
    candidate = text[:maximum].rstrip()
    newline = candidate.rfind("\n")
    if newline > 0: candidate = candidate[:newline].rstrip()
    lines = candidate.splitlines()
    while lines:
        tail = lines[-1].strip()
        if tail.endswith(":") or (tail.startswith("---") and tail.endswith("---")):
            lines.pop()
        else: break
    return "\n".join(lines) + "\n  ... [truncated]" if lines else ""


def _budget_boot(mandatory, optional, budget):
    # Preserve the admitted token-hint clamp and character approximation;
    # mandatory identity/policy survives even when it alone exceeds the hint.
    budget = min(max(int(budget), 50), 500)
    tiers = {"P0": mandatory, **{key: optional.get(key, "") for key in ("P1", "P2", "P3")}}
    total = sum(len(text) // 4 for text in tiers.values())
    for key in ("P3", "P2", "P1"):
        if total <= budget: break
        available = len(tiers[key])
        tiers[key] = _truncate_optional(tiers[key], available - min((total - budget) * 4, available))
        total = sum(len(text) // 4 for text in tiers.values())
    return "\n\n".join(text for text in tiers.values() if text.strip())


def project_boot_response(snapshot, *, budget, query, full):
    """Project exact canonical revisions and compute body/provenance proofs.

    Operational text comes from the explicitly separate native projection seam;
    identity, selection and proof never come from that provider or local files.
    """
    import copy
    import hashlib

    context = snapshot["context"]
    resolved = resolve_boot_bindings(context, agent_rows=snapshot["agent_rows"],
        entry_rows=snapshot["entry_rows"], publication_rows=snapshot["publication_rows"])
    binding = resolved["agent_binding"]
    indexes = {}
    for kind in ("persona", "skill", "rule"):
        index = {}
        for row in snapshot[kind + "_rows"]:
            _reference_revision(row["revision"])
            key = (row["scope_id"], row[kind + "_id"], row["revision"])
            if key in index: raise ValueError("Ambiguous canonical boot revision")
            index[key] = row
        indexes[kind] = index
    persona_key = (context["project_scope_id"], binding["persona_id"], binding["persona_revision"])
    person = indexes["persona"].get(persona_key)
    if person is None or person.get("audience") != "agent_boot":
        raise ValueError("Required own-agent canonical persona is unavailable")
    manifest = _manifest(person, "persona")
    if (manifest["name"] != context["actor_name"] or manifest["scope"] != "project"
            or manifest["identity_text"] != person["body"]
            or set(manifest["functional_roles"]) != set(context["functional_roles"])
            or context["actor_role"] not in manifest["functional_roles"]):
        raise ValueError("Canonical boot persona does not match registered identity")
    publications = resolved["publications"]
    entries = resolved["entry_bindings"]
    candidates = {"skill": [], "rule": []}
    for entry in entries:
        kind = entry["entry_kind"]
        key = (entry["entry_scope_id"], entry[kind + "_id"], entry["bound_revision"])
        row = indexes[kind].get(key)
        if row is None: raise ValueError("Bound canonical boot entry is unavailable")
        value = _manifest(row, kind)
        pub = next((p for p in publications if (p["catalogue_scope_id"], p["entry_id"], p["entry_revision"]) == key
                    and p["entry_kind"] == kind), None)
        local = key[0] == context["project_scope_id"]
        if value["scope"] != ("project" if local else "global"):
            raise ValueError("Boot entry manifest scope differs from canonical origin")
        candidates[kind].append((row, entry, pub, local, entry["priority"]))
    for pub in publications:
        kind = pub["entry_kind"]
        if kind not in candidates: raise ValueError("Invalid published boot entry kind")
        key = (pub["catalogue_scope_id"], pub["entry_id"], pub["entry_revision"])
        row = indexes[kind].get(key)
        if row is None: raise ValueError("Published canonical boot entry is unavailable")
        if _manifest(row, kind)["scope"] != "global":
            raise ValueError("Published boot entry must name its catalogue origin")
        candidates[kind].append((row, None, pub, False, 0))
    selected = {}
    for kind, rows in candidates.items():
        # PostgreSQL legacy version DESC is text ordering, with NULL first.
        rows.sort(key=lambda item: (item[3], item[4], item[0]["boot_manifest"]["version"] is None,
                                   item[0]["boot_manifest"]["version"] or ""), reverse=True)
        winners = {}
        for item in rows: winners.setdefault(item[0]["slug"], item)
        selected[kind] = [winners[slug] for slug in sorted(winners)]
    proof = {"schema_version": "cortex.boot_validation.v1", "project_key": context["project_key"],
        "project_scope_id": str(context["project_scope_id"]), "actor_id": str(context["actor_id"]),
        "agent_binding_revision": binding["revision"], "workspace_root": context["workspace_root"],
        "persona_ref": {"scope_id": str(persona_key[0]), "persona_id": str(persona_key[1]), "revision": persona_key[2]},
        "body_pins": []}
    output = {"skill": [], "rule": []}
    for kind in ("skill", "rule"):
        for row, entry, pub, _, _ in selected[kind]:
            value = row["boot_manifest"]
            if kind == "skill":
                output[kind].append({"skill_slug": row["slug"], **{k: copy.deepcopy(v) for k,v in value.items() if k != "schema_version"}})
            else:
                output[kind].append({"rule_slug": row["slug"], "title": value["title"], "body": row["body"],
                    "source_file": value["source_file"], "version": value["version"]})
            proof["body_pins"].append({"entry_kind": kind, "slug": row["slug"], "scope_id": str(row["scope_id"]),
                "entry_id": str(row[kind + "_id"]), "revision": row["revision"], "body_ref": value["body_ref"],
                "body_sha256": hashlib.sha256(row["body"].encode("utf-8")).hexdigest(),
                "binding_ref": {"binding_id": str(entry["binding_id"]), "revision": entry["revision"]} if entry else None,
                "publication_ref": {"publication_id": str(pub["publication_id"]), "revision": pub["revision"]} if pub else None})
    operational = snapshot["operational"]
    boot_context = copy.deepcopy(operational["boot_context"])
    available = operational.get("projection_available") is True
    boot_context.update(schema_version="cortex.boot_context.v2",
        source_boundary="Cortex v2 canonical scoped read; explicit published catalogue visibility; no filesystem fallback",
        sources=[{"section": "identity", "kind": "boot_agent_binding_revision",
            "project_scope_id": str(context["project_scope_id"]), "actor_id": str(context["actor_id"]), "revision": binding["revision"]},
            {"section": "persona", "kind": "persona_revision", **proof["persona_ref"]},
            {"section": "bodies", "kind": "exact_canonical_revisions", "refs": [{k:p[k] for k in ("entry_kind","scope_id","entry_id","revision")} for p in proof["body_pins"]]},
            {"section": "catalogue", "kind": "owner_publication_revisions", "refs": [p["publication_ref"] for p in proof["body_pins"] if p["publication_ref"]]},
            {"section": "operational", "kind": "native_operational_projection", "project_scope_id": str(context["project_scope_id"]), "available": available}])
    tail = operational["boot_tail"] if available else "Operational projection unavailable."
    if not available:
        boot_context.setdefault("availability", {})["operational"] = {"state":"unavailable", "reason":"projection_unavailable"}
        boot_context["counts"] = {key: None for key in boot_context["counts"]}
        boot_context["work_products"] = []
    tail = tail.replace("Sources: live Cortex rows scoped to this project only; no filesystem fallback.",
        "Sources: scoped canonical Cortex v2 rows and explicitly published catalogue revisions; no filesystem fallback.")
    tail = tail.replace("This Kaidera OS deployment: cortex-api:8501", "Cortex access: use the selected qualified API profile.")
    persona = {"schema_version":"cortex.persona.v2", "project":context["project_key"], "agent":context["actor_name"],
        "agent_identity":context["actor_name"] + "@" + context["project_key"], "role":context["actor_role"],
        "identity_text":manifest["identity_text"], "skills":output["skill"], "rules":output["rule"],
        "pending_handoffs":copy.deepcopy(operational["pending_handoffs"]) if available else [],
        "harness":copy.deepcopy(operational["harness"]),
        "metadata":{"boot_context":boot_context, "boot_validation":proof}}
    return {"boot":_budget_boot(manifest["identity_text"] + "\n" + tail,
        operational.get("optional_tiers", {}) if available else {}, budget),
        "surface_version":operational["surface_version"], "persona":persona}


async def load_boot_snapshot(connection, context):
    from .interface.agent_boot import load_snapshot
    return await load_snapshot(connection, context)


async def read_boot(connection, context, agent, *, budget=1200, query=None, full=False, agent_label=None):
    snapshot = await load_boot_snapshot(connection, context)
    trusted = snapshot['context']
    if (trusted['project_scope_id'] != context.selected.scope_id
            or trusted['installation_id'] != context.principal.installation_id
            or not isinstance(agent, str) or agent.casefold() != trusted['actor_name'].casefold()
            or agent_label is not None and agent_label.casefold() != trusted['actor_name'].casefold()):
        raise ApiProblem(403, 'boot_identity_mismatch', 'Boot requires the credential-bound own agent.')
    try:
        return project_boot_response(snapshot, budget=budget, query=query, full=full)
    except (ValueError, KeyError, TypeError):
        raise ApiProblem(409, 'boot_binding_unavailable', 'Required canonical boot facts are unavailable.') from None
