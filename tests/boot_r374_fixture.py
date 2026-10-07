"""Declared public native rows and independent r379 golden-envelope patches.

Original packets supply immutable expected values. They are never supplied as a
response object to the product. Skill bodies/native UUIDs are synthetic; no
actual current skill body, credential, database or installation is qualified.
Operational text/metadata are an explicit existing-plane provider seam.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import uuid

from test_agent_boot_heads_r374 import fixture as head_fixture, uid

ROOT = Path(__file__).parent / "fixtures/r379-boot"
CASES = (
    ("kai", "default", []), ("kai", "full", ["--full"]),
    ("bob", "default", []), ("bob", "full", ["--full"]),
    ("vera", "default", []), ("vera", "full", ["--full"]),
    ("kai", "budget50", ["--budget", "50"]),
    ("kai", "budget500", ["--budget", "500"]),
    ("kai", "query-release", ["--query", "release"]),
)
SOURCE_LINE = "Sources: scoped canonical Cortex v2 rows and explicitly published catalogue revisions; no filesystem fallback."
ACCESS_LINE = "Cortex access: use the selected qualified API profile."


def required():
    assert importlib.util.find_spec("cortex_v2.agent_boot") is not None, "R379 native boot projection missing"
    module = importlib.import_module("cortex_v2.agent_boot")
    assert callable(getattr(module, "project_boot_response", None)), "R379 native boot projection missing"
    return module


def sha(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def packet(agent, variant):
    name = agent + "-" + variant + ".json"
    manifest = json.loads((ROOT / "raw-packet-sha256.json").read_text())
    raw = (ROOT / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == manifest[name]
    data = json.loads(raw)
    assert all("skill_slug" in s and "slug" not in s for s in data["persona"]["skills"])
    return data


def seed(agent="kai", variant="default", workspace_root="/PUBLIC/registered-code-root"):
    original = packet(agent, variant)
    persona = original["persona"]
    context, streams = head_fixture()
    actor = uid("actor-" + agent)
    context.update(project_key="helix", actor_id=actor, actor_name=agent,
                   actor_role=persona["role"], functional_roles=(persona["role"],),
                   workspace_root=str(workspace_root))
    streams["agent"][0].update(actor_id=actor, identity_id=uid("identity-" + agent),
                               persona_id=uid("persona-" + agent), functional_roles=[persona["role"]])
    streams["entry"] = []
    streams["publication"] = []
    person = {"scope_id": context["project_scope_id"], "persona_id": uid("persona-" + agent),
              "revision": 1, "audience": "agent_boot", "body": persona["identity_text"],
              "boot_manifest": {"schema_version": "cortex.boot-persona-manifest.v1",
                                "name": agent, "description": None, "scope": "project",
                                "permission": None, "body_ref": None, "version": "1",
                                "functional_roles": [persona["role"]],
                                "identity_text": persona["identity_text"]}}
    skills, rules = [], []
    catalogue = uid("catalogue")
    for item in persona["skills"]:
        slug = item["skill_slug"]
        row = {"scope_id": catalogue, "skill_id": uid("skill-" + slug), "revision": 1,
               "body": "PUBLIC R379 synthetic canonical skill body: " + slug + "\n",
               "boot_manifest": {"schema_version": "cortex.boot-skill-manifest.v1",
                                 **{k: copy.deepcopy(v) for k, v in item.items() if k != "skill_slug"}}}
        row["slug"] = slug
        skills.append(row)
        streams["publication"].append({"installation_id": context["installation_id"],
            "publication_id": uid("publication-" + slug), "revision": 1, "state": "active",
            "catalogue_scope_id": catalogue, "entry_kind": "skill", "entry_id": row["skill_id"],
            "entry_revision": 1, "published_by_principal": context["installation_owner_principal_id"],
            "published_at": datetime(2026, 10, 7, tzinfo=timezone.utc),
            "source_reference": "PUBLIC explicitly enacted catalogue revision"})
    for item in persona["rules"]:
        slug = item["rule_slug"]
        row = {"scope_id": context["project_scope_id"], "rule_id": uid("rule-" + slug),
               "revision": 1, "audience": "scope", "slug": slug, "body": item["body"],
               "boot_manifest": {"schema_version": "cortex.boot-rule-manifest.v1",
                                 "name": None, "description": None, "scope": "project",
                                 "permission": None, "body_ref": None, "version": item["version"],
                                 "title": item["title"], "source_file": item["source_file"]}}
        rules.append(row)
        streams["entry"].append({"project_scope_id": context["project_scope_id"],
            "binding_id": uid("rule-binding-" + slug), "revision": 1, "state": "active",
            "subject_kind": "project", "actor_id": None, "role_slug": None,
            "entry_kind": "rule", "entry_scope_id": row["scope_id"], "rule_id": row["rule_id"],
            "skill_id": None, "bound_revision": 1, "priority": 10,
            "enacted_by_principal": context["installation_owner_principal_id"],
            "enacted_at": datetime(2026, 10, 7, tzinfo=timezone.utc),
            "source_reference": "PUBLIC project-wide exact rule binding"})
    # Only the existing-plane operational tail is a seam; identity/manifest
    # selection and the native validation carrier must be computed by product.
    first_line, tail = original["boot"].split("\n", 1)
    assert first_line == persona["identity_text"]
    operational = {"boot_tail": tail, "boot_context": copy.deepcopy(persona["metadata"]["boot_context"]),
                   "pending_handoffs": copy.deepcopy(persona["pending_handoffs"]),
                   "harness": copy.deepcopy(persona["harness"]),
                   "surface_version": original["surface_version"],
                   "projection_available": True}
    snapshot = {"context": context, "agent_rows": streams["agent"],
                "entry_rows": streams["entry"], "publication_rows": streams["publication"],
                "persona_rows": [person], "skill_rows": skills, "rule_rows": rules,
                "operational": operational}
    return snapshot, original


def native_expected(snapshot, original, *, selected_skills=None, selected_rules=None,
                    agent_binding=None, expected_bindings=None, expected_publications=None):
    """Golden patch uses explicit expected selection, never product selectors."""
    result = copy.deepcopy(original)
    context = snapshot["context"]
    skills = snapshot["skill_rows"] if selected_skills is None else selected_skills
    rules = snapshot["rule_rows"] if selected_rules is None else selected_rules
    result["boot"] = result["boot"].replace(
        "Sources: live Cortex rows scoped to this project only; no filesystem fallback.", SOURCE_LINE
    ).replace("This Kaidera OS deployment: cortex-api:8501", ACCESS_LINE)
    result["persona"]["skills"] = [
        {"skill_slug": r["slug"], **{k: copy.deepcopy(v) for k, v in r["boot_manifest"].items()
                                   if k != "schema_version"}}
        for r in sorted(skills, key=lambda r: r["slug"])
    ]
    result["persona"]["rules"] = [
        {"rule_slug": r["slug"], "title": r["boot_manifest"]["title"], "body": r["body"],
         "source_file": r["boot_manifest"]["source_file"], "version": r["boot_manifest"]["version"]}
        for r in sorted(rules, key=lambda r: r["slug"])
    ]
    binding = snapshot["agent_rows"][0] if agent_binding is None else agent_binding
    binding_refs = snapshot["entry_rows"] if expected_bindings is None else expected_bindings
    publication_refs = snapshot["publication_rows"] if expected_publications is None else expected_publications
    proof = {"schema_version": "cortex.boot_validation.v1", "project_key": "helix",
             "project_scope_id": str(context["project_scope_id"]), "actor_id": str(context["actor_id"]),
             "agent_binding_revision": binding["revision"], "workspace_root": context["workspace_root"],
             "persona_ref": {"scope_id": str(context["project_scope_id"]),
                             "persona_id": str(binding["persona_id"]), "revision": binding["persona_revision"]},
             "body_pins": []}
    for kind, rows in (("skill", skills), ("rule", rules)):
        for row in sorted(rows, key=lambda r: r["slug"]):
            entry = row[kind + "_id"]
            # The oracle is handed the exact expected record. Its references are
            # fixture values, not an independent reimplementation of eligibility.
            binds = [b for b in binding_refs if b["entry_kind"] == kind
                     and b["entry_scope_id"] == row["scope_id"]
                     and b.get(kind + "_id") == entry and b["bound_revision"] == row["revision"]]
            pubs = [p for p in publication_refs if p["entry_kind"] == kind
                    and p["catalogue_scope_id"] == row["scope_id"]
                    and p["entry_id"] == entry and p["entry_revision"] == row["revision"]]
            assert len(binds) <= 1 and len(pubs) <= 1, "oracle needs explicit expected selected references"
            bind = binds[0] if binds else None
            pub = pubs[0] if pubs else None
            assert bind is None or bind["state"] == "active"
            assert pub is None or pub["state"] == "active"
            proof["body_pins"].append({"entry_kind": kind, "slug": row["slug"],
                "scope_id": str(row["scope_id"]), "entry_id": str(entry), "revision": row["revision"],
                "body_ref": row["boot_manifest"]["body_ref"], "body_sha256": sha(row["body"]),
                "binding_ref": {"binding_id": str(bind["binding_id"]), "revision": bind["revision"]} if bind else None,
                "publication_ref": {"publication_id": str(pub["publication_id"]), "revision": pub["revision"]} if pub else None})
    metadata = result["persona"]["metadata"]
    boot_context = metadata["boot_context"]
    boot_context["schema_version"] = "cortex.boot_context.v2"
    boot_context["source_boundary"] = "Cortex v2 canonical scoped read; explicit published catalogue visibility; no filesystem fallback"
    boot_context["sources"] = [
        {"section": "identity", "kind": "boot_agent_binding_revision",
         "project_scope_id": str(context["project_scope_id"]), "actor_id": str(context["actor_id"]),
         "revision": binding["revision"]},
        {"section": "persona", "kind": "persona_revision", **proof["persona_ref"]},
        {"section": "bodies", "kind": "exact_canonical_revisions",
         "refs": [{k: p[k] for k in ("entry_kind", "scope_id", "entry_id", "revision")} for p in proof["body_pins"]]},
        {"section": "catalogue", "kind": "owner_publication_revisions",
         "refs": [p["publication_ref"] for p in proof["body_pins"] if p["publication_ref"]]},
        {"section": "operational", "kind": "native_operational_projection",
         "project_scope_id": str(context["project_scope_id"]), "available": True},
    ]
    metadata["boot_validation"] = proof
    return result


def install_public_bodies(snapshot, root):
    for row in snapshot["skill_rows"]:
        target = Path(root) / row["boot_manifest"]["body_ref"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(row["body"])
        target.chmod(0o644)


def json_bytes(response):
    # Current cortex_api_call prints the body verbatim, with no appended LF.
    return json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
