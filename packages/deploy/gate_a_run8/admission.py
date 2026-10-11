"""Immutable rule admission and complete executable custody for one run."""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import re
import subprocess
import time

from custody import Refusal, bound_file, bound_json, hex_value, json_bytes, parse_json, require, source_record


OPERATION = "POST_SIGNED_GATE_A_INSTALL"
GO_LITERAL = "GATE A RUN 8 GO (H-D455)"
ROOT_ROLES = frozenset({
    "launcher", "manager", "local_watchdog", "guest_watchdog", "template_checker",
    "shell_path_checker", "admin_preparer", "identity_sweeper", "guard_checker",
    "literal_recorder", "receipt_exporter", "retirer", "cleanup_verifier",
    "production_verifier", "payload_extractor", "cached_image_verifier", "owner_setup",
    "homebrew_installer", "runtime", "image_contract", "provider_prepare",
    "provider_labels", "prerequisite_writer", "denials", "ordinary_restart",
    "kos_installer", "kos_appdb_doctor", "openkai_installer", "omp_binary",
    "restoration_template", "restoration_observer", "python", "podman",
    "podman_compose", "systemctl", "loginctl", "dnf", "nft", "restorecon",
    "sha256sum", "ssh", "scp", "reboot", "archive_tools",
})
CHECKS = frozenset({"token_sweep", "shell_path", "template", "guard",
                    "fresh_price", "fresh_ami", "operator_ip"})
STATIC_FILES = frozenset({"c_receipt", "main_receipt", "vera_packet_receipt",
                         "manifest", "signature", "production_public_key",
                         "verifier", "helper_ledger", "command_inventory_file"})
NATIVE_FIELDS = frozenset({"instance_id", "owner_uid", "container_ids", "boot_id",
                          "armed_receipt", "appdb_runtime_identity_sha256", "appdb_port"})


def load_authority(binding_row, go_row):
    """Expected rows are supplied independently; neither body approves its hash."""
    B = bound_json(binding_row)
    body = bound_file(go_row, read=True, limit=4 * 1024 * 1024).decode()
    require(GO_LITERAL in body, "go_literal_missing")
    blocks = re.findall(r"^```json\n(.*?)\n```", body, re.M | re.S)
    require(len(blocks) == 1, "go_authority_block_count")
    G = parse_json(blocks[0])
    validate_rules(B, G, binding_row["sha256"], go_row["sha256"])
    return Authority(B, G, binding_row["sha256"], go_row["sha256"])


