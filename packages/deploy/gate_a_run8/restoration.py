"""Render admitted existing-container units and validate observed reboot effects."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

from custody import bound_file, bound_json, hex_value, require


SLOTS = {"PG": "cortex-pg", "PROVIDER": "cortex-provider", "API": "cortex-api",
         "EMBED": "cortex-embed-worker", "GRAPH": "cortex-graph-worker",
         "PDF": "cortex-pdf-worker", "APPDB": "kos-appdb"}
UNIT_NAME = "kaidera-dev-cortex-restoration-8.service"


def validate_template(body):
    text = body.decode()
    require("Type=oneshot" in text and "RemainAfterExit=yes" in text and "Restart=no" in text,
            "restoration_unit_type")
    require("After=default.target" not in text and "WantedBy=default.target" in text,
            "restoration_ordering_cycle")
    require(all(x not in text for x in ("--all", "--latest", "--ignore", "ExecStart=-", "User=root",
                                        "cortex-migrate", "cortex-tls-init", "compose up")),
            "restoration_scope_or_ignored_failure")
    require(set(re.findall(r"@([A-Z]+)@", text)) == {*SLOTS, "PODMAN"}, "restoration_slot_membership")
    starts = re.findall(r"^ExecStart=(.*)$", text, re.M)
    stops = re.findall(r"^ExecStop=(.*)$", text, re.M)
    require(len(starts) == 8 and len(stops) == 3 and
            sum("--condition=healthy" in x for x in starts) == 4,
            "restoration_health_barriers")
    require(starts[0].endswith("start @PG@ @APPDB@") and
            starts[2].endswith("start @PROVIDER@") and starts[4].endswith("start @API@"),
            "restoration_dependency_order")
    require("TimeoutStartSec=900" in text and "ExecStartPre=/usr/bin/sha256sum --check " in text,
            "restoration_deadline_or_custody")
    return text


def render(authority, installed, host, *, template_row):
    require(template_row == authority.B["restoration"]["template"], "restoration_template_alias")
    text = validate_template(bound_file(template_row, read=True, limit=64 * 1024))
    identities = installed.get("container_identity_map")
    require(isinstance(identities, dict) and set(identities) == set(SLOTS.values()),
            "restoration_container_membership")
    ids = [identities[name].get("container_id") for name in SLOTS.values()]
    require(all(hex_value(cid) for cid in ids) and len(ids) == len(set(ids)),
            "restoration_full_unique_ids")
    require(host["owner_uid"] > 0 and installed["instance_id"] == host["instance_id"],
            "restoration_owner_host")
    for name, row in identities.items():
        require(row.get("owner_uid") == host["owner_uid"] and row.get("running") is True
                and row.get("healthy") is True, "restoration_initial_effect")
        require(row.get("source_revision") == (authority.B["kos_signed_binding"]["source_revision"]
                if name == "kos-appdb" else authority.B["source_revision"]), "restoration_source")
    podman = str(bound_file(host["engine"]["executable"]))
    require(not any(c.isspace() or c in "@%" for c in podman), "restoration_engine_path")
    text = text.replace("@PODMAN@", podman)
    for slot, name in SLOTS.items():
        text = text.replace("@" + slot + "@", identities[name]["container_id"])
    require(not re.search(r"@[A-Z]+@", text), "restoration_unbound_slot")
    return text.encode()


def armed(authority, installed, host, row):
    receipt = bound_json(row)
    authority.validate_context(receipt)
    require(receipt.get("schema") == "cortex.gate-a.run8.restoration-armed.v2",
            "restoration_armed_schema")
    require(receipt.get("instance_id") == host["instance_id"] and
            receipt.get("owner_uid") == host["owner_uid"], "restoration_armed_host_owner")
    properties = receipt.get("unit_properties", {})
    expected = {"LoadState": "loaded", "UnitFileState": "enabled", "ActiveState": "active",
                "SubState": "exited", "Result": "success", "ExecMainStatus": 0,
                "Restart": "no", "TimeoutStartUSec": 900000000}
    require(all(type(properties.get(k)) is type(v) and properties[k] == v for k, v in expected.items()),
            "restoration_unit_not_armed")
    require(receipt.get("linger") == "yes" and receipt.get("same_owner_rootless_private_store") is True
            and receipt.get("console_dependency_effective") is True, "restoration_manager_dependency")
    require(receipt.get("container_identity_map") == installed["container_identity_map"],
            "restoration_armed_identity")
    require(receipt.get("boot_id_before") == host["boot_id"], "restoration_initial_boot")
    require(receipt.get("reboot_recovery") == "NOT_RUN", "arming_is_not_recovery")
    actual_unit = bound_file(receipt["unit_file"], read=True, limit=64 * 1024)
    expected_unit = render(authority, installed, host, template_row=authority.B["restoration"]["template"])
    require(actual_unit == expected_unit, "restoration_filled_unit_drift")
    for key in ("console_dropin", "input_checksum_map", "native_properties_receipt"):
        bound_file(receipt[key])
    require(receipt.get("cortex_check") == installed["cortex_check"] and
            receipt.get("appdb_doctor") == installed["initial_appdb_doctor"], "restoration_before_effects")
    return receipt


def recovered(authority, installed, initial_armed, row):
    receipt = bound_json(row)
    authority.validate_context(receipt)
    require(receipt.get("schema") == "cortex.gate-a.run8.recovery.v2", "recovery_schema")
    require(receipt.get("instance_id") == initial_armed["instance_id"] and
            receipt.get("owner_uid") == initial_armed["owner_uid"], "recovery_host_owner")
    require(isinstance(receipt.get("boot_id_after"), str) and
            receipt["boot_id_after"] != initial_armed["boot_id_before"], "same_boot_recovery_refused")
    require(receipt.get("manual_start_or_repair") is False and
            receipt.get("unit_new_boot_success") is True, "automatic_recovery_missing")
    require(receipt.get("container_identity_map") == installed["container_identity_map"] and
            receipt.get("cortex_check") == installed["cortex_check"] and
            receipt.get("appdb_doctor") == installed["initial_appdb_doctor"] and
            receipt.get("cm1_sha256") == installed["cm1_sha256"] and
            receipt.get("credential_metadata") == installed["credential_metadata"], "recovery_identity_drift")
    require(receipt.get("denials_pass") is True and receipt.get("console_health_pass") is True
            and receipt.get("console_service_active") is True, "recovery_effects_missing")
    for record in receipt.get("raw_evidence", {}).values():
        bound_file(record)
    require(receipt.get("raw_evidence"), "recovery_raw_proof_missing")
    return receipt
