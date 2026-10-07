"""Publish a CM-1 prerequisite after signed-input and authenticated agreement.

This fresh-install writer performs no provisioning, migration or engine action.
Native runtime/image qualification precedes it in the manual installation guide.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
from urllib import error, request
from urllib.parse import urlsplit
from uuid import UUID



PINNED_PUBLIC_KEY = "RWQuhegMfku7e4RltjV64sZmxXHEETzAntDePCQsJPYvXXujVMqKIvHL"
IDENTITY = ("release_id", "release_lineage", "release_sequence", "api_contract", "source_revision")
ROLES = {"db", "migrate", "api", "graph", "embed", "pdf", "audio", "vision"}
IMAGE = re.compile(r"ghcr\.io/kaidera-ai/cortex-[a-z0-9-]+@sha256:[0-9a-f]{64}")
PROJECT = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")
TOKEN = re.compile(r"ctx1_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}")


class PrerequisiteRefusal(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise PrerequisiteRefusal(code)


def _path(value, code):
    path = Path(value)
    if (not path.is_absolute() or ".." in path.parts
            or any(ord(char) < 32 or ord(char) == 127 for char in str(path))):
        _fail(code)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError:
            _fail(code)
        if stat.S_ISLNK(info.st_mode) or info.st_mode & 0o022:
            _fail(code)
    return path


def _read(value, uid, limit, code, private=False):
    path = _path(value, code)
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid or before.st_nlink != 1
                or before.st_size > limit or before.st_mode & 0o022
                or (private and stat.S_IMODE(before.st_mode) != 0o600)):
            _fail(code)
        chunks = []
        size = 0
        while size <= limit:
            block = os.read(fd, min(65536, limit + 1 - size))
            if not block:
                break
            chunks.append(block)
            size += len(block)
        body = b"".join(chunks)
        after = os.fstat(fd)
        if (len(body) > limit or len(body) != before.st_size or (before.st_dev, before.st_ino, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino,
                after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            _fail(code)
        return body
    except OSError:
        _fail(code)
    finally:
        if fd >= 0:
            os.close(fd)


def _json(body, code):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("nonfinite")

    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError):
        _fail(code)
    if not isinstance(value, dict):
        _fail(code)
    return value


def _signature(body, signature):
    # Runtime shares the stdlib custody validators; signing alone needs crypto.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    code = "cortex_release_signature_invalid"
    try:
        public = base64.b64decode(PINNED_PUBLIC_KEY, validate=True)
        lines = signature.decode("utf-8").splitlines()
        if (len(public) != 42 or public[:2] != b"Ed" or len(lines) != 4
                or not lines[0].startswith("untrusted comment:")
                or not lines[2].startswith("trusted comment: ")):
            _fail(code)
        packet = base64.b64decode(lines[1], validate=True)
        global_signature = base64.b64decode(lines[3], validate=True)
        if (len(packet) != 74 or packet[:2] != b"ED" or packet[2:10] != public[2:10]
                or len(global_signature) != 64):
            _fail(code)
        verifier = Ed25519PublicKey.from_public_bytes(public[10:])
        verifier.verify(packet[10:], hashlib.blake2b(body, digest_size=64).digest())
        verifier.verify(global_signature, packet[10:] + lines[2][len("trusted comment: "):].encode("utf-8"))
    except (UnicodeError, ValueError, binascii.Error, InvalidSignature):
        _fail(code)


def _release(manifest):
    if (manifest.get("schema") != "cortex.release.v1"
            or any(key not in manifest for key in IDENTITY)
            or type(manifest.get("release_sequence")) is not int
            or not isinstance(manifest.get("source_revision"), str)
            or re.fullmatch(r"[0-9a-f]{40}", manifest["source_revision"]) is None):
        _fail("cortex_release_identity_missing")
    if (manifest["release_id"] != "v0.1.003-manual.1"
            or manifest["release_lineage"] != "cortex-v1-manual"
            or manifest["release_sequence"] != 1
            or manifest["api_contract"] != "cortex-kos-v02009.v1"):
        _fail("cortex_release_unsupported")
    images = manifest.get("images")
    images = images.get("linux/amd64") if isinstance(images, dict) else None
    if (not isinstance(images, dict) or not {"db", "migrate", "api"} <= set(images)
            or not set(images) <= ROLES or any(not isinstance(ref, str) or IMAGE.fullmatch(ref) is None
                                              for ref in images.values())):
        _fail("cortex_image_mismatch")
    podman = manifest.get("podman")
    if (not isinstance(podman, dict) or podman.get("supported_family") != "6.0.x"
            or podman.get("tested_baseline") != "6.0.2"):
        _fail("cortex_podman_unsupported")
    return {key: manifest[key] for key in IDENTITY}, dict(images)


def _origin(value):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        parsed.port
        loopback = host == "localhost"
        if host and not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (not host or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
                or parsed.scheme not in {"http", "https"}
                or (parsed.scheme == "http" and not loopback)):
            raise ValueError("origin")
    except (ValueError, TypeError):
        _fail("cortex_descriptor_invalid")
    return value.rstrip("/")


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _http_get(url, headers, *, timeout, max_bytes):
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request.Request(url, headers=headers), timeout=timeout) as response:
            return response.status, response.read(max_bytes + 1)
    except error.HTTPError as failure:
        return failure.code, b""


def _private_directory(path, uid, code):
    path = _path(path, code)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        _fail(code)
    return path


def _same_json_types(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return (set(actual) == set(expected)
                and all(_same_json_types(actual[key], value) for key, value in expected.items()))
    if isinstance(expected, list):
        return (len(actual) == len(expected)
                and all(_same_json_types(a, b) for a, b in zip(actual, expected)))
    return True


def _publish(home, descriptor, uid):
    # Fresh installation only. An exact replay is read-only; another selected
    # installation is never silently overwritten.
    code = "cortex_descriptor_invalid"
    home_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    directory_fd = -1
    file_fd = -1
    temporary = ".prerequisite-" + secrets.token_hex(8)
    created = False
    published = False
    try:
        home_info = os.fstat(home_fd)
        try:
            os.mkdir(".cortex", mode=0o700, dir_fd=home_fd)
        except FileExistsError:
            pass
        directory_fd = os.open(".cortex", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home_fd)
        info = os.fstat(directory_fd)
        if info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
            _fail("cortex_descriptor_owner_mismatch")
        target = home / ".cortex/prerequisite.json"
        if target.exists() or target.is_symlink():
            existing = _json(_read(target, uid, 64 * 1024, code, private=True), code)
            if not _same_json_types(existing, descriptor):
                _fail(code)
            if existing != descriptor:
                _fail("cortex_instance_mismatch")
            return target
        body = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
        file_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=directory_fd)
        created = True
        view = memoryview(body)
        while view:
            count = os.write(file_fd, view)
            if count <= 0:
                _fail(code)
            view = view[count:]
        os.fsync(file_fd)
        if (home.stat().st_dev, home.stat().st_ino) != (home_info.st_dev, home_info.st_ino):
            _fail(code)
        current = (home / ".cortex").lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            _fail(code)
        held = os.fstat(file_fd)
        named = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
        if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
            _fail(code)
        os.link(temporary, "prerequisite.json", src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd, follow_symlinks=False)
        published = True
        target_info = os.stat("prerequisite.json", dir_fd=directory_fd, follow_symlinks=False)
        if (held.st_dev, held.st_ino) != (target_info.st_dev, target_info.st_ino):
            _fail(code)
        named = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
        if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
            _fail(code)
        os.unlink(temporary, dir_fd=directory_fd)
        created = False
        os.fsync(directory_fd)
        os.fsync(home_fd)
        current = (home / ".cortex").lstat()
        if ((current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                or _read(target, uid, 64 * 1024, code, private=True) != body):
            _fail(code)
        return target
    except BaseException as failure:
        if published and file_fd >= 0:
            # Never unlink a possibly replaced basename. Invalidate only the
            # just-published held inode so uncertain durability cannot admit it.
            try:
                os.ftruncate(file_fd, 0)
                os.fsync(file_fd)
            except OSError:
                pass
        if isinstance(failure, OSError):
            _fail(code)
        raise
    finally:
        if created and directory_fd >= 0 and file_fd >= 0:
            try:
                held = os.fstat(file_fd)
                named = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
                if (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino):
                    os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass
        if file_fd >= 0:
            os.close(file_fd)
        if directory_fd >= 0:
            os.close(directory_fd)
        os.close(home_fd)


def write_prerequisite(*, home, manifest_path, signature_path, api_url, installation_id,
                       credential_dir, project, http_get=None):
    uid = os.geteuid()
    if uid == 0 or os.getuid() != uid:
        _fail("cortex_descriptor_owner_mismatch")
    home = _path(home, "cortex_descriptor_owner_mismatch")
    home_info = home.stat()
    if not stat.S_ISDIR(home_info.st_mode) or home_info.st_uid != uid:
        _fail("cortex_descriptor_owner_mismatch")
    manifest_path = _path(manifest_path, "cortex_release_signature_invalid")
    signature_path = _path(signature_path, "cortex_release_signature_invalid")
    body = _read(manifest_path, uid, 4 * 1024 * 1024, "cortex_release_signature_invalid")
    signature = _read(signature_path, uid, 4096, "cortex_release_signature_invalid")
    _signature(body, signature)
    identity, images = _release(_json(body, "cortex_release_identity_missing"))
    api_url = _origin(api_url)
    try:
        if str(UUID(installation_id)) != installation_id:
            raise ValueError("instance")
    except (ValueError, TypeError, AttributeError):
        _fail("cortex_instance_mismatch")
    if not isinstance(project, str) or PROJECT.fullmatch(project) is None:
        _fail("cortex_descriptor_invalid")
    credential_dir = _private_directory(credential_dir, uid, "cortex_credential_unavailable")
    project_dir = _private_directory(credential_dir / project, uid, "cortex_credential_unavailable")
    credential_file = project_dir / "console.token"
    try:
        token = _read(credential_file, uid, 128, "cortex_credential_unavailable", private=True).decode("ascii").strip()
    except UnicodeError:
        _fail("cortex_credential_unavailable")
    if TOKEN.fullmatch(token) is None:
        _fail("cortex_credential_unavailable")
    try:
        status, health_body = (http_get or _http_get)(api_url + "/health",
            {"Authorization": "Bearer " + token, "X-Project": project, "X-Agent-Name": "console"},
            timeout=5, max_bytes=64 * 1024)
    except (OSError, ValueError, TimeoutError):
        _fail("cortex_health_unavailable")
    if status in {401, 403}:
        _fail("cortex_credential_refused")
    if status != 200 or not isinstance(health_body, bytes) or len(health_body) > 64 * 1024:
        _fail("cortex_health_unavailable")
    health = _json(health_body, "cortex_health_unavailable")
    if health.get("status") != "healthy" or health.get("postgres") != "connected" or health.get("rls_enforced") is not True:
        _fail("cortex_health_degraded")
    if health.get("installation_id") != installation_id:
        _fail("cortex_instance_mismatch")
    if any(health.get(key) != value or type(health.get(key)) is not type(value) for key, value in identity.items()):
        _fail("cortex_release_unsupported")
    # A validated response cannot authorize later changed release/credential material.
    if (_read(manifest_path, uid, 4 * 1024 * 1024, "cortex_release_signature_invalid") != body
            or _read(signature_path, uid, 4096, "cortex_release_signature_invalid") != signature):
        _fail("cortex_release_signature_invalid")
    _private_directory(credential_dir, uid, "cortex_credential_unavailable")
    _private_directory(project_dir, uid, "cortex_credential_unavailable")
    try:
        current_token = _read(credential_file, uid, 128, "cortex_credential_unavailable", private=True).decode("ascii").strip()
    except UnicodeError:
        _fail("cortex_credential_unavailable")
    if current_token != token:
        _fail("cortex_credential_unavailable")
    descriptor = {"schema": "cortex.prerequisite.v1", "api_url": api_url,
        "installation_id": installation_id, "owner_uid": uid, **identity,
        "platform": "linux/amd64", "host_os": "linux", "release_manifest": str(manifest_path),
        "release_signature": str(signature_path), "images": images, "health_path": "/health",
        "credential_dir": str(credential_dir), "credential_file": str(credential_file),
        "credential_project": project, "credential_agent": "console",
        "podman": {"supported_family": "6.0.x", "tested_baseline": "6.0.2",
                   "machine_name": None, "machine_image_digest": None},
        "install_guide": "https://github.com/Kaidera-AI/cortex/releases/tag/" + identity["release_id"]}
    target = _publish(home, descriptor, uid)
    return {"path": str(target), "schema": descriptor["schema"], "release_id": identity["release_id"],
            "installation_id": installation_id, "platform": "linux/amd64"}


def main(argv=None):
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Write the signed Cortex CM-1 prerequisite after qualified manual setup.")
    parser.add_argument("--home", type=Path, default=Path.home(), help="selected runtime user's home")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--signature", required=True, type=Path)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--installation-id", required=True, help="instance UUID from the private owner status")
    parser.add_argument("--credential-dir", required=True, type=Path)
    parser.add_argument("--project", required=True)
    args = parser.parse_args(argv)
    try:
        receipt = write_prerequisite(home=args.home, manifest_path=args.manifest,
            signature_path=args.signature, api_url=args.api_url, installation_id=args.installation_id,
            credential_dir=args.credential_dir, project=args.project)
    except PrerequisiteRefusal as failure:
        print(json.dumps({"ok": False, "reason": failure.code}), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **receipt}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
