"""CM-1 writer contract; signed synthetic fixtures, fake health, OS scratch."""
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest


ROOT = Path(__file__).resolve().parents[3]
WRITER = ROOT / "packages/deploy/prerequisite.py"
TOKEN = "ctx1_" + "1" * 32 + "." + "a" * 43
INSTANCE = str(UUID(int=407))
IDENTITY = {"release_id": "v0.1.003-manual.1", "release_lineage": "cortex-v1-manual",
            "release_sequence": 1, "api_contract": "cortex-kos-v02009.v1",
            "source_revision": "123aa522d45fae46ee7749cd971e3733f45fe1ec"}
IMAGES = {role: "ghcr.io/kaidera-ai/cortex-" + role + "@sha256:" + digit * 64
          for role, digit in (("db", "1"), ("migrate", "2"), ("api", "3"))}
# A deliberately public, deterministic fixture seed. Never a production key.
KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
KEY_ID = b"R407KEY1"
PUBLIC = base64.b64encode(b"Ed" + KEY_ID + KEY.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def sign(body, key=KEY):
    first = key.sign(hashlib.blake2b(body, digest_size=64).digest())
    comment = "PUBLIC synthetic CM-1 fixture"
    return "\n".join(("untrusted comment: PUBLIC test only",
        base64.b64encode(b"ED" + KEY_ID + first).decode(), "trusted comment: " + comment,
        base64.b64encode(key.sign(first + comment.encode())).decode())) + "\n"


def required(monkeypatch):
    if not WRITER.exists():
        pytest.fail("R407 CM-1 descriptor writer missing")
    spec = importlib.util.spec_from_file_location("r407_prerequisite", WRITER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PINNED_PUBLIC_KEY", PUBLIC)
    return module


def seed(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    release_dir = home / "release"
    release_dir.mkdir(mode=0o700)
    credential_dir = home / "credentials"
    credential_dir.mkdir(mode=0o700)
    project_dir = credential_dir / "notes"
    project_dir.mkdir(mode=0o700)
    credential = project_dir / "console.token"
    credential.write_text(TOKEN + "\n")
    credential.chmod(0o600)
    manifest = {"schema": "cortex.release.v1", **IDENTITY,
        "images": {"linux/amd64": dict(IMAGES)},
        "podman": {"supported_family": "6.0.x", "tested_baseline": "6.0.2"}}
    manifest_path = release_dir / "release.json"
    signature_path = release_dir / "release.json.minisig"
    health = {**IDENTITY, "installation_id": INSTANCE, "status": "healthy",
              "postgres": "connected", "rls_enforced": True}
    calls = []

    def write_manifest(body=None, key=KEY):
        body = (json.dumps(manifest, sort_keys=True) + "\n").encode() if body is None else body
        manifest_path.write_bytes(body)
        signature_path.write_text(sign(body, key))

    write_manifest()

    def http_get(url, headers, *, timeout, max_bytes):
        assert url == "http://127.0.0.1:8501/health"
        assert headers == {"Authorization": "Bearer " + TOKEN, "X-Project": "notes", "X-Agent-Name": "console"}
        assert timeout == 5 and max_bytes == 64 * 1024
        calls.append(url)
        return 200, json.dumps(health).encode()

    args = dict(home=home, manifest_path=manifest_path, signature_path=signature_path,
        api_url="http://127.0.0.1:8501", installation_id=INSTANCE,
        credential_dir=credential_dir, project="notes", http_get=http_get)
    expected = {"schema": "cortex.prerequisite.v1", "api_url": args["api_url"],
        "installation_id": INSTANCE, "owner_uid": os.getuid(), **IDENTITY,
        "platform": "linux/amd64", "host_os": "linux", "release_manifest": str(manifest_path),
        "release_signature": str(signature_path), "images": IMAGES, "health_path": "/health",
        "credential_dir": str(credential_dir), "credential_file": str(credential),
        "credential_project": "notes", "credential_agent": "console",
        "podman": {"supported_family": "6.0.x", "tested_baseline": "6.0.2",
                   "machine_name": None, "machine_image_digest": None},
        "install_guide": "https://github.com/Kaidera-AI/cortex/releases/tag/v0.1.003-manual.1"}
    return args, manifest, health, expected, calls, write_manifest


def test_exact_signed_authenticated_descriptor_publication(tmp_path, monkeypatch):
    module = required(monkeypatch)
    args, manifest, health, expected, calls, _ = seed(tmp_path)
    before = copy.deepcopy((manifest, health))
    receipt = module.write_prerequisite(**args)
    target = args["home"] / ".cortex/prerequisite.json"
    assert json.loads(target.read_text()) == expected
    assert stat.S_IMODE(target.stat().st_mode) == 0o600 and target.stat().st_nlink == 1
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert receipt["path"] == str(target) and receipt["schema"] == expected["schema"]
    assert TOKEN not in target.read_text() and TOKEN not in repr(receipt)
    assert calls == [args["api_url"] + "/health"] and (manifest, health) == before


@pytest.mark.parametrize("damage", (
    "bad-signature", "wrong-key", "tampered-comment", "duplicate-json", "wrong-lineage",
    "bool-sequence", "wrong-contract", "v2-release", "missing-api-image", "mutable-image",
    "missing-platform", "wrong-podman", "credential-missing", "credential-mode", "credential-link",
    "credential-hardlink", "credential-dir-mode", "manifest-link", "signature-link",
    "wrong-instance", "unhealthy", "rls-false", "wrong-health-identity", "oversize-health", "redirect",
))
def test_refusal_keeps_existing_descriptor_and_never_publishes_partial_success(tmp_path, monkeypatch, damage):
    module = required(monkeypatch)
    args, manifest, health, expected, calls, write_manifest = seed(tmp_path)
    output = args["home"] / ".cortex"
    output.mkdir(mode=0o700)
    target = output / "prerequisite.json"
    target.write_text(json.dumps(expected, indent=2) + "\n")
    target.chmod(0o600)
    original = target.read_bytes()
    before_stat = target.stat()
    token = args["credential_dir"] / "notes/console.token"
    if damage == "bad-signature":
        args["signature_path"].write_text("PUBLIC bad signature\n")
    elif damage == "wrong-key":
        write_manifest(key=Ed25519PrivateKey.from_private_bytes(b"x" * 32))
    elif damage == "tampered-comment":
        args["signature_path"].write_text(args["signature_path"].read_text().replace("CM-1", "CM-2"))
    elif damage == "duplicate-json":
        body = json.dumps(manifest)
        write_manifest((body[:-1] + ', "release_sequence": 1}').encode())
    elif damage in {"wrong-lineage", "bool-sequence", "wrong-contract", "v2-release"}:
        key, value = {"wrong-lineage": ("release_lineage", "cortex-v2"), "bool-sequence": ("release_sequence", True),
                      "wrong-contract": ("api_contract", "wrong"), "v2-release": ("release_id", "v0.2.001")}[damage]
        manifest[key] = value
        write_manifest()
    elif damage == "missing-api-image":
        del manifest["images"]["linux/amd64"]["api"]
        write_manifest()
    elif damage == "mutable-image":
        manifest["images"]["linux/amd64"]["api"] = "ghcr.io/kaidera-ai/cortex-api:latest"
        write_manifest()
    elif damage == "missing-platform":
        manifest["images"] = {"linux/arm64": IMAGES}
        write_manifest()
    elif damage == "wrong-podman":
        manifest["podman"]["supported_family"] = "6.1.x"
        write_manifest()
    elif damage == "credential-missing": token.unlink()
    elif damage == "credential-mode": token.chmod(0o644)
    elif damage == "credential-link":
        real = token.with_name("PUBLIC-real-token")
        token.rename(real)
        token.symlink_to(real)
    elif damage == "credential-hardlink": os.link(token, token.with_name("PUBLIC-link"))
    elif damage == "credential-dir-mode": args["credential_dir"].chmod(0o755)
    elif damage in {"manifest-link", "signature-link"}:
        key = "manifest_path" if damage == "manifest-link" else "signature_path"
        real = args[key].with_name("PUBLIC-real-" + args[key].name)
        args[key].rename(real)
        args[key].symlink_to(real)
    elif damage == "wrong-instance": health["installation_id"] = str(UUID(int=999))
    elif damage == "unhealthy": health["postgres"] = "disconnected"
    elif damage == "rls-false": health["rls_enforced"] = False
    elif damage == "wrong-health-identity": health["source_revision"] = "a" * 40
    elif damage in {"oversize-health", "redirect"}:
        def invalid_transport(url, headers, *, timeout, max_bytes):
            calls.append(url)
            return (200, b"x" * (max_bytes + 1)) if damage == "oversize-health" else (302, b"PUBLIC redirect")
        args["http_get"] = invalid_transport
    with pytest.raises(module.PrerequisiteRefusal) as failure:
        module.write_prerequisite(**args)
    expected_code = {
        "bad-signature": "cortex_release_signature_invalid", "wrong-key": "cortex_release_signature_invalid",
        "tampered-comment": "cortex_release_signature_invalid", "duplicate-json": "cortex_release_identity_missing",
        "wrong-lineage": "cortex_release_unsupported", "bool-sequence": "cortex_release_identity_missing",
        "wrong-contract": "cortex_release_unsupported", "v2-release": "cortex_release_unsupported",
        "missing-api-image": "cortex_image_mismatch", "mutable-image": "cortex_image_mismatch",
        "missing-platform": "cortex_image_mismatch", "wrong-podman": "cortex_podman_unsupported",
        "manifest-link": "cortex_release_signature_invalid", "signature-link": "cortex_release_signature_invalid",
        "wrong-instance": "cortex_instance_mismatch", "unhealthy": "cortex_health_degraded",
        "rls-false": "cortex_health_degraded", "wrong-health-identity": "cortex_release_unsupported",
        "oversize-health": "cortex_health_unavailable", "redirect": "cortex_health_unavailable",
    }.get(damage, "cortex_credential_unavailable")
    assert failure.value.code == expected_code
    assert target.read_bytes() == original and target.stat().st_nlink == 1
    after_stat = target.stat()
    assert (after_stat.st_dev, after_stat.st_ino, after_stat.st_mtime_ns, after_stat.st_ctime_ns) == (
        before_stat.st_dev, before_stat.st_ino, before_stat.st_mtime_ns, before_stat.st_ctime_ns)
    assert not any(p.name.startswith(".prerequisite-") for p in output.iterdir())
    assert len(calls) <= 1
