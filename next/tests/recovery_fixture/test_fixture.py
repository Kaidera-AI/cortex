"""X01a RED-first ledger, fault-reconciliation and edition adapter controls."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cortex_core.recovery_fixture import (
    AckLedger, RecoveryError, capture_write, fault_plan, native_restore_plan, reconcile,
    synthetic_fixture,
)


HASH = "a" * 64


def request(n=1):
    return {"request_id": f"req-{n}", "record_id": f"record-{n}",
            "revision": n, "payload_sha256": HASH, "tombstone": False,
            "ack_ns": n * 1_000_000_000}


def result(n=1, state="committed"):
    return {"request_id": f"req-{n}", "record_id": f"record-{n}",
            "revision": n, "payload_sha256": HASH, "tombstone": False,
            "state": state}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cox-x01a-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger_dir = self.root / "controller"
        self.fault = self.root / "faulted-primary"
        self.ledger_dir.mkdir()
        self.fault.mkdir()

    def ledger(self):
        return AckLedger(self.ledger_dir, [self.fault])

    def test_external_ledger_rejects_faulted_root_and_symlink(self):
        with self.assertRaises(RecoveryError):
            AckLedger(self.fault, [self.fault])
        linked = self.root / "linked"
        linked.symlink_to(self.ledger_dir, target_is_directory=True)
        with self.assertRaises(RecoveryError):
            AckLedger(linked, [self.fault])

    def test_only_matching_server_confirmed_commit_enters_ack_chain(self):
        ledger = self.ledger()
        with self.assertRaises(RecoveryError):
            ledger.append_ack(request(), result(state="timed_out"))
        wrong = result()
        wrong["revision"] = 2
        with self.assertRaises(RecoveryError):
            ledger.append_ack(request(), wrong)
        self.assertEqual(ledger.read(), [])
        ledger.append_ack(request(), result())
        self.assertEqual([row["request_id"] for row in ledger.read()], ["req-1"])
        self.assertEqual(len(ledger.digest()), 64)
        with self.assertRaises(RecoveryError):
            ledger.append_ack(request(), result())

    def test_ambiguous_is_separate_and_can_later_resolve(self):
        ledger = self.ledger()
        ledger.mark_ambiguous(request(), "timeout_after_send")
        self.assertEqual(ledger.read(), [])
        self.assertEqual(ledger.read("ambiguous")[0]["request_id"], "req-1")
        ledger.append_ack(request(), result())
        self.assertEqual(len(ledger.read()), 1)

    def test_tampered_or_truncated_chain_refuses_before_next_append(self):
        ledger = self.ledger()
        ledger.append_ack(request(), result())
        ledger.append_ack(request(2), result(2))
        path = self.ledger_dir / "acks.jsonl"
        original = path.read_bytes()
        path.write_bytes(original.replace(b"record-1", b"record-X"))
        with self.assertRaises(RecoveryError):
            ledger.read()
        with self.assertRaises(RecoveryError):
            ledger.append_ack(request(3), result(3))
        path.write_bytes(original[:-3])
        with self.assertRaises(RecoveryError):
            ledger.read()

    def test_digest_detects_complete_tail_truncation_against_prefault_copy(self):
        ledger = self.ledger()
        ledger.append_ack(request(), result())
        ledger.append_ack(request(2), result(2))
        before = ledger.digest()
        path = self.ledger_dir / "acks.jsonl"
        path.write_bytes(path.read_bytes().splitlines(keepends=True)[0])
        self.assertNotEqual(ledger.digest(), before)

    def test_single_entry_content_hash_is_checked(self):
        ledger = self.ledger()
        ledger.append_ack(request(), result())
        path = self.ledger_dir / "acks.jsonl"
        path.write_bytes(path.read_bytes().replace(b"record-1", b"record-X"))
        with self.assertRaises(RecoveryError):
            ledger.read()

    def test_controller_records_ack_only_after_confirmed_server_callback(self):
        ledger = self.ledger()
        calls = []
        pending = {key: value for key, value in request().items() if key != "ack_ns"}
        def send(value):
            calls.append(value)
            self.assertEqual(ledger.read(), [])
            return result()
        self.assertEqual(capture_write(send, pending, ledger, clock_ns=lambda: 4_000_000_000),
                         "committed")
        self.assertEqual(calls, [pending])
        self.assertEqual(ledger.read()[0]["ack_ns"], 4_000_000_000)

    def test_controller_timeout_is_ambiguous_not_ack(self):
        ledger = self.ledger()
        pending = {key: value for key, value in request().items() if key != "ack_ns"}
        def send(_):
            raise TimeoutError("after-send timeout")
        self.assertEqual(capture_write(send, pending, ledger, clock_ns=lambda: 4_000_000_000),
                         "ambiguous")
        self.assertEqual(ledger.read(), [])
        self.assertEqual(ledger.read("ambiguous")[0]["reason"], "timeout_after_send")


class ReconciliationTests(unittest.TestCase):
    def check(self, recovered, **changes):
        values = dict(fault_ns=5_000_000_000, detect_mono_ns=10_000_000_000,
                      read_mono_ns=11_000_000_000, write_mono_ns=12_000_000_000,
                      rpo_limit_ns=3_000_000_000, rto_limit_ns=30_000_000_000)
        values.update(changes)
        return reconcile([request(1), request(2), request(3)], recovered, **values)

    def test_contiguous_prefix_reports_lost_ack_and_measured_windows(self):
        verdict = self.check([result(1), result(2)])
        self.assertEqual(verdict["recovered_ack_count"], 2)
        self.assertEqual(verdict["lost_ack_ids"], ["req-3"])
        self.assertEqual(verdict["rpo_ns"], 3_000_000_000)
        self.assertEqual(verdict["rto_ns"], 2_000_000_000)
        self.assertTrue(verdict["passed"])

    def test_noncontiguous_or_altered_recovery_is_refused(self):
        for rows in ([result(1), result(3)], [result(1), dict(result(2), payload_sha256="b"*64)],
                     [result(1), result(2), result(3), result(4)]):
            with self.subTest(rows=rows), self.assertRaises(RecoveryError):
                self.check(rows)

    def test_rpo_or_rto_miss_is_red_not_averaged(self):
        self.assertFalse(self.check([result(1)], rpo_limit_ns=1_000_000_000)["passed"])
        self.assertFalse(self.check([result(1), result(2)],
                                    write_mono_ns=41_000_000_001)["passed"])
        with self.assertRaises(RecoveryError):
            self.check([], fault_ns=0)


class NativeAdapterTests(unittest.TestCase):
    def plan(self, edition="production-linux", **changes):
        base = dict(os_name="Linux", arch="x86_64", image_ref="cortex@sha256:" + HASH,
                    source_volume="kaidera-test-cortex-primary",
                    archive_volume="kaidera-test-cortex-archive",
                    target_volume="kaidera-test-cortex-recovered",
                    target_container="kaidera-test-cortex-recovery", cpus=2,
                    memory_gib=4)
        if edition == "standalone-macos":
            base.update(os_name="Darwin", arch="arm64", memory_gib=2)
        base.update(changes)
        return native_restore_plan(edition, **base)

    def test_both_editions_render_native_dry_run_without_host_execution(self):
        for edition in ("production-linux", "standalone-macos"):
            with self.subTest(edition=edition):
                plan = self.plan(edition)
                self.assertEqual(plan["edition"], edition)
                self.assertTrue(plan["dry_run"])
                self.assertEqual(plan["restore_volume"], "kaidera-test-cortex-recovered")
                argv = plan["commands"]
                self.assertTrue(any("pg_verifybackup" in part for cmd in argv for part in cmd))
                self.assertTrue(any("pg_ctl" in part for cmd in argv for part in cmd))
                flat = " ".join(part for cmd in argv for part in cmd)
                self.assertNotIn("--publish", flat)
                self.assertNotIn("type=bind", flat)
                self.assertIn("--network none", flat)
                self.assertIn("/wal", flat)
                self.assertIn("-w /wal", flat)
                self.assertIn("--detach", argv[2])

    def test_wrong_platform_or_shared_volume_refused_before_plan(self):
        with self.assertRaises(RecoveryError):
            self.plan(os_name="Darwin")
        with self.assertRaises(RecoveryError):
            self.plan("standalone-macos", arch="x86_64")
        with self.assertRaises(RecoveryError):
            self.plan(target_volume="kaidera-test-cortex-primary")
        with self.assertRaises(RecoveryError):
            self.plan(archive_volume="kaidera-test-cortex-primary")
        with self.assertRaises(RecoveryError):
            self.plan(image_ref="cortex:latest")
        with self.assertRaises(RecoveryError):
            self.plan("standalone-macos", cpus=5)


class FaultFixtureTests(unittest.TestCase):
    def test_synthetic_fixture_is_deterministic_and_cross_domain(self):
        first = synthetic_fixture("x01a-seed")
        self.assertEqual(first, synthetic_fixture("x01a-seed"))
        self.assertEqual(len(first["tenants"]), 2)
        self.assertEqual(len({row["tenant_id"] for row in first["records"]}), 2)
        self.assertTrue(first["blobs"])
        self.assertTrue(first["vectors"])
        self.assertTrue(first["consumer_checkpoints"])
        self.assertTrue(all(len(row["payload_sha256"]) == 64 for row in first["records"]))

    def test_faults_only_name_owned_synthetic_primary(self):
        fixture = synthetic_fixture("x01a-fault")
        record_id = fixture["records"][0]["record_id"]
        for kind in ("primary", "storage", "logical"):
            with self.subTest(kind=kind):
                plan = fault_plan(kind, edition="production-linux",
                                  primary_container="kaidera-test-cortex-primary",
                                  primary_volume="kaidera-test-cortex-primary-data",
                                  archive_volume="kaidera-test-cortex-archive",
                                  record_id=record_id, fixture=fixture)
                self.assertEqual(plan["fault"], kind)
                self.assertTrue(plan["dry_run"])
                self.assertNotEqual(plan["target"], "kaidera-test-cortex-archive")
        with self.assertRaises(RecoveryError):
            fault_plan("storage", edition="production-linux",
                       primary_container="kaidera-test-cortex-primary",
                       primary_volume="kaidera-test-cortex-archive",
                       archive_volume="kaidera-test-cortex-archive",
                       record_id=record_id, fixture=fixture)
        with self.assertRaises(RecoveryError):
            fault_plan("primary", edition="production-linux",
                       primary_container="cortex-live", primary_volume="kaidera-test-cortex-primary-data",
                       archive_volume="kaidera-test-cortex-archive",
                       record_id=record_id, fixture=fixture)
        with self.assertRaises(RecoveryError):
            fault_plan("logical", edition="production-linux",
                       primary_container="kaidera-test-cortex-primary",
                       primary_volume="kaidera-test-cortex-primary-data",
                       archive_volume="kaidera-test-cortex-archive",
                       record_id="00000000-0000-4000-8000-000000000001",
                       fixture=fixture)


if __name__ == "__main__":
    unittest.main()
