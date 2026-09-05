"""Read-only Cortex port over the host-derived OpenKai provider projection.

The deployment mounts only ``~/.openkai/kos-cortex-provider`` read-only.  Its
pending marker and token-free manifest bind ``provider.env`` to the sole editable
``~/.openkai/.env`` authority without exposing OpenKai OAuth/session files.
Provider names come only from the hash-pinned OpenKai snapshot; process
environment and KOS app_settings are never credential fallbacks.

This module intentionally has no logging and exposes no status/HTTP payload.  Its
return values are in-memory call credentials for embedding/rerank/analysis only.
"""

from __future__ import annotations

import hashlib
import errno
import json
import os
import re
import stat
from pathlib import Path


REGISTRY_SHA256 = "c71e04025710cbddf6fa1942b9752b9ac5c90837c82c7492c220ce945847d2de"
REGISTRY_SOURCE_COMMIT = "f3660f3c19939d2a6ff3b95be9aab3f85fb8312a"
PROJECTION_SCHEMA = "kos.openkai.provider-projection.v2"
PROJECTION_ENV_NAME = "provider.env"
PROJECTION_MANIFEST_NAME = "manifest.json"
PROJECTION_PENDING_NAME = "pending.json"
MAX_ENV_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 16 * 1024
_ASSIGNMENT = re.compile(r"^[ \t]*(?:export[ \t]+)?(?P<key>[A-Z_][A-Z0-9_]*)=(?P<value>.*)$")
_SAFE_UNQUOTED = re.compile(r"^[^\s#'\"\x00-\x1f\x7f]+$")


class OpenKaiProviderEnvError(RuntimeError):
    pass


def _registry_path() -> Path:
    override = (os.getenv("OPENKAI_PROVIDER_REGISTRY_FILE") or "").strip()
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise OpenKaiProviderEnvError("OpenKai provider registry path must be absolute")
        return path
    return Path(__file__).resolve().parents[2] / "redistributable" / "config" / "openkai-providers.json"


def _load_registry() -> tuple[dict[str, tuple[str, ...]], frozenset[str]]:
    try:
        data = _registry_path().read_bytes()
    except OSError as exc:
        raise OpenKaiProviderEnvError("OpenKai provider registry is unavailable") from exc
    if hashlib.sha256(data).hexdigest() != REGISTRY_SHA256:
        raise OpenKaiProviderEnvError("OpenKai provider registry hash mismatch")
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OpenKaiProviderEnvError("OpenKai provider registry is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != "openkai.providers.snapshot.v1"
        or payload.get("source_commit") != REGISTRY_SOURCE_COMMIT
        or payload.get("source_path") != "packages/cli/src/providers.ts"
        or not isinstance(payload.get("providers"), list)
        or len(payload["providers"]) != 21
    ):
        raise OpenKaiProviderEnvError("OpenKai provider registry provenance mismatch")
    providers: dict[str, tuple[str, ...]] = {}
    all_keys: set[str] = set()
    for row in payload["providers"]:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("env_keys"), list)
            or not isinstance(row.get("oauth"), bool)
            or not str(row.get("label") or "")
        ):
            raise OpenKaiProviderEnvError("OpenKai provider registry row is invalid")
        provider = str(row.get("id") or "")
        keys = tuple(str(item) for item in row["env_keys"])
        if not provider or provider in providers or any(
            not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) for key in keys
        ) or len(set(keys)) != len(keys) or all_keys.intersection(keys):
            raise OpenKaiProviderEnvError("OpenKai provider registry row is invalid")
        providers[provider] = keys
        all_keys.update(keys)
    if payload.get("default_provider") not in providers:
        raise OpenKaiProviderEnvError("OpenKai provider registry default is invalid")
    return providers, frozenset(all_keys)


