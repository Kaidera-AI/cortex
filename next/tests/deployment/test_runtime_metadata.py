"""Independent consistency facts produce an unsigned draft, never admission."""
import copy
import hashlib
import inspect
import json
from pathlib import Path
import socket
import subprocess
import unittest
from unittest.mock import patch

from test_runtime_policy import LINUX, MAC, policy, production_module, wire


SCHEMA = Path(__file__).resolve().parents[2] / "contracts/deployment/runtime-policy.schema.json"
IDENTITY = {"release_id": "v0.2.1", "release_lineage": "cortex-test", "release_sequence": 1,
            "api_contract": "cortex-test.v1", "source_revision": "a" * 40}
IMAGES = {"api": "ghcr.io/kaidera-ai/cortex-api@sha256:" + "b" * 64}


def metadata_inputs(reader):
    source = b"independently measured synthetic source snapshot"
    parsed = reader.parse_runtime_policy(wire(policy()))
    return dict(release_metadata=wire({**IDENTITY, "images": IMAGES}),
                expected_identity=dict(IDENTITY), expected_images=dict(IMAGES),
                source_bytes=source, source_sha256=hashlib.sha256(source).hexdigest(),
                schema_bytes=SCHEMA.read_bytes(), schema_sha256=hashlib.sha256(SCHEMA.read_bytes()).hexdigest(),
                runtime_policy=parsed, expected_policy_sha256=parsed.sha256,
                profile_id=LINUX, host_os="linux", platform="linux/amd64",
                image_platforms=("linux/amd64",), host_platforms=("linux/amd64",))


class RuntimeMetadata(unittest.TestCase):
    def test_seam_presence_independent_identity_and_positive_draft(self):
        reader, module = production_module(), production_module("release_metadata")
        self.assertIsNotNone(module, "deployment runtime metadata seam missing")
        self.assertIsNotNone(reader, "deployment runtime-policy reader missing")
        kwargs = metadata_inputs(reader)
        bad = copy.deepcopy(kwargs["expected_identity"])
        bad["release_sequence"] = 2
        with self.assertRaises(reader.RuntimePolicyRefusal) as refused:
            module.assemble_runtime_metadata(**{**kwargs, "expected_identity": bad})
        self.assertEqual(refused.exception.code, "runtime_metadata_identity")
        draft = module.assemble_runtime_metadata(**kwargs)
        self.assertEqual(type(draft).__name__, "UnsignedRuntimeMetadataDraft")
        value = json.loads(draft.raw)
        self.assertEqual({k: value[k] for k in IDENTITY}, IDENTITY)
        self.assertEqual(value["images"], IMAGES)
        self.assertEqual(value["runtime_policy"], policy())
        self.assertEqual(draft.sha256, hashlib.sha256(draft.raw).hexdigest())
        for forbidden in ("signature_ok", "native_qualified", "admitted", "qualified"):
            self.assertFalse(hasattr(draft, forbidden))
            self.assertNotIn(forbidden, value)

    def test_measured_source_schema_and_policy_binding_refusals(self):
        reader, module = production_module(), production_module("release_metadata")
        self.assertIsNotNone(module, "deployment runtime metadata seam missing")
        self.assertIsNotNone(reader, "deployment runtime-policy reader missing")
        kwargs = metadata_inputs(reader)
        for changes in ({"source_bytes": b"different actual source"}, {"source_sha256": "0" * 64},
                        {"schema_bytes": b"{}"}, {"schema_sha256": "0" * 64},
                        {"schema_bytes": b"{}", "schema_sha256": hashlib.sha256(b"{}").hexdigest()},
                        {"expected_policy_sha256": "0" * 64}):
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(reader.RuntimePolicyRefusal):
                    module.assemble_runtime_metadata(**{**kwargs, **changes})
        draft = module.assemble_runtime_metadata(**kwargs)
        self.assertEqual(draft.source_sha256, kwargs["source_sha256"])
        self.assertEqual(draft.schema_sha256, kwargs["schema_sha256"])
        self.assertEqual(draft.policy_sha256, kwargs["expected_policy_sha256"])

    def test_each_identity_field_images_and_ambiguous_carrier_refuse(self):
        reader, module = production_module(), production_module("release_metadata")
        self.assertIsNotNone(module, "deployment runtime metadata seam missing")
        self.assertIsNotNone(reader, "deployment runtime-policy reader missing")
        kwargs = metadata_inputs(reader)
        for key in IDENTITY:
            altered = dict(IDENTITY)
            altered[key] = 2 if key == "release_sequence" else ("c" * 40 if key == "source_revision" else "different")
            with self.subTest(key=key):
                with self.assertRaises(reader.RuntimePolicyRefusal):
                    module.assemble_runtime_metadata(**{**kwargs, "release_metadata": wire({**altered, "images": IMAGES})})
        for extra in ({"podman": {}}, {"runtime_policy": policy(MAC)}, {"images": {"api": "different"}}):
            with self.subTest(extra=tuple(extra)):
                with self.assertRaises(reader.RuntimePolicyRefusal):
                    module.assemble_runtime_metadata(**{**kwargs, "release_metadata": wire({**IDENTITY, "images": IMAGES, **extra})})
        for changes in ({"profile_id": MAC}, {"host_os": "macos"}, {"platform": "linux/arm64"}):
            with self.subTest(changes=changes):
                with self.assertRaises(reader.RuntimePolicyRefusal):
                    module.assemble_runtime_metadata(**{**kwargs, **changes})

    def test_raw_metadata_refusals_and_no_effect_ports(self):
        reader, module = production_module(), production_module("release_metadata")
        self.assertIsNotNone(module, "deployment runtime metadata seam missing")
        self.assertIsNotNone(reader, "deployment runtime-policy reader missing")
        kwargs = metadata_inputs(reader)
        raw = kwargs["release_metadata"]
        with patch.object(socket, "socket", side_effect=AssertionError("network effect")), \
             patch.object(subprocess, "run", side_effect=AssertionError("process effect")):
            draft = module.assemble_runtime_metadata(**kwargs)
            self.assertIsInstance(draft.raw, bytes)
            for bad in (raw.replace(b'"release_sequence":1', b'"release_sequence":1,"release_sequence":1'), b"\xff", b"[]", b" " * (1048576 + 1)):
                with self.subTest(size=len(bad)):
                    with self.assertRaises(reader.RuntimePolicyRefusal):
                        module.assemble_runtime_metadata(**{**kwargs, "release_metadata": bad})
        for parameter in inspect.signature(module.assemble_runtime_metadata).parameters:
            self.assertNotIn(parameter, ("signer", "verifier", "host", "token", "callback"))


if __name__ == "__main__":
    unittest.main()
