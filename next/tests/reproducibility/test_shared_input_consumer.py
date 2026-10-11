import importlib.util
import tempfile
import unittest
from pathlib import Path
from cortex_core.release.build import inputs
from cortex_core.release.codec import canonical_bytes, sha256
from cortex_core.release.contracts import VerifiedBuildInput

# Consume the actual R01-owned fixture at its adopted source location.
spec = importlib.util.spec_from_file_location('r01_shared_fixture', Path(__file__).parents[1]/'release/fixture.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class SharedInput(unittest.TestCase):
    def test_real_owner_input_roundtrip_is_immutable_and_fixture_only(self):
        value, data = fixture.build_input()
        raw = canonical_bytes(value)
        policy = {'input_sha256': sha256(raw), 'policy_sha256': 'e'*64, 'qualification_scope': 'fixture'}
        verified = inputs.admit(raw, data.__getitem__, policy)
        self.assertIsInstance(verified, VerifiedBuildInput)
        self.assertEqual(verified.input_sha256, sha256(raw))
        self.assertFalse(verified.native_qualified)
        with self.assertRaises(TypeError):
            verified.target['module_id'] = 'foreign'

    def test_physical_reader_stages_captured_bytes_from_actual_admission(self):
        value, data = fixture.build_input()
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            source = p/'original'
            source.mkdir()
            for path, body in data.items():
                file = source/path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(body)
                file.chmod(0o644)
            reader = inputs.FilesystemReader(source, max_bytes=1024*1024)
            raw = canonical_bytes(value)
            policy = {'input_sha256': sha256(raw), 'policy_sha256': 'e'*64, 'qualification_scope': 'fixture'}
            verified = inputs.admit(raw, reader, policy)
            inputs.materialize(verified, reader, p/'clone')
            self.assertEqual({path:(p/'clone'/path).read_bytes() for path in data}, data)
            (source/'source/module.py').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                inputs.materialize(verified, reader, p/'second')
            self.assertFalse((p/'second').exists())

    def test_dictionary_lookalike_cannot_stage_or_build(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            with self.assertRaises(ValueError):
                inputs.materialize({'input_sha256':'a'*64}, None, p/'clone')
            self.assertFalse((p/'clone').exists())
