"""Exact-head admission control: pinned key bytes must be the verified bytes."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types


SOURCE = (Path(__file__).resolve().parents[2] / 'src/cortex_core/upgrade.py').read_text()
module = types.ModuleType("vera_pr83_exact_upgrade_key")
sys.modules[module.__name__] = module
exec(compile(SOURCE, "exact-head-upgrade.py", "exec"), module.__dict__)
real_run = subprocess.run


def run(*args):
    result = real_run(list(args), capture_output=True)
    assert result.returncode == 0, result.stderr


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    trusted = root / "trusted.pub"
    attacker = root / "other.pub"
    attacker_secret = root / "other.sec"
    run("minisign", "-G", "-W", "-p", str(trusted), "-s", str(root / "trusted.sec"))
    run("minisign", "-G", "-W", "-p", str(attacker), "-s", str(attacker_secret))

    def signed(name, release, schema, module_digest):
        manifest = root / (name + ".json")
        signature = root / (name + ".minisig")
        manifest.write_bytes(json.dumps({
            "format": "cortex-aggregate-v1", "release": release,
            "edition": "standalone", "platform": "darwin-arm64",
            "schema_version": schema, "event_version": 1,
            "modules": {"core": "sha256:" + module_digest * 64},
        }, sort_keys=True, separators=(",", ":")).encode())
        run("minisign", "-S", "-W", "-s", str(attacker_secret),
            "-m", str(manifest), "-x", str(signature))
        return manifest, signature

    old, old_sig = signed("old", "v0.1.003", 1, "1")
    new, new_sig = signed("new", "v0.1.020", 2, "2")
    policy = {
        "trusted_key_sha256": hashlib.sha256(trusted.read_bytes()).hexdigest(),
        "old_release": "v0.1.003", "new_release": "v0.1.020",
        "old_manifest_sha256": hashlib.sha256(old.read_bytes()).hexdigest(),
        "new_manifest_sha256": hashlib.sha256(new.read_bytes()).hexdigest(),
        "edition": "standalone", "platform": "darwin-arm64",
        "old_schema": 1, "new_schema": 2, "old_event": 1, "new_event": 1,
        "old_modules": {"core": "sha256:" + "1" * 64},
        "new_modules": {"core": "sha256:" + "2" * 64},
        "expand_steps": ["expand-1"],
        "readers": [["v0.1.003", 1], ["v0.1.020", 2]],
        "writers": [["v0.1.003", 1], ["v0.1.020", 2]],
    }
    swapped = False

    def swap_before_verify(command, **kwargs):
        global swapped
        if command[:2] == ["minisign", "-V"] and not swapped:
            trusted.write_bytes(attacker.read_bytes())
            swapped = True
        return real_run(command, **kwargs)

    module.subprocess.run = swap_before_verify
    try:
        module.admit_signed_pair(policy, old, old_sig, new, new_sig, trusted)
    except module.UpgradeRefusal:
        print("PASS: unpinned replacement key refused")
    else:
        assert swapped
        raise AssertionError("admitted manifests signed by replacement key, not pinned key")
