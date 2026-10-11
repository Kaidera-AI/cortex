import hashlib
import tempfile
import unittest
from pathlib import Path
from cortex_core.release.build import runner, custody


class Lifecycle(unittest.TestCase):
    def test_early_preparation_failure_always_custodies_then_cleans(self):
        events = []
        records = []

        def fail(stage):
            events.append(stage)
            raise TimeoutError("slow transport")

        result = runner.lifecycle(
            ["prepare", "build"],
            fail,
            lambda: events.append("custody") or {"complete": False},
            lambda: events.append("cleanup")
            or {"complete": True, "remaining_owned_resources": []},
            records.append,
        )
        self.assertEqual(events, ["prepare", "custody", "cleanup"])
        self.assertEqual(result["result"], "RED")
        self.assertEqual(records, [result])

    def test_custody_failure_does_not_suppress_cleanup(self):
        events = []

        def fail():
            events.append("custody")
            raise OSError()

        result = runner.lifecycle(
            [],
            lambda s: None,
            fail,
            lambda: events.append("cleanup")
            or {"complete": True, "remaining_owned_resources": []},
            lambda r: None,
        )
        self.assertEqual(events, ["custody", "cleanup"])
        self.assertEqual(result["result"], "RED")

    def test_missing_owned_cleanup_is_not_success(self):
        result = runner.lifecycle(
            [],
            lambda s: None,
            lambda: {"complete": True},
            lambda: {"complete": True, "remaining_owned_resources": ["owned-process"]},
            lambda r: None,
        )
        self.assertEqual(result["result"], "RED")

    def test_complete_lifecycle_positive(self):
        result = runner.lifecycle(
            ["admit"],
            lambda s: "observed",
            lambda: {"complete": True},
            lambda: {"complete": True, "remaining_owned_resources": []},
            lambda r: None,
        )
        self.assertEqual(result["result"], "PASS")

    def test_prefix_or_reordered_transport_never_accepted(self):
        raw = b"abcdef"
        digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual(custody.complete_transport([b"abc", b"def"], 6, digest), raw)
        for chunks in [[b"abc"], [b"def", b"abc"]]:
            with self.assertRaises(ValueError):
                custody.complete_transport(chunks, 6, digest)

    def test_validated_bytes_copied_without_source_reread(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            source = p / "source"
            source.write_bytes(b"checked")
            digest = hashlib.sha256(b"checked").hexdigest()
            raw, identity = custody.capture(source, digest)
            source.write_bytes(b"changed")
            custody.stage_captured(raw, digest, p / "staged")
            self.assertEqual((p / "staged").read_bytes(), b"checked")

    def test_complete_independent_pair_positive_and_mutants(self):
        def lane(name):
            return {
                "identity": {
                    k: name + k
                    for k in ["build_id", "source", "store", "runroot", "output"]
                },
                "exit": 0,
                "archive": b"archive",
                "oci": {"blob": "digest"},
                "audit": {"complete": True},
                "cleanup": True,
            }

        a = lane("a")
        b = lane("b")
        self.assertTrue(runner.verify_pair(a, b)["raw_archives_equal"])
        b["archive"] = b"different"
        with self.assertRaises(ValueError):
            runner.verify_pair(a, b)
        b = lane("b")
        b["cleanup"] = False
        with self.assertRaises(ValueError):
            runner.verify_pair(a, b)


class ArtifactRace(unittest.TestCase):
    def test_archive_replacement_during_hash_refuses(self):
        from unittest.mock import patch
        from cortex_core.release.build import compare

        original = hashlib.file_digest
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "archive"
            p.write_bytes(b"original")

            def replace(stream, algorithm):
                digest = original(stream, algorithm)
                replacement = p.with_name("replacement")
                replacement.write_bytes(b"different")
                replacement.replace(p)
                return digest

            with patch.object(compare.hashlib, "file_digest", side_effect=replace):
                with self.assertRaises(ValueError):
                    compare.compare_archives(p, b"original")


class OperationFailure(unittest.TestCase):
    def test_nonzero_operation_cannot_advance_build(self):
        events = []
        result = runner.lifecycle(
            ["prepare", "build"],
            lambda stage: events.append(stage) or {"exit": 7},
            lambda: {"complete": True},
            lambda: {"complete": True, "remaining_owned_resources": []},
            lambda row: None,
        )
        self.assertEqual(events, ["prepare"])
        self.assertEqual(result["result"], "RED")
        self.assertEqual(result["stages"]["prepare"]["exit"], 7)

    def test_empty_stage_list_never_grants_pass(self):
        result = runner.lifecycle(
            [],
            lambda s: None,
            lambda: {"complete": True},
            lambda: {"complete": True, "remaining_owned_resources": []},
            lambda row: None,
        )
        self.assertEqual(result["result"], "RED")


class SnapshotBinding(unittest.TestCase):
    def test_postvalidation_source_change_refuses_before_staging(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            source = p / "source"
            source.write_bytes(b"checked")
            digest = hashlib.sha256(b"checked").hexdigest()
            raw, identity = custody.capture(source, digest)
            source.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                custody.stage_snapshot(source, raw, identity, digest, p / "staged")
            self.assertFalse((p / "staged").exists())

    def test_unchanged_snapshot_positive(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            source = p / "source"
            source.write_bytes(b"checked")
            digest = hashlib.sha256(b"checked").hexdigest()
            raw, identity = custody.capture(source, digest)
            custody.stage_snapshot(source, raw, identity, digest, p / "staged")
            self.assertEqual((p / "staged").read_bytes(), raw)


class TransportBounds(unittest.TestCase):
    def test_boolean_size_and_nonbytes_chunk_refuse(self):
        for chunks, size in [([b"a"], True), (["a"], 1)]:
            with self.assertRaises(ValueError):
                custody.complete_transport(
                    chunks, size, hashlib.sha256(b"a").hexdigest()
                )

    def test_overlong_stream_stops_before_consuming_remainder(self):
        seen = []

        def chunks():
            yield b"too large"
            seen.append("remainder")
            yield b"more"

        with self.assertRaises(ValueError):
            custody.complete_transport(chunks(), 1, hashlib.sha256(b"a").hexdigest())
        self.assertEqual(seen, [])
