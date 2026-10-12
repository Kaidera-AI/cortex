"""Actual strict-profile preparation transport; no build is executed."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from cortex_core.release.codec import canonical_bytes, sha256

sys.path.insert(0, str(Path(__file__).parents[1] / "release"))
import native_matrix_fixture as matrix


class ProfileCLI(unittest.TestCase):
    def run_case(self, absent=False):
        value, data, *_, acceptance = matrix.family()
        if absent:
            record = value["descriptor"]
            descriptor = json.loads(data[record["path"]])
            descriptor.pop("release_policy")
            data[record["path"]] = canonical_bytes(descriptor)
            record.update(
                bytes=len(data[record["path"]]), sha256=sha256(data[record["path"]])
            )
            acceptance = dict(acceptance, input_sha256=sha256(canonical_bytes(value)))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, body in data.items():
                path = root / "inputs" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                path.chmod(0o644)
            raw = canonical_bytes(value)
            policy = canonical_bytes(acceptance)
            (root / "input.json").write_bytes(raw)
            (root / "policy.json").write_bytes(policy)
            argv = [
                sys.executable,
                "-m",
                "cortex_core.release.build",
                "prepare",
                "--profile-v1",
                "--input",
                str(root / "input.json"),
                "--input-sha256",
                sha256(raw),
                "--input-root",
                str(root / "inputs"),
                "--policy",
                str(root / "policy.json"),
                "--policy-record-sha256",
                sha256(policy),
                "--output",
                str(root / "clone"),
            ]
            result = subprocess.run(
                argv, env=os.environ.copy(), capture_output=True, timeout=15
            )
            self.assertEqual(result.returncode, 1 if absent else 0, result.stderr)
            decision = json.loads(result.stdout)
            self.assertEqual(decision["result"], "RED" if absent else "PREPARED")
            self.assertFalse(decision["native_build_qualified"])
            self.assertEqual((root / "clone").exists(), not absent)
            if not absent:
                self.assertTrue(decision["profile_v1_required"])
                self.assertEqual(
                    {name: (root / "clone" / name).read_bytes() for name in data}, data
                )

    def test_independent_A_B_preparation_consumes_same_owner_profile(self):
        from cortex_core.release.build import inputs
        from cortex_core.release.contracts import thaw

        value, data, *_, acceptance = matrix.family()
        acceptance = dict(acceptance, require_profile_v1=True)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, body in data.items():
                path = root / "original" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                path.chmod(0o644)
            admitted = []
            for slot in ("A", "B"):
                reader = inputs.FilesystemReader(
                    root / "original", max_bytes=value["resources"]["memory_bytes"]
                )
                verified, clone = inputs.prepare(
                    canonical_bytes(value), reader, acceptance, root / slot
                )
                admitted.append(verified)
                self.assertEqual(
                    {name: (clone / name).read_bytes() for name in data}, data
                )
                self.assertFalse(verified.native_qualified)
            self.assertEqual(admitted[0].input_sha256, admitted[1].input_sha256)
            self.assertEqual(
                thaw(admitted[0].metadata)["release_policy"],
                thaw(admitted[1].metadata)["release_policy"],
            )
            for name in data:
                self.assertNotEqual(
                    (root / "A" / name).stat().st_ino, (root / "B" / name).stat().st_ino
                )

    def test_profile_v1_actual_cli_prepares_only_admitted_bytes(self):
        self.run_case()

    def test_absent_profile_actual_cli_refuses_without_clone(self):
        self.run_case(absent=True)