def _decode_value(raw: str, line_number: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        if len(value) < 2 or value[-1] != value[0]:
            raise OpenKaiProviderEnvError(f"invalid OpenKai env grammar at line {line_number}")
        value = value[1:-1]
    elif not _SAFE_UNQUOTED.fullmatch(value):
        raise OpenKaiProviderEnvError(f"invalid OpenKai env grammar at line {line_number}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise OpenKaiProviderEnvError(f"invalid OpenKai env grammar at line {line_number}")
    return value


def _read_regular(path: Path, *, limit: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise OpenKaiProviderEnvError("OpenKai provider env path is unsafe") from exc
        raise OpenKaiProviderEnvError("OpenKai provider projection is unavailable") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OpenKaiProviderEnvError("OpenKai provider projection path is unsafe")
        # The host writer enforces ownership. Container UID mappings differ across
        # rootful Docker/rootless Podman, so this read-only mount validates type and
        # exact mode but deliberately does not compare st_uid to container euid.
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise OpenKaiProviderEnvError(
                "OpenKai provider projection permissions are unsafe"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise OpenKaiProviderEnvError(
                    "OpenKai provider projection exceeds the size limit"
                )
        return b"".join(chunks)
    except OpenKaiProviderEnvError:
        raise
    except OSError as exc:
        raise OpenKaiProviderEnvError("OpenKai provider projection is unavailable") from exc
    finally:
        os.close(fd)


def _projection_dir() -> Path:
    raw_path = (os.getenv("OPENKAI_PROVIDER_PROJECTION_DIR") or "").strip()
    if not raw_path:
        raise OpenKaiProviderEnvError("OpenKai provider projection is unavailable")
    path = Path(raw_path)
    if not path.is_absolute():
        raise OpenKaiProviderEnvError("OpenKai provider projection path must be absolute")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise OpenKaiProviderEnvError("OpenKai provider projection is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OpenKaiProviderEnvError("OpenKai provider projection path is unsafe")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise OpenKaiProviderEnvError(
            "OpenKai provider projection permissions are unsafe"
        )
    return path


def _pending_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise OpenKaiProviderEnvError("OpenKai provider projection is unavailable") from exc
    return True


def _parse_projection_values(
    data: bytes, providers: dict[str, tuple[str, ...]]
) -> dict[str, str]:
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise OpenKaiProviderEnvError("OpenKai provider projection is not UTF-8") from exc
    canonical = {keys[0] for keys in providers.values() if keys}
    values: dict[str, str] = {}
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        match = _ASSIGNMENT.fullmatch(line)
        if match is None:
            raise OpenKaiProviderEnvError(
                f"invalid OpenKai projection grammar at line {index}"
            )
        key = match.group("key")
        if key not in canonical:
            raise OpenKaiProviderEnvError(
                "OpenKai provider projection contains a noncanonical key"
            )
        if key in values:
            raise OpenKaiProviderEnvError(
                f"duplicate OpenKai projection assignment at line {index}"
            )
        value = _decode_value(match.group("value"), index)
        if value:
            values[key] = value
    return values


def load_provider_projection() -> tuple[dict[str, str], str]:
    """Provider ID -> canonical API key, in memory only.

    The returned revision is token-free and must be checked against the masked
    host status before callers accept the mapping. Compatibility aliases remain
    valid for OpenKai itself but never enter this fixed-semantics Cortex cache.
    """
    providers, _allowlist = _load_registry()
    directory = _projection_dir()
    pending = directory / PROJECTION_PENDING_NAME
    env_path = directory / PROJECTION_ENV_NAME
    manifest_path = directory / PROJECTION_MANIFEST_NAME
    if _pending_exists(pending):
        raise OpenKaiProviderEnvError("OpenKai provider projection is incomplete")

    manifest_before = _read_regular(manifest_path, limit=MAX_MANIFEST_BYTES)
    projection_data = _read_regular(env_path, limit=MAX_ENV_BYTES)
    manifest_after = _read_regular(manifest_path, limit=MAX_MANIFEST_BYTES)
    if manifest_before != manifest_after or _pending_exists(pending):
        raise OpenKaiProviderEnvError("OpenKai provider projection changed during read")
    names = {entry.name for entry in os.scandir(directory)}
    if names != {PROJECTION_ENV_NAME, PROJECTION_MANIFEST_NAME}:
        raise OpenKaiProviderEnvError("OpenKai provider projection contains extra files")
    try:
        manifest = json.loads(manifest_before)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OpenKaiProviderEnvError("OpenKai provider projection manifest is invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "registry_sha256",
        "authority_existed",
        "authority_revision",
        "projection_sha256",
    }:
        raise OpenKaiProviderEnvError("OpenKai provider projection manifest is invalid")
    revision = manifest.get("authority_revision")
    projection_hash = manifest.get("projection_sha256")
    if (
        manifest.get("schema_version") != PROJECTION_SCHEMA
        or manifest.get("registry_sha256") != REGISTRY_SHA256
        or not isinstance(manifest.get("authority_existed"), bool)
        or not isinstance(revision, str)
        or re.fullmatch(r"[0-9a-f]{64}", revision) is None
        or not isinstance(projection_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", projection_hash) is None
        or not hashlib.sha256(projection_data).hexdigest() == projection_hash
    ):
        raise OpenKaiProviderEnvError("OpenKai provider projection manifest mismatch")

    values = _parse_projection_values(projection_data, providers)
    resolved: dict[str, str] = {}
    for provider, env_keys in providers.items():
        value = values.get(env_keys[0], "") if env_keys else ""
        if value:
            resolved[provider] = value
    return resolved, revision


def load_provider_keys() -> dict[str, str]:
    """Compatibility wrapper; primary callers also verify the live revision."""
    keys, _revision = load_provider_projection()
    return keys


def provider_ids() -> tuple[str, ...]:
    providers, _allowlist = _load_registry()
    return tuple(providers)