def validate_rules(B, G, binding_sha, go_sha):
    require(isinstance(B, dict) and isinstance(G, dict), "authority_shape")
    require(B.get("schema") == "cortex.gate-a.run8.static.v2", "static_schema")
    require(B.get("operation") == OPERATION, "operation_not_install")
    for key, length in (("source_revision", 40), ("source_tree", 40), ("schema_revision", 64)):
        require(hex_value(B.get(key), length), "unbound_slot_" + key)
    require(B.get("runtime_owner_name") == "kos", "runtime_owner")
    require(isinstance(B.get("owner_tag"), str) and B["owner_tag"], "owner_tag_unbound")
    require(isinstance(B.get("run_id"), str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{2,80}", B["run_id"]),
            "run_id_unbound")
    require(isinstance(B.get("client_token"), str) and
            re.fullmatch(r"helix-cortex-gate-a-run8-[0-9]{8}-launch1", B["client_token"]),
            "client_token_unbound")
    require(not NATIVE_FIELDS.intersection(B), "native_fact_in_static_seal")
    restoration = B.get("restoration")
    require(isinstance(restoration, dict) and
            restoration.get("kind") == "template-and-observed-ID-rule" and
            not NATIVE_FIELDS.intersection(restoration), "restoration_phase_conflict")
    require(B.get("trust_namespace") == "production", "test_trust_refused")
    require(hex_value(B.get("trusted_public_key_sha256")), "trusted_key_slot_unbound")
    require(B.get("release") == {
        "release_id": "v0.1.003-manual.1", "release_lineage": "cortex-v1-manual",
        "release_sequence": 1, "api_contract": "cortex-kos-v02009.v1",
    }, "release_identity")
    require(B.get("envelope") == {"max_allocations": 1, "budget_usd": 3,
        "retirement_reserve_usd": 1.5, "stop_new_minutes": 210,
        "watchdog_minutes": 230, "absolute_minutes": 240}, "admission_envelope")
    require(G.get("schema") == "cortex.gate-a.run8.go.v2" and G.get("literal") == GO_LITERAL,
            "go_schema_or_literal")
    require(G.get("static_binding_sha256") == binding_sha and hex_value(go_sha), "go_static_binding")
    for key in ("run_id", "client_token", "owner_tag", "source_revision", "operation"):
        require(G.get(key) == B.get(key), "go_context_" + key)
    require(G.get("envelope") == B["envelope"], "go_envelope")
    require(G.get("scope") == "one-post-sign-install-and-owned-retirement", "go_scope")
    require("go_sha256" not in B and "kai_go" not in B, "cyclic_go_static_seal")


class Authority:
    def __init__(self, B, G, binding_sha, go_sha):
        self.B, self.G = B, G
        self.binding_sha, self.go_sha = binding_sha, go_sha

    @property
    def context(self):
        return {**{k: self.B[k] for k in ("run_id", "client_token", "owner_tag", "source_revision", "operation")},
                "static_binding_sha256": self.binding_sha, "go_sha256": self.go_sha}

    def validate_context(self, row):
        require(isinstance(row, dict), "context_shape")
        for key, value in self.context.items():
            require(row.get(key) == value, "stale_context_" + key)

    def validate_files(self):
        for name in STATIC_FILES:
            bound_file(self.B.get(name))
        require(self.B["production_public_key"]["sha256"] == self.B["trusted_public_key_sha256"],
                "production_key_custody")
        inventory = bound_json(self.B["command_inventory_file"])
        require(inventory == self.B.get("command_inventory"), "command_inventory_binding")
        files = self.B.get("sealed_files")
        require(isinstance(files, dict) and files, "empty_executable_closure")
        required = inventory.get("required_executable_paths")
        require(isinstance(required, list) and required and len(required) == len(set(required))
                and set(required) == set(files), "executable_membership")
        roles = inventory.get("root_roles")
        require(isinstance(roles, dict) and set(roles) == ROOT_ROLES and
                all(path in files for path in roles.values()), "root_role_membership")
        consumers = inventory.get("active_token_consumers")
        require(isinstance(consumers, list) and consumers and len(consumers) == len(set(consumers))
                and set(consumers) <= set(files), "token_consumer_membership")
        for path, row in files.items():
            require(row.get("kind") == "archive_member" or row.get("path") == path, "sealed_path_alias")
            source_record(row)
        seal = hashlib.sha256(json_bytes(files)).hexdigest()
        require(self.G.get("sealed_executable_index_sha256") == seal, "go_executable_seal")
        return inventory

    def check_measured_receipts(self, rows):
        require(isinstance(rows, dict) and set(rows) == CHECKS, "measured_check_membership")
        specifications = self.B.get("control_specs")
        require(isinstance(specifications, dict) and set(specifications) == CHECKS, "control_spec_membership")
        for name, file_row in rows.items():
            R = bound_json(file_row)
            self.validate_context(R)
            S = specifications[name]
            require(R.get("schema") == "cortex.gate-a.run8.measured-check.v2" and
                    R.get("check") == name and R.get("result") == "PASS", "measured_check_schema")
            for key in ("input_digests", "expected_facts", "expected_outcomes", "required_raw_roles"):
                require(S.get(key), "empty_check_contract")
            require(R.get("input_digests") == S["input_digests"], "check_input_digests")
            require(R.get("template_rule_sha256") == self.B["launch"]["template_rule_sha256"],
                    "check_template_rule")
            require(R.get("guard_authority_sha256") == self.go_sha, "check_guard_authority")
            require(R.get("measured_facts") == S["expected_facts"] and
                    R.get("outcomes") == S["expected_outcomes"], "check_measured_facts")
            raw = R.get("raw_evidence")
            require(isinstance(raw, dict) and raw and set(raw) == set(S["required_raw_roles"]),
                    "raw_provenance_membership")
            for record in raw.values():
                bound_file(record)
            # Derive the facts from bound raw bytes, not from a PASS/body declaration.
            facts_role = S.get("facts_role")
            require(facts_role in raw, "raw_facts_role_missing")
            facts = bound_json(raw[facts_role])
            self.validate_context(facts)
            require(facts.get("schema") == "cortex.gate-a.run8.raw-facts.v2", "raw_facts_schema")
            require(facts.get("facts") == R["measured_facts"], "raw_facts_disagree")
            require(facts.get("input_digests") == S["input_digests"], "raw_inputs_disagree")
            # Context-sensitive output hashes/time are observations, not future hashes in B.
            stamp = R.get("observed_at_unix")
            age_limit = S.get("max_age_seconds")
            require(type(stamp) in {int, float} and math.isfinite(stamp)
                    and type(age_limit) is int and 0 < age_limit <= 600
                    and 0 <= time.time() - stamp <= age_limit, "check_not_fresh")


def production_signature(authority, *, runner=subprocess.run):
    B = authority.B
    command = [B["verifier"]["path"], "-Vm", B["manifest"]["path"], "-x",
               B["signature"]["path"], "-p", B["production_public_key"]["path"]]
    clean = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    result = runner(command, env=clean, capture_output=True, timeout=30)
    require(result.returncode == 0, "production_signature_invalid")


def validate_release(authority, *, signature_runner=subprocess.run):
    authority.validate_files()
    production_signature(authority, runner=signature_runner)
    B = authority.B
    M = bound_json(B["manifest"])
    require(isinstance(M, dict) and M.get("schema") == "cortex.release.v1", "manifest_schema")
    for key, value in B["release"].items():
        require(M.get(key) == value, "manifest_release_identity")
    require(M.get("source_revision") == B["source_revision"], "manifest_source")
    inventory = M.get("oci_inventory")
    require(isinstance(inventory, dict) and inventory.get("source_revision") == B["source_revision"]
            and set(inventory.get("platforms", {})) == {"linux/amd64"}, "manifest_image_source_platform")
    require(set(inventory["platforms"]["linux/amd64"]) ==
            {"api", "db", "embed-worker", "graph-worker", "pdf-worker", "provider", "tls"},
            "manifest_role_closure")
    artifacts = M.get("artifacts")
    require(isinstance(artifacts, dict) and artifacts, "empty_signed_artifact_map")
    expected = B.get("signed_artifacts")
    require(isinstance(expected, dict) and set(expected) == set(artifacts), "complete_signed_map_required")
    for name, artifact in artifacts.items():
        require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
                and not name.startswith("TEST"), "artifact_name")
        row = expected[name]
        require(row["bytes"] == artifact["size"] and row["sha256"] == artifact["sha256"],
                "signed_artifact_binding")
        bound_file(row)
    return M
