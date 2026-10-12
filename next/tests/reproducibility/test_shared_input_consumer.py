import importlib.util
import tempfile
import unittest
from pathlib import Path
from cortex_core.release.build import inputs
from cortex_core.release.codec import canonical_bytes, sha256
from cortex_core.release.contracts import VerifiedBuildInput

# Consume the actual R01-owned fixture at its adopted source location.
spec = importlib.util.spec_from_file_location(
    "r01_shared_fixture", Path(__file__).parents[1] / "release/fixture.py"
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class SharedInput(unittest.TestCase):
    def test_real_owner_input_roundtrip_is_immutable_and_fixture_only(self):
        value, data = fixture.build_input()
        raw = canonical_bytes(value)
        policy = {
            "input_sha256": sha256(raw),
            "policy_sha256": "e" * 64,
            "qualification_scope": "fixture",
        }
        verified = inputs.admit(raw, data.__getitem__, policy)
        self.assertIsInstance(verified, VerifiedBuildInput)
        self.assertEqual(verified.input_sha256, sha256(raw))
        self.assertFalse(verified.native_qualified)
        with self.assertRaises(TypeError):
            verified.target["module_id"] = "foreign"

    def test_physical_reader_stages_captured_bytes_from_actual_admission(self):
        value, data = fixture.build_input()
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            source = p / "original"
            source.mkdir()
            for path, body in data.items():
                file = source / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(body)
                file.chmod(0o644)
            reader = inputs.FilesystemReader(source, max_bytes=1024 * 1024)
            raw = canonical_bytes(value)
            policy = {
                "input_sha256": sha256(raw),
                "policy_sha256": "e" * 64,
                "qualification_scope": "fixture",
            }
            verified = inputs.admit(raw, reader, policy)
            inputs.materialize(verified, reader, p / "clone")
            self.assertEqual(
                {path: (p / "clone" / path).read_bytes() for path in data}, data
            )
            (source / "source/module.py").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                inputs.materialize(verified, reader, p / "second")
            self.assertFalse((p / "second").exists())

    def test_dictionary_lookalike_cannot_stage_or_build(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            with self.assertRaises(ValueError):
                inputs.materialize({"input_sha256": "a" * 64}, None, p / "clone")
            self.assertFalse((p / "clone").exists())


class StagingMutations(unittest.TestCase):
    def fixture(self, root):
        value, data = fixture.build_input()
        for name, body in data.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            path.chmod(0o644)
        reader = inputs.FilesystemReader(root, max_bytes=1024 * 1024)
        raw = canonical_bytes(value)
        verified = inputs.admit(
            raw,
            reader,
            {
                "input_sha256": sha256(raw),
                "policy_sha256": "e" * 64,
                "qualification_scope": "fixture",
            },
        )
        return reader, verified

    def test_changed_captured_metadata_refuses_before_clone(self):
        from cortex_core.release.inputs import FileObservation

        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            reader, verified = self.fixture(p / "original")
            observed, identity = reader.captured["source/module.py"]
            reader.captured["source/module.py"] = (
                FileObservation(observed.data, "regular", 0o777),
                identity,
            )
            with self.assertRaises(ValueError):
                inputs.materialize(verified, reader, p / "clone")
            self.assertFalse((p / "clone").exists())

    def test_extra_source_after_admission_refuses_before_clone(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            reader, verified = self.fixture(p / "original")
            (p / "original/source/extra.py").write_bytes(b"extra")
            with self.assertRaises(ValueError):
                inputs.materialize(verified, reader, p / "clone")
            self.assertFalse((p / "clone").exists())


class AdmissionBeforeStage(unittest.TestCase):
    def test_rehashed_changed_input_cannot_create_clone_under_original_policy(self):
        value, data = fixture.build_input()
        policy = {
            "input_sha256": sha256(canonical_bytes(value)),
            "policy_sha256": "e" * 64,
            "qualification_scope": "fixture",
        }
        with tempfile.TemporaryDirectory() as t:
            for kind in ["inventory", "recipe", "platform", "mode"]:
                import copy

                changed = copy.deepcopy(value)
                if kind == "inventory":
                    changed["source"]["inventory"] = []
                elif kind == "recipe":
                    changed["recipe"]["recipe_id"] = "foreign"
                elif kind == "platform":
                    changed["target"]["oci_platform"] = "linux/arm64"
                else:
                    changed["source"]["inventory"][0]["mode"] = 0o777
                destination = Path(t) / kind
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    inputs.prepare(
                        canonical_bytes(changed), data.__getitem__, policy, destination
                    )
                self.assertFalse(destination.exists())


class CloneCustody(unittest.TestCase):
    def test_final_clone_tamper_is_refused_before_prepared_result(self):
        from unittest.mock import patch

        original_chmod = Path.chmod
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            helper = StagingMutations()
            reader, verified = helper.fixture(p / "original")
            destination = p / "clone"

            def tamper(path, mode, **kwargs):
                if path == destination / "source/module.py":
                    path.write_bytes(b"changed after write")
                return original_chmod(path, mode, **kwargs)

            with patch.object(Path, "chmod", tamper), self.assertRaises(ValueError):
                inputs.materialize(verified, reader, destination)
