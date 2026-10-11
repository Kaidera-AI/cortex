"""The literal command inputs and original closed installation predicate."""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path

from custody import bound_file, bound_json, physical, require


PROJECTS_AST = "18b334aa50df17e9001863a448e61c315dbe2bef33641b9c4b191c1b7b9d1b25"


def literal_values(authority):
    B = authority.B
    root = B.get("payload_dir")
    require(isinstance(root, str) and root.startswith("/") and ".." not in Path(root).parts
            and not any(c in root for c in "\n\r\t:"), "payload_path_slot_unbound")
    row = B.get("source_members", {}).get("packages/deploy/cortex-runtime")
    require(isinstance(row, dict) and row.get("path") == root + "/packages/deploy/cortex-runtime",
            "literal_runtime_binding")
    return {"cto_payload": root, "cto_runtime": row["path"], "cto_runtime_sha": row["sha256"]}


def validate_literals(authority, actual, delivery_row):
    expected = literal_values(authority)
    require(isinstance(actual, dict) and all(actual.get(k) == v for k, v in expected.items()),
            "literal_command_input_mismatch")
    require(delivery_row == authority.B.get("delivery_binding"), "delivery_binding_alias")
    delivery = bound_json(delivery_row)
    require(delivery.get("payload_dir") == expected["cto_payload"] and
            delivery.get("runtime_sha256") == expected["cto_runtime_sha"] and
            delivery.get("cortex_source") == authority.B["source_revision"], "delivery_identity")
    return expected


def pre_load(authority, manifest, actual, delivery_row):
    values = validate_literals(authority, actual, delivery_row)
    root = physical(values["cto_payload"])
    require(root.is_dir(), "payload_directory_type")
    members = authority.B.get("source_members")
    require(isinstance(members, dict), "source_members_unbound")
    helper_name = "packages/deploy/image_manifest.py"
    for name in (helper_name, "packages/deploy/cortex-runtime", "packages/deploy/release.json"):
        require(name in members and members[name]["path"] == str(root / name), "source_member_path")
        bound_file(members[name])
    identity = bound_json(members["packages/deploy/release.json"])
    require(identity.get("schema") == "cortex.payload.v1" and
            identity.get("source_revision") == authority.B["source_revision"] == manifest["source_revision"]
            and identity.get("schema_revision") == authority.B["schema_revision"]
            and identity.get("version") == manifest["oci_inventory"]["version"], "payload_signed_identity")
    helper = members[helper_name]
    namespace = {"__file__": helper["path"], "__name__": "verified_run8_image_contract"}
    body = bound_file(helper, read=True, limit=1024 * 1024)
    # This exact final-source module is independently reviewed/frozen, not selected
    # by an untrusted install.files value. It has only stdlib definitions/reads.
    exec(compile(body, helper["path"], "exec"), namespace)
    installation, inventory = namespace["load_installation"](root, identity)
    require(installation["delivery_kind"] == "prebuilt" and installation["platform"] == "linux/amd64",
            "closed_installation_delivery")
    require(inventory == manifest["oci_inventory"], "closed_inventory_signed_main_mismatch")
    return {"closed_installation": True, "before_image_load": True,
            "source_revision": identity["source_revision"], "schema_revision": identity["schema_revision"],
            "platform": installation["platform"], "runtime_sha256": values["cto_runtime_sha"]}


def validate_projects(authority, actual, delivery_row, environment):
    values = validate_literals(authority, actual, delivery_row)
    raw = bound_file(authority.B["source_members"]["packages/deploy/cortex-runtime"],
                     read=True, limit=2 * 1024 * 1024)
    nodes = [n for n in ast.parse(raw).body if isinstance(n, ast.FunctionDef)
             and n.name == "validated_projects_root"]
    require(len(nodes) == 1 and not nodes[0].decorator_list, "projects_predicate_count")
    require(hashlib.sha256(ast.dump(nodes[0], include_attributes=False).encode()).hexdigest() == PROJECTS_AST,
            "final_projects_predicate_reopens_review")
    require(environment.get("CORTEX_PROJECTS_ROOT") == authority.B.get("projects_root")
            and isinstance(authority.B.get("projects_root"), str), "projects_literal_binding")
    # Supply only the predicate's environment, not the process's secret-bearing environment.
    class PredicateOS:
        environ = environment
        path = os.path
        R_OK, X_OK = os.R_OK, os.X_OK
        access = staticmethod(os.access)
    namespace = {"Path": Path, "os": PredicateOS}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), values["cto_runtime"], "exec"), namespace)
    selected = namespace["validated_projects_root"]()
    info = Path(selected).stat()
    require(info.st_uid == os.getuid() and not info.st_mode & 0o022, "projects_owner_custody")
    return {"projects_root": selected, "uid": info.st_uid, "mode": oct(info.st_mode & 0o777),
            "predicate_ast_sha256": PROJECTS_AST}
