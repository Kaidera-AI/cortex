"""U2 exercises actual deployment predicates; no host or release admission."""
import copy
import hashlib
import importlib
import json
from pathlib import Path
import unittest


VECTORS = json.loads(Path(__file__).with_name("runtime-policy-vectors.json").read_bytes())
LINUX = "podman-linux-rootless.v1"
MAC = "podman-macos-machine-rootless.v1"


def production_module(name="runtime_policy"):
    try:
        return importlib.import_module("cortex_core.deployment." + name)
    except ModuleNotFoundError as error:
        if error.name in ("cortex_core.deployment", "cortex_core.deployment." + name):
            return None
        raise


def policy(name=LINUX):
    case = next(c for c in VECTORS["cases"] if c["id"] == ("RF-P-MAC" if name == MAC else "RF-P-LINUX"))
    return json.loads(case["raw_utf8"])


def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf8")


def effective(module, left, right, **kwargs):
    args = dict(profile_id=LINUX, host_os="linux", platform="linux/amd64",
                image_platforms=("linux/amd64",), host_platforms=("linux/amd64",))
    args.update(kwargs)
    return module.effective_runtime_policy(module.parse_runtime_policy(wire(left)),
                                           module.parse_runtime_policy(wire(right)), **args)


class RuntimePolicyReader(unittest.TestCase):
    def test_reader_presence_and_float_refusal(self):
        module = production_module()
        self.assertIsNotNone(module, "deployment runtime-policy reader missing")
        self.assertTrue(callable(getattr(module, "parse_runtime_policy", None)))
        positive = module.parse_runtime_policy(wire(policy()))
        self.assertEqual(positive.document["schema"], "cortex.runtime-policy.v1")
        raw = next(c["raw_utf8"] for c in VECTORS["cases"] if c["id"] == "RF-R-FLOAT-1.0").encode("utf8")
        with self.assertRaises(module.RuntimePolicyRefusal) as refused:
            module.parse_runtime_policy(raw)
        self.assertEqual(refused.exception.code, "runtime_policy_noninteger_number")

    def test_actual_reader_all_28_frozen_vectors(self):
        module = production_module()
        self.assertIsNotNone(module, "deployment runtime-policy reader missing")
        observed = []
        for case in VECTORS["cases"]:
            with self.subTest(case=case["id"]):
                raw = case["raw_utf8"].encode("utf8")
                self.assertEqual(hashlib.sha256(raw).hexdigest(), case["raw_sha256"])
                if case["required_receiver_verdict"] == "REFUSE":
                    with self.assertRaises(module.RuntimePolicyRefusal):
                        module.parse_runtime_policy(raw)
                    verdict = "REFUSE"
                else:
                    parsed = module.parse_runtime_policy(raw)
                    self.assertEqual(parsed.sha256, case["raw_sha256"])
                    verdict = "PARSED_ONLY"
                observed.append((case["id"], verdict))
        self.assertEqual(len(observed), 28)
        self.assertEqual(sum(v == "PARSED_ONLY" for _, v in observed), 3)

    def test_baseline_refusal_and_stricter_positive(self):
        module = production_module()
        self.assertIsNotNone(module, "deployment runtime-policy reader missing")
        value = policy()
        value["profiles"][LINUX]["minimum_version"] = "5.0.0"
        with self.assertRaises(module.RuntimePolicyRefusal) as refused:
            module.parse_runtime_policy(wire(value))
        self.assertEqual(refused.exception.code, "runtime_policy_baseline")
        value["profiles"][LINUX]["minimum_version"] = "7.0.0"
        self.assertEqual(module.parse_runtime_policy(wire(value)).document["profiles"][LINUX]["minimum_version"], "7.0.0")

    def test_denylist_semantics_and_utf8_byte_bound(self):
        module = production_module()
        self.assertIsNotNone(module, "deployment runtime-policy reader missing")
        cases = [c for c in VECTORS["cases"] if c["required_phase"] == "semantic" and c["id"] != "RF-S-BASELINE"]
        for case in cases:
            with self.subTest(case=case["id"]):
                with self.assertRaises(module.RuntimePolicyRefusal):
                    module.parse_runtime_policy(case["raw_utf8"].encode("utf8"))
        valid = policy()
        valid["profiles"][LINUX]["denylist"]["entries"] = [{"version": "6.0.8", "date": "2026-10-06", "reason": "é" * 512}]
        self.assertEqual(module.parse_runtime_policy(wire(valid)).document["profiles"][LINUX]["denylist"]["entries"][0]["reason"], "é" * 512)

    def test_raw_duplicates_encoding_size_nonfinite_and_depth_refuse(self):
        module = production_module()
        self.assertIsNotNone(module, "deployment runtime-policy reader missing")
        raw = wire(policy())
        invalid = [raw.replace(b'"format_version":1', b'"format_version":1,"format_version":1'),
                   raw.replace(b'"engine":"podman"', b'"engine":"podman","engine":"podman"'),
                   raw.replace(b'"format_version":1', b'"format_version":NaN'),
                   b"\xff" + raw, b"\xef\xbb\xbf" + raw, raw + b" " * 65536,
                   b"[" * 3000 + b"0" + b"]" * 3000, raw.decode("utf8")]
        for item in invalid:
            with self.subTest(kind=type(item).__name__, size=len(item)):
                with self.assertRaises(module.RuntimePolicyRefusal):
                    module.parse_runtime_policy(item)
        surrogate = policy()
        surrogate["profiles"][LINUX]["denylist"]["entries"] = [{"version": "6.0.8", "date": "2026-10-06", "reason": "\ud800"}]
        with self.assertRaises(module.RuntimePolicyRefusal):
            module.parse_runtime_policy(json.dumps(surrogate).encode("ascii"))

    def test_canonical_digest_unicode_order_and_immutability(self):
        module = production_module()
        self.assertIsNotNone(module, "deployment runtime-policy reader missing")
        value = policy()
        value["profiles"][LINUX]["denylist"]["entries"] = [{"version": "6.0.8", "date": "2026-10-06", "reason": "cafe\u0301 / café"}]
        expected = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf8")
        parsed = module.parse_runtime_policy(json.dumps(value, indent=2).encode("utf8"))
        self.assertEqual(module.canonical_runtime_policy_bytes(parsed), expected)
        self.assertEqual(module.runtime_policy_sha256(parsed), hashlib.sha256(expected).hexdigest())
        self.assertFalse(parsed.canonical.endswith(b"\n"))
        self.assertIn("cafe\u0301 / café".encode("utf8"), parsed.canonical)
        reordered = dict(reversed(list(value.items())))
        self.assertEqual(module.parse_runtime_policy(wire(reordered)).sha256, parsed.sha256)
        with self.assertRaises(TypeError):
            parsed.document["profiles"][LINUX]["engine"] = "docker"

    def test_independent_product_floor_and_denial_union(self):
        module = production_module()
        self.assertIsNotNone(module, "deployment runtime-policy reader missing")
        left, right = policy(), policy()
        left["profiles"][LINUX]["denylist"]["entries"] = [{"version": "7.0.1", "date": "2026-10-06", "reason": "Cortex denial"}]
        right["profiles"][LINUX]["minimum_version"] = "7.0.0"
        right["profiles"][LINUX]["denylist"]["entries"] = [{"version": "7.0.2", "date": "2026-10-06", "reason": "KOS denial"}, {"version": "7.0.1", "date": "2026-10-06", "reason": "KOS independent reason"}]
        result = effective(module, left, right)
        self.assertEqual(result.minimum_version, "7.0.0")
        self.assertEqual(result.denied_versions, ("7.0.1", "7.0.2"))
        self.assertEqual(len(result.denial_sources), 3)
        self.assertEqual({r["product"] for r in result.denial_sources}, {"cortex", "kos"})
        self.assertEqual(len(result.required_capabilities), 8)
        self.assertEqual(effective(module, right, left).minimum_version, result.minimum_version)
        self.assertEqual(effective(module, policy(), policy()).minimum_version, "6.0.2")

    def test_profile_host_and_platform_intersections_refuse(self):
        module = production_module()
        self.assertIsNotNone(module, "deployment runtime-policy reader missing")
        for arguments in ({"profile_id": "docker-linux-rootless.v1"}, {"host_os": "macos"},
                          {"platform": "linux/arm64"}, {"image_platforms": ("linux/arm64",)},
                          {"host_platforms": ("linux/arm64",)}, {"image_platforms": ()}):
            with self.subTest(arguments=arguments):
                with self.assertRaises(module.RuntimePolicyRefusal):
                    effective(module, policy(), policy(), **arguments)
        with self.assertRaises(module.RuntimePolicyRefusal):
            effective(module, policy(), policy(MAC))
        mac = effective(module, policy(MAC), policy(MAC), profile_id=MAC, host_os="macos",
                        platform="linux/arm64", image_platforms=("linux/arm64",), host_platforms=("linux/arm64",))
        self.assertEqual(mac.platforms, ("linux/arm64",))


if __name__ == "__main__":
    unittest.main()
