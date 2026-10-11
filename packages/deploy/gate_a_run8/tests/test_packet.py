"""Fail-capable local controls; no cloud, SSH, Podman, key or native Gate."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from admission import CHECKS, load_authority, validate_release, validate_rules
from custody import Refusal, bound_json, json_bytes, measured_file, parse_json, public_data
from keys import KeyWait, classify, decoded_outcome, scan_binding
from observations import ObservationStore
from payload import pre_load, validate_literals, validate_projects
from recorder import PersistentRecorder
from restoration import validate_template
from support import Fixture, INSTANCE, VOLUME
from workflow import Workflow


class PacketCase(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)

    def refuses(self, code, fn):
        with self.assertRaises(Refusal) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_authority_positive_and_default_slots_refuse(self):
        A = load_authority(self.f.binding_row, self.f.go_row)
        self.assertEqual(A.B["source_revision"], self.f.B["source_revision"])
        default = parse_json((ROOT / "templates/static-binding.template.json").read_bytes())
        self.refuses("unbound_slot_source_revision", lambda: validate_rules(default, self.f.G, "a" * 64, "b" * 64))

    def test_static_no_observed_facts_or_hash_cycle(self):
        for field in ("instance_id", "owner_uid", "container_ids", "boot_id", "armed_receipt"):
            with self.subTest(field=field):
                B = copy.deepcopy(self.f.B); B[field] = "future"
                self.refuses("native_fact_in_static_seal", lambda: validate_rules(B, self.f.G, self.f.binding_row["sha256"], self.f.go_row["sha256"]))
        B = copy.deepcopy(self.f.B); B["go_sha256"] = "a" * 64
        self.refuses("cyclic_go_static_seal", lambda: validate_rules(B, self.f.G, self.f.binding_row["sha256"], self.f.go_row["sha256"]))

    def test_production_only_complete_asset_custody(self):
        self.assertEqual(validate_release(self.f.A, signature_runner=self.f.signature_spy)["schema"], "cortex.release.v1")
        self.f.B["signed_artifacts"] = {}
        self.refuses("complete_signed_map_required", lambda: validate_release(self.f.A, signature_runner=self.f.signature_spy))

    def test_test_trust_refuses_before_verifier(self):
        B = copy.deepcopy(self.f.B); B["trust_namespace"] = "TEST"
        self.refuses("test_trust_refused", lambda: validate_rules(B, self.f.G, self.f.binding_row["sha256"], self.f.go_row["sha256"]))

    def test_duplicate_json_nonfinite_and_secret_export(self):
        self.refuses("duplicate_json_field", lambda: parse_json('{"x":1,"x":2}'))
        self.refuses("nonfinite_json", lambda: parse_json('{"x":NaN}'))
        self.refuses("secret_field_in_receipt", lambda: public_data({"password": "fixture"}))
        self.refuses("credential_content_in_receipt", lambda: public_data({"credential_metadata": {"sha256": "a" * 64}}))
        public_data({"client_token": "public-run-nonce", "credential_metadata": {"uid": 1001, "mode": 384}})

    def test_empty_and_missing_executable_closure(self):
        self.f.A.validate_files()
        self.f.B["sealed_files"] = {}
        self.refuses("empty_executable_closure", self.f.A.validate_files)

    def test_root_roles_and_token_consumers_required(self):
        self.f.B["command_inventory"]["root_roles"].pop("manager")
        self.f.B["command_inventory_file"] = self.f.file("command-inventory.json", json_bytes(self.f.B["command_inventory"]))
        self.refuses("root_role_membership", self.f.A.validate_files)

    def test_typed_measured_receipts_and_raw_facts(self):
        self.f.A.check_measured_receipts(self.f.checks)
        row = bound_json(self.f.checks["guard"]); row["owner_tag"] = "foreign"
        self.f.checks["guard"] = self.f.file("checks/guard.json", json_bytes(row))
        self.refuses("stale_context_owner_tag", lambda: self.f.A.check_measured_receipts(self.f.checks))

    def test_header_pass_cannot_hide_wrong_raw_facts(self):
        R = bound_json(self.f.checks["guard"])
        raw = bound_json(R["raw_evidence"]["facts"]); raw["facts"] = {"fixture_actual_matches": False}
        R["raw_evidence"]["facts"] = self.f.file("raw/guard.json", json_bytes(raw))
        self.f.checks["guard"] = self.f.file("checks/guard.json", json_bytes(R))
        self.refuses("raw_facts_disagree", lambda: self.f.A.check_measured_receipts(self.f.checks))

    def test_old_timestamp_and_empty_provenance_refuse(self):
        R = bound_json(self.f.checks["guard"]); R["observed_at_unix"] = time.time() - 1000
        self.f.checks["guard"] = self.f.file("checks/guard.json", json_bytes(R))
        self.refuses("check_not_fresh", lambda: self.f.A.check_measured_receipts(self.f.checks))
        R["raw_evidence"] = {}; self.f.checks["guard"] = self.f.file("checks/guard.json", json_bytes(R))
        self.refuses("raw_provenance_membership", lambda: self.f.A.check_measured_receipts(self.f.checks))

    def store(self):
        return ObservationStore(self.f.root / "observations", self.f.A, create=True)

    def test_observation_exclusive_linked_prefix(self):
        S = self.store()
        S.append("PRE_ALLOC_CHECKED", {"checks": self.f.checks})
        S.append("ALLOCATED", {"instance_id": INSTANCE, "volume_id": VOLUME})
        rows, records = S.read()
        self.assertEqual([r["phase"] for r in rows], ["PRE_ALLOC_CHECKED", "ALLOCATED"])
        self.assertEqual(rows[1]["previous_receipt_sha256"], records[0]["sha256"])
        self.refuses("observation_phase_transition", lambda: S.append("ALLOCATED", {"instance_id": INSTANCE, "volume_id": VOLUME}))

    def test_future_phase_and_secret_observation_refuse(self):
        S = self.store()
        self.refuses("observation_phase_transition", lambda: S.append("ARMED", {}))
        self.refuses("secret_field_in_receipt", lambda: S.append("PRE_ALLOC_CHECKED", {"checks": {}, "bearer": "fixture"}))

    def test_partial_observation_is_retained_not_overwritten(self):
        S = self.store(); S.append("PRE_ALLOC_CHECKED", {"checks": {}})
        self.f.file("observations/0002-event.json", b"{}\n")
        self.refuses("uncommitted_observation_retained", S.read)
        self.assertTrue((S.directory / "0002-event.json").exists())

    def test_failure_then_retirement_is_not_recovery(self):
        S = self.store(); S.append("PRE_ALLOC_CHECKED", {"checks": {}})
        S.append("FAILURE", {"failed_phase": "allocation", "refusal": "fixture_refusal"})
        self.refuses("observation_phase_transition", lambda: S.append("ARMED", {}))
        S.append("RETIRED", {"retirement_receipt": {"fixture": True}, "custody_complete": False})
        self.refuses("observation_phase_transition", lambda: S.append("PRE_ALLOC_CHECKED", {"checks": {}}))

    def test_literals_equal_and_each_mismatch_refuses(self):
        E = self.f.literals(); validate_literals(self.f.A, E, self.f.B["delivery_binding"])
        for field in E:
            with self.subTest(field=field):
                wrong = dict(E); wrong[field] = "other"
                self.refuses("literal_command_input_mismatch", lambda: validate_literals(self.f.A, wrong, self.f.B["delivery_binding"]))

    def host_store(self):
        S = self.store(); S.append("PRE_ALLOC_CHECKED", {"checks": self.f.checks})
        S.append("ALLOCATED", {"instance_id": INSTANCE, "volume_id": VOLUME})
        S.append("HOST_BOUND", {"instance_id": INSTANCE, "owner_uid": 1001, "boot_id": "boot-fixture-1",
            "engine": {"fixture": True}, "host_key_binding": {"fixture": True},
            "local_watchdog": {"state": "ARMED"}, "guest_watchdog": {"state": "ARMED"}})
        return S

    def test_extractor_never_sees_wrong_destination(self):
        flow = Workflow(self.f.A, self.host_store(), signature_runner=self.f.signature_spy)
        calls = []
        E = self.f.literals(); E["cto_payload"] = str(self.f.root / "foreign")
        self.refuses("literal_command_input_mismatch", lambda: flow.extract(E, self.f.B["delivery_binding"], calls.append))
        self.assertEqual(calls, [])
        flow.extract(self.f.literals(), self.f.B["delivery_binding"], calls.append)
        self.assertEqual(calls, [self.f.literals()])

    def test_source_closed_installation_before_loader_effect(self):
        flow = Workflow(self.f.A, self.host_store(), signature_runner=self.f.signature_spy)
        calls = []
        environment = {"CORTEX_PROJECTS_ROOT": self.f.B["projects_root"]}
        flow.load_images(self.f.literals(), self.f.B["delivery_binding"], environment, calls.append)
        self.assertEqual(len(calls), 1); self.assertTrue(calls[0]["before_image_load"])
        install = dict(self.f.install); install["files"] = {}
        self.f.file("payload/packages/deploy/install.json", json_bytes(install))
        with self.assertRaises(ValueError):
            flow.load_images(self.f.literals(), self.f.B["delivery_binding"], environment, calls.append)
        self.assertEqual(len(calls), 1)

    def test_source_delivery_and_external_compose_refuse(self):
        install = dict(self.f.install); install["delivery_kind"] = "source"
        self.f.file("payload/packages/deploy/install.json", json_bytes(install))
        with self.assertRaisesRegex(ValueError, "prebuilt installation identity"):
            pre_load(self.f.A, self.f.manifest, self.f.literals(), self.f.B["delivery_binding"])

    def test_latest_projects_predicate_owner_positive_and_reserved_negative(self):
        values = validate_projects(self.f.A, self.f.literals(), self.f.B["delivery_binding"],
            {"CORTEX_PROJECTS_ROOT": self.f.B["projects_root"]})
        self.assertTrue(values["predicate_ast_sha256"])
        self.f.B["projects_root"] = "/usr"
        with self.assertRaisesRegex(ValueError, "reserved"):
            validate_projects(self.f.A, self.f.literals(), self.f.B["delivery_binding"], {"CORTEX_PROJECTS_ROOT": "/usr"})

    def test_integrated_console_decode_extract_count_and_caller(self):
        cases = [("", "donor-auto", 75), ("ssh-ed25519 AAAA one\n", "donor-auto", 0),
                 ("ssh-ed25519 AAAA one\nssh-ed25519 BBBB two\n", "donor-auto", 76),
                 (base64.b64encode(b"boot without key\n").decode(), "base64", 75),
                 (base64.b64encode(b"ssh-ed25519 AAAA one\n").decode(), "base64", 0)]
        for text, representation, code in cases:
            with self.subTest(code=code, representation=representation):
                self.assertEqual(decoded_outcome(text, representation)["exit"], code)
        self.refuses("STOP_CONSOLE_DECODE_FAILURE", lambda: decoded_outcome("not-base64 !", "donor-auto"))
        self.refuses("STOP_CONSOLE_DECODE_FAILURE", lambda: decoded_outcome(base64.b64encode(b"\xff").decode(), "base64"))

    def test_conflict_scan_mismatch_and_bounded_wait(self):
        self.assertEqual(scan_binding("ssh-ed25519 AAAA one\n", "host ssh-ed25519 AAAA\n", "host")["state"], "BOUND")
        self.refuses("host_key_mismatch", lambda: scan_binding("ssh-ed25519 AAAA one\n", "host ssh-ed25519 BBBB\n", "host"))
        wait = KeyWait(0); wait.admit_attempt(0, True)
        self.assertEqual(wait.retry(classify([]), 1), 60)
        self.refuses("key_wait_too_frequent", lambda: wait.admit_attempt(59, True))
        wait.admit_attempt(60, True)
        self.refuses("key_failure_not_retryable", lambda: wait.retry(classify(["AAAA", "BBBB"]), 61))
        self.refuses("key_wait_deadline", lambda: wait.admit_attempt(600, True))

    def test_restoration_scope_and_order_template(self):
        body = (ROOT / "templates/restoration.service.in").read_bytes(); validate_template(body)
        for bad in (body.replace(b"Restart=no", b"Restart=always"), body.replace(b"[Service]", b"After=default.target\n[Service]"),
                    body.replace(b" start @PG@", b" start --all @PG@"), body.replace(b"--condition=healthy", b"--condition=running")):
            with self.assertRaises(Refusal): validate_template(bad)

    def test_recorder_same_bash_and_failure_cleanup(self):
        R = PersistentRecorder(["/bin/bash", "-s"], str(self.f.root), {"PATH": "/usr/bin:/bin"})
        self.addCleanup(R.close)
        one = self.f.file("literal1.sh", b"run8_value=42\n")
        two = self.f.file("literal2.sh", b"test \"$run8_value\" = 42\nprintf 'effect-ok\\n'\n")
        R.run("one", one, observer=lambda out, err: {"initialized": True})
        receipt = R.run("two", two, observer=lambda out, err: {"effect": out.decode().strip()})
        self.assertEqual(receipt["post_effect"], {"effect": "effect-ok"})
        bad = self.f.file("literal-bad.sh", b"false\nprintf 'must-not-run\\n'\n")
        self.refuses("literal_failed_or_missing_ack", lambda: R.run("bad", bad, timeout=2, observer=lambda out, err: {}))
        self.assertIsNotNone(R.process.poll())

    def test_recorder_close_releases_every_pipe(self):
        R = PersistentRecorder(["/bin/bash", "-s"], str(self.f.root), {"PATH": "/usr/bin:/bin"})
        R.close()
        self.assertTrue(all(stream.closed for stream in (R.process.stdin, R.process.stdout, R.process.stderr)))

    def test_cli_default_template_refuses_no_target(self):
        result = subprocess.run([sys.executable, str(ROOT / "run8.py"), "inspect-template", "--binding",
                                 str(ROOT / "templates/static-binding.template.json")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["execution_ready"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
