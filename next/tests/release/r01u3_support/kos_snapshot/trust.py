"""Step 0 of every installer mode: verify the release before anything is written (plan section 3).

This module only reads. release.json is verified against the pinned Minisign public key before one
byte of it is parsed; then the payload tree and the app-DB archive this host will use are checked
against the digests the signature covers. Any mismatch is a typed refusal (R1L-01).

The signature is Minisign's own format, checked here with Ed25519 so a fresh Mac needs no minisign
binary: algorithm "ED" signs the BLAKE2b-512 of the file, "Ed" signs the file; a second signature
covers the first plus the trusted comment.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .refusal import Refusal

RELEASE_FILE = "release.json"
SIGNATURE_FILE = "release.json.minisig"
TREE_DIR = "runtime"
IMAGES_DIR = "images"
SCHEMA = "kaidera-os.release.v1"
VERSION = re.compile(r"\d+\.\d+\.\d+")
COMMENT_PREFIX = "trusted comment: "


@dataclass(frozen=True)
class VerifiedRelease:
    release: dict
    release_sha256: str
    platform: str
    tree: Path
    appdb_archive: Path


def _decode(text: str, length: int, code: str) -> bytes:
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise Refusal(code) from None
    if len(raw) != length:
        raise Refusal(code)
    return raw


def _public_key(text: str) -> tuple[bytes, Ed25519PublicKey]:
    """The key id and key of a Minisign public key: the two-line file or its bare base64 line."""
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith("untrusted comment:")]
    if len(lines) != 1:
        raise Refusal("pinned_key_invalid")
    raw = _decode(lines[0], 42, "pinned_key_invalid")
    if raw[:2] != b"Ed":
        raise Refusal("pinned_key_invalid")
    return raw[2:10], Ed25519PublicKey.from_public_bytes(raw[10:])


def verify_minisign(message: bytes, signature: str, public_key: str) -> dict:
    pinned_id, key = _public_key(public_key)
    lines = signature.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) != 4 or not lines[0].startswith("untrusted comment:") or not lines[2].startswith(COMMENT_PREFIX.rstrip()):
        raise Refusal("release_signature_invalid")
    raw = _decode(lines[1], 74, "release_signature_invalid")
    algorithm, key_id, first = raw[:2], raw[2:10], raw[10:]
    if algorithm not in (b"ED", b"Ed") or key_id != pinned_id:
        raise Refusal("release_signature_invalid")
    comment = lines[2][len(COMMENT_PREFIX):]
    second = _decode(lines[3], 64, "release_signature_invalid")
    try:
        key.verify(first, hashlib.blake2b(message, digest_size=64).digest() if algorithm == b"ED" else message)
        key.verify(second, first + comment.encode("utf-8"))
    except InvalidSignature:
        raise Refusal("release_signature_invalid") from None
    return {"key_id": key_id[::-1].hex().upper(), "trusted_comment": comment, "prehashed": algorithm == b"ED"}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_rows(tree: Path) -> list[dict]:
    """Every file and symlink under `tree` with its mode class, size and sha256 (lstat: links are never followed).

    The same listing build-runtime-tree.py writes, so the signed digest can be recomputed here."""
    rows = []
    for current, dirnames, filenames in os.walk(tree):
        for name in dirnames + filenames:
            full = Path(current) / name
            info = os.lstat(full)
            relative = full.relative_to(tree).as_posix()
            if stat.S_ISLNK(info.st_mode):
                rows.append({"path": relative, "type": "symlink", "target": os.readlink(full)})
            elif stat.S_ISREG(info.st_mode):
                rows.append({
                    "path": relative, "type": "file", "mode": "0755" if info.st_mode & 0o100 else "0644",
                    "size": info.st_size, "sha256": _sha256_file(full),
                })
            elif not stat.S_ISDIR(info.st_mode):
                raise Refusal("payload_tree_mismatch", f"unexpected file type: {relative}")
    return sorted(rows, key=lambda row: row["path"])


def tree_digest(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _row(rows: object, platform: str) -> dict | None:
    return next((row for row in rows if isinstance(row, dict) and row.get("platform") == platform), None) if isinstance(rows, list) else None


def verify_package(package: Path, *, public_key: str, platform: str, image_platform: str,
                   release_dir: Path | None = None) -> VerifiedRelease:
    """Verify the release and the package it covers. The signed release.json is read from `release_dir` when one is given
    (the release is published beside the artifacts, which cannot hold a manifest that names their own digests) and from the
    package otherwise; the package's own copy is never read when a release folder is given."""
    signed = package if release_dir is None else release_dir
    try:
        body = (signed / RELEASE_FILE).read_bytes()
    except OSError:
        raise Refusal("release_json_missing") from None
    try:
        signature = (signed / SIGNATURE_FILE).read_bytes().decode("utf-8")
    except OSError:
        raise Refusal("release_signature_missing") from None
    except UnicodeDecodeError:
        raise Refusal("release_signature_invalid") from None
    verify_minisign(body, signature, public_key)

    try:
        release = json.loads(body)
    except ValueError:
        raise Refusal("release_json_invalid") from None
    if not isinstance(release, dict):
        raise Refusal("release_json_invalid")
    if release.get("schema") != SCHEMA:
        raise Refusal("release_schema_unsupported", str(release.get("schema"))[:40])
    version = release.get("version")  # it names the runtime folder and orders upgrades: well formed, whatever was signed (rulings r53 4)
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise Refusal("release_json_invalid", "version")

    tree_row = _row(release.get("runtime_trees"), platform)
    if tree_row is None:
        raise Refusal("platform_not_in_release", platform)
    tree = package / TREE_DIR
    if tree.is_symlink() or not tree.is_dir():
        raise Refusal("payload_tree_missing")
    if tree_digest(tree_rows(tree)) != tree_row.get("tree_sha256"):
        raise Refusal("payload_tree_mismatch")

    appdb = release.get("images")
    appdb = appdb.get("appdb") if isinstance(appdb, dict) else None
    image = _row(appdb.get("images") if isinstance(appdb, dict) else None, image_platform)
    if image is None:
        raise Refusal("image_platform_not_in_release", image_platform)
    name = image.get("file")
    if not isinstance(name, str) or Path(name).name != name or name in ("", ".", ".."):
        raise Refusal("release_json_invalid", "image file name")
    digest = image.get("image_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise Refusal("release_json_invalid", "image digest")
    archive = package / IMAGES_DIR / name
    if archive.is_symlink() or not archive.is_file():
        raise Refusal("artifact_missing", name)
    if _sha256_file(archive) != image.get("sha256"):
        raise Refusal("artifact_digest_mismatch", name)
    return VerifiedRelease(release, hashlib.sha256(body).hexdigest(), platform, tree, archive)
