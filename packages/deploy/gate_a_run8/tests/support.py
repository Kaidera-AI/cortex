"""Synthetic filesystem/control fixtures; no production signature or target proof."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

from admission import Authority, CHECKS, GO_LITERAL, OPERATION, ROOT_ROLES, validate_rules
from custody import json_bytes, measured_file


ROOT = Path(__file__).resolve().parents[1]
SOURCE = "8e854269efd5da8522c2b9b4c2981034f5378c37"
INSTANCE = "i-" + "a" * 17
VOLUME = "vol-" + "b" * 17


class Fixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="run8-synthetic-", dir=ROOT / "tests")
        self.root = Path(self.temp.name).resolve()
        self.B = {"schema": "cortex.gate-a.run8.static.v2", "operation": OPERATION,
                  "runtime_owner_name": "kos", "owner_tag": "ren-cx-fixture", "run_id": "run8-fixture",
                  "client_token": "helix-cortex-gate-a-run8-20261011-launch1", "source_revision": SOURCE,
                  "source_tree": "1" * 40, "schema_revision": "2" * 64, "trust_namespace": "production",
                  "release": {"release_id": "v0.1.003-manual.1", "release_lineage": "cortex-v1-manual",
                              "release_sequence": 1, "api_contract": "cortex-kos-v02009.v1"},
                  "envelope": {"max_allocations": 1, "budget_usd": 3, "retirement_reserve_usd": 1.5,
                               "stop_new_minutes": 210, "watchdog_minutes": 230, "absolute_minutes": 240},
                  "restoration": {"kind": "template-and-observed-ID-rule"}, "launch": {"template_rule_sha256": "3" * 64}}
        tool = self.file("tools/mock-tool", b"#!/bin/sh\nexit 0\n", mode=0o500)
        public = self.file("public-fixture.data", b"NOT A KEY; SYNTHETIC VERIFIER TEST DATA\n")
        self.B["production_public_key"] = public
        self.B["trusted_public_key_sha256"] = public["sha256"]
        self.B["verifier"] = tool
        self.B["signature"] = self.file("signature-fixture.data", b"NO CRYPTOGRAPHIC SIGNATURE CLAIM\n")
        for name in ("c_receipt", "main_receipt", "vera_packet_receipt"):
            self.B[name] = self.file(name + ".json", json_bytes({"fixture_only": True}))
        roles = {role: tool["path"] for role in ROOT_ROLES}
        files = {tool["path"]: tool}
        inventory = {"required_executable_paths": list(files), "root_roles": roles,
                     "active_token_consumers": list(files)}
        self.B["command_inventory"] = inventory
        self.B["command_inventory_file"] = self.file("command-inventory.json", json_bytes(inventory))
        self.B["sealed_files"] = files
        self._payload()
        self.B["helper_ledger"] = self.file("helper-ledger.json", json_bytes({"application": SOURCE, "files": {}}))
        self.B["delivery_binding"] = self.file("delivery.json", json_bytes({
            "payload_dir": self.B["payload_dir"], "runtime_sha256": self.runtime["sha256"], "cortex_source": SOURCE}))
        template = self.file("restoration.service.in", (ROOT / "templates/restoration.service.in").read_bytes())
        self.B["restoration"]["template"] = template
        self.B["kos_signed_binding"] = {"source_revision": "4" * 40}
        self.B["phase_command_inputs"] = {phase: {"fixture": {"input": "5" * 64}} for phase in
            ("PRE_ALLOC_CHECKED", "ALLOCATED", "HOST_BOUND", "INSTALLED", "ARMED", "REBOOT_VERIFIED", "FAILURE", "RETIRED")}
        self.B["commands"] = {}
        self.B["control_specs"] = {name: {"input_digests": {"static": "6" * 64},
            "expected_facts": {"fixture_actual_matches": True},
            "expected_outcomes": {"positive": "PASS", "negative": "REFUSED"},
            "required_raw_roles": ["facts"], "facts_role": "facts", "max_age_seconds": 600} for name in CHECKS}
        self.refresh()
        self.checks = self.measured_checks()

    def file(self, name, body, mode=0o600):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(body)
        path.chmod(mode)
        return measured_file(path)

    def _payload(self):
        payload = self.root / "payload"
        helper = (ROOT / "tests/fixtures/image_manifest.py.txt").read_bytes()
        runtime = (ROOT / "tests/fixtures/cortex-runtime.txt").read_bytes()
        self.image = self.file("payload/packages/deploy/image_manifest.py", helper)
        self.runtime = self.file("payload/packages/deploy/cortex-runtime", runtime)
        N = {"__name__": "synthetic_exact_source_reader"}
        exec(compile(helper, "<source-fixture>", "exec"), N)
        identity = {"schema": "cortex.payload.v1", "version": "0.1.003-manual.1",
                    "source_revision": SOURCE, "schema_revision": self.B["schema_revision"]}
        self.identity_row = self.file("payload/packages/deploy/release.json", json_bytes(identity))
        locks = {role: {"repository": "ghcr.io/kaidera-ai/cortex-" + role,
                       "tag": "v0.1.003-manual.1", "manifest_digest": "sha256:" + "7" * 64}
                 for role in set(N["SERVICE_ROLES"].values())}
        self.inventory = {"schema": "cortex.images.v1", "version": identity["version"],
                          "source_revision": SOURCE, "platforms": {"linux/amd64": locks}}
        compose = {"networks": {"cortex-net": {}}, "services": {}}
        for service, role in N["SERVICE_ROLES"].items():
            row = {"image": N["image_ref"](locks[role]), "platform": "linux/amd64", "pull_policy": "never"}
            row.update({"network_mode": "none"} if service in {"cortex-tls-init", "cortex-pki-restore"}
                       else {"networks": ["cortex-net"]})
            if service == "cortex-api":
                row["ports"] = ["127.0.0.1:${CORTEX_API_PORT:-8501}:8501"]
            compose["services"][service] = row
        self.compose = compose
        self.file("payload/PROJECTION_MANIFEST.json", b"{}\n")
        self.file("payload/packages/deploy/install-compose.json", json_bytes(compose))
        self.file("payload/packages/deploy/image-lock.json", json_bytes(self.inventory))
        members = [p for p in payload.rglob("*") if p.is_file()]
        install = {**identity, "schema": "cortex.install.v1", "delivery_kind": "prebuilt", "platform": "linux/amd64",
                   "files": {p.relative_to(payload).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in members}}
        self.install = install
        self.file("payload/packages/deploy/install.json", json_bytes(install))
        self.B["payload_files"] = {p.relative_to(payload).as_posix(): measured_file(p)
                                   for p in payload.rglob("*") if p.is_file()}
        self.B["payload_dir"] = str(payload)
        self.B["source_members"] = {"packages/deploy/image_manifest.py": self.image,
                                    "packages/deploy/cortex-runtime": self.runtime,
                                    "packages/deploy/release.json": self.identity_row}
        projects = self.root / "projects"
        projects.mkdir(mode=0o700)
        self.B["projects_root"] = str(projects)
        artifact = self.file("artifacts/synthetic-artifact.data", b"SYNTHETIC MAP, NO OCI OR NATIVE CLAIM\n")
        self.manifest = {"schema": "cortex.release.v1", **self.B["release"], "source_revision": SOURCE,
                         "oci_inventory": self.inventory,
                         "artifacts": {"synthetic-artifact.data": {"size": artifact["bytes"], "sha256": artifact["sha256"]}}}
        self.B["manifest"] = self.file("manifest.json", json_bytes(self.manifest))
        self.B["signed_artifacts"] = {"synthetic-artifact.data": artifact}

    def refresh(self):
        self.binding_row = self.file("static.json", json_bytes(self.B))
        self.G = {"schema": "cortex.gate-a.run8.go.v2", "literal": GO_LITERAL,
                  **{k: self.B[k] for k in ("operation", "run_id", "client_token", "owner_tag", "source_revision")},
                  "static_binding_sha256": self.binding_row["sha256"],
                  "sealed_executable_index_sha256": hashlib.sha256(json_bytes(self.B["sealed_files"])).hexdigest(),
                  "scope": "one-post-sign-install-and-owned-retirement", "envelope": self.B["envelope"]}
        self.go_row = self.file("GO.fixture.md", (GO_LITERAL + "\n```json\n").encode() + json_bytes(self.G) + b"```\n")
        validate_rules(self.B, self.G, self.binding_row["sha256"], self.go_row["sha256"])
        self.A = Authority(self.B, self.G, self.binding_row["sha256"], self.go_row["sha256"])

    def measured_checks(self):
        checks = {}
        for name, spec in self.B["control_specs"].items():
            raw = self.file("raw/" + name + ".json", json_bytes({
                "schema": "cortex.gate-a.run8.raw-facts.v2", **self.A.context,
                "facts": spec["expected_facts"], "input_digests": spec["input_digests"]}))
            report = {"schema": "cortex.gate-a.run8.measured-check.v2", **self.A.context,
                      "check": name, "result": "PASS", "input_digests": spec["input_digests"],
                      "template_rule_sha256": self.B["launch"]["template_rule_sha256"],
                      "guard_authority_sha256": self.A.go_sha, "measured_facts": spec["expected_facts"],
                      "outcomes": spec["expected_outcomes"], "raw_evidence": {"facts": raw},
                      "observed_at_unix": time.time()}
            checks[name] = self.file("checks/" + name + ".json", json_bytes(report))
        return checks

    def literals(self):
        return {"cto_payload": self.B["payload_dir"], "cto_runtime": self.runtime["path"],
                "cto_runtime_sha": self.runtime["sha256"]}

    def signature_spy(self, *args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=b"UNIT SPY ONLY", stderr=b"")

    def close(self):
        self.temp.cleanup()
