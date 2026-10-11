"""Frozen causal requirements, derived from actual attempts5–14."""

import importlib.util
import marshal
import struct
import unittest
from cortex_core.release.build import policy, audit, compare, receipts


class Attempts(unittest.TestCase):
    def test_A05_creating_uid_cleanup_cannot_remove_root_owned_path(self):
        with self.assertRaises(ValueError):
            policy.cleanup_paths(10001, [{"path": "var/cache/ldconfig", "uid": 0}])

    def test_A06_complete_argv_has_one_clock_channel(self):
        argv = policy.build_argv("/usr/bin/podman", "source", "output", 123)
        self.assertFalse(
            "--timestamp" in argv
            and any(str(v).startswith("SOURCE_DATE_EPOCH=") for v in argv)
        )
        self.assertIn("--source-date-epoch", argv)
        self.assertIn("--rewrite-timestamp", argv)

    def test_A07_runtime_label_drop_is_not_admitted_by_raw_repetition(self):
        with self.assertRaises(ValueError):
            audit.compare_metadata(
                {"Labels": {"io.buildah.version": "1.43.1"}}, {"Labels": {}}
            )

    def test_A08_stale_or_changed_source_cache_refuses(self):
        source = b"VALUE=1\n"
        raw = (
            importlib.util.MAGIC_NUMBER
            + struct.pack("<I", 3)
            + importlib.util.source_hash(source)
            + marshal.dumps(compile(source, "/app/module.py", "exec"))
        )
        with self.assertRaises(ValueError):
            audit.audit_cache(raw, b"VALUE=2\n", "/app/module.py")

    def test_A09_body_only_mutation_refuses_with_same_header_filename(self):
        source = b"VALUE=1\n"
        raw = (
            importlib.util.MAGIC_NUMBER
            + struct.pack("<I", 3)
            + importlib.util.source_hash(source)
            + marshal.dumps(compile(source + b"\nMUTANT=1\n", "/app/module.py", "exec"))
        )
        with self.assertRaises(ValueError):
            audit.audit_cache(raw, source, "/app/module.py")

    def test_A10_final_filename_serialization_mismatch_refuses(self):
        source = b"VALUE=1\n"
        raw = (
            importlib.util.MAGIC_NUMBER
            + struct.pack("<I", 3)
            + importlib.util.source_hash(source)
            + marshal.dumps(compile(source, "/temporary/module.py", "exec"))
        )
        with self.assertRaises(ValueError):
            audit.audit_cache(raw, source, "/app/module.py")

    def test_A11_account_log_and_unknown_acl_drift_refuses(self):
        for before, after in [
            ({"shadow": "locked:123"}, {"shadow": "locked:124"}),
            ({"log": b"one"}, {"log": b"two"}),
            ({"xattrs": {}}, {"xattrs": {"SCHILY.acl.access": "unreviewed"}}),
        ]:
            with self.subTest(before=before), self.assertRaises(ValueError):
                audit.compare_metadata(before, after)

    def test_A12_missing_actual_cli_exit_is_not_success(self):
        with self.assertRaises(ValueError):
            receipts.require_complete(
                {"result": "PASS", "exit": None, "custody": True, "cleanup": True}
            )

    def test_A13_wrong_native_uid_or_platform_not_qualified(self):
        with self.assertRaises(ValueError):
            policy.verify_native(
                {"os": "darwin", "arch": "arm64", "uid": 0, "rootless": False},
                "linux/arm64",
            )

    def test_A14_zero_node_false_success_and_missing_offline_grammar_refuse(self):
        with self.assertRaises(ValueError):
            audit.require_useful_result("ok", 0, ["grammar unavailable"])
        with self.assertRaises(ValueError):
            audit.validate_offline_assets({"python": "1" * 64}, {})

    def test_whole_archive_inequality_is_RED(self):
        with self.assertRaises(ValueError):
            compare.compare_archives(b"archiveA", b"archiveB")

    def test_two_exports_of_one_build_are_not_independent(self):
        lane = {
            "build_id": "same",
            "source": "same",
            "store": "same",
            "runroot": "same",
            "output": "same",
        }
        with self.assertRaises(ValueError):
            compare.require_independent(lane, lane)

    def test_missing_cleanup_never_admits_receipt(self):
        with self.assertRaises(ValueError):
            receipts.require_complete(
                {"result": "PASS", "exit": 0, "custody": True, "cleanup": False}
            )

    def test_changed_module_checked_before_any_execution(self):
        with self.assertRaises(ValueError):
            receipts.verify_checked_bytes(b"changed", "0" * 64)
