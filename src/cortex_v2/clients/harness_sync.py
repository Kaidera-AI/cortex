"""Host-side harness mirror executor (R06 host-adapter half).

The server owns generation/provenance/drift truth; this module executes the
committed plan on disk: atomic file writes, removal of files a rolled-back
generation introduced, and post-write hash verification. Paths are validated
against traversal; nothing outside the target root is touched.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from .client import CortexClient
from .errors import ClientError


class HarnessSyncError(ClientError):
    """A host-side mirror execution failure (never a partial success)."""


def _safe_relative(path: str) -> PurePosixPath:
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in ("..", "") for part in pure.parts):
        raise HarnessSyncError(f"unsafe mirror path: {path!r}")
    return pure


def observed_manifest(root: Path) -> dict[str, str]:
    """sha256 per relative POSIX path for every file under ``root``."""
    manifest: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            absolute = Path(dirpath) / name
            relative = absolute.relative_to(root).as_posix()
            manifest[relative] = hashlib.sha256(absolute.read_bytes()).hexdigest()
    return manifest


def apply_files(
    root: Path,
    files: Iterable[Mapping[str, Any]],
    remove: Iterable[str] = (),
) -> None:
    """Atomically write ``files`` ([{path, body}]) and delete ``remove``."""
    root = root.resolve()
    for entry in files:
        relative = _safe_relative(entry["path"])
        target = root / Path(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".cortex-tmp")
        temporary.write_bytes(entry["body"].encode("utf-8"))
        os.replace(temporary, target)
    for path in remove:
        relative = _safe_relative(path)
        target = root / Path(*relative.parts)
        target.unlink(missing_ok=True)
    _prune_empty_dirs(root)


def _prune_empty_dirs(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        current = Path(dirpath)
        if current == root:
            continue
        if not dirnames and not filenames:
            try:
                current.rmdir()
            except OSError:
                pass


def _verify(root: Path, expected: Iterable[Mapping[str, Any]]) -> None:
    for entry in expected:
        relative = _safe_relative(entry["path"])
        target = root / Path(*relative.parts)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != entry["content_sha256"]:
            raise HarnessSyncError(
                f"verification failed for {entry['path']}: wrote {digest}, "
                f"server committed {entry['content_sha256']}"
            )


def check_drift(
    client: CortexClient, root: Path, mirror_label: str, *,
    scope: str | None = None,
) -> dict[str, Any]:
    result = client.call(
        "harness.drift",
        payload={
            "mirror_label": mirror_label,
            "observed_manifest": observed_manifest(root),
        },
        scope=scope,
    )
    return result.data


def sync_mirror(
    client: CortexClient,
    root: Path,
    mirror_label: str,
    *,
    pins: Mapping[str, int] | None = None,
    scope: str | None = None,
    idempotency_key: str,
) -> dict[str, Any]:
    """Preview → apply → write bytes → verify against the committed receipt.

    A hand-edited host mirror makes apply fail with the typed server problem
    ``harness_drift_detected`` (raised as CortexApiError); nothing is
    overwritten.
    """
    preview = client.call(
        "harness.preview",
        payload={"mirror_label": mirror_label, "pins": dict(pins or {})},
        scope=scope,
    ).data
    applied = client.call(
        "harness.apply",
        payload={
            "mirror_label": mirror_label,
            "pins": dict(pins or {}),
            "observed_manifest": observed_manifest(root) or None,
        },
        scope=scope,
        idempotency_key=idempotency_key,
    ).data
    if applied["manifest_sha256"] != preview["manifest_sha256"]:
        raise HarnessSyncError(
            "mirror inputs changed between preview and apply "
            f"({preview['manifest_sha256']} != {applied['manifest_sha256']}); "
            "re-run the sync"
        )
    apply_files(root, preview["files"])
    _verify(root, applied["files"])
    return {
        "generation": applied["generation"],
        "manifest_sha256": applied["manifest_sha256"],
        "written": [entry["path"] for entry in preview["files"]],
        "removed": [],
        "verified": True,
    }


def rollback_mirror(
    client: CortexClient,
    root: Path,
    mirror_label: str,
    *,
    target_generation: int | None = None,
    scope: str | None = None,
    idempotency_key: str,
) -> dict[str, Any]:
    """Execute the server-computed rollback plan on disk: restore changed
    files and remove files the rolled-back generation introduced."""
    result = client.call(
        "harness.rollback",
        payload={
            "mirror_label": mirror_label,
            "target_generation": target_generation,
            "observed_manifest": observed_manifest(root),
        },
        scope=scope,
        idempotency_key=idempotency_key,
    ).data
    apply_files(root, result["restore"], result["remove"])
    _verify(root, result["restore"])
    return {
        "generation": result["generation"],
        "manifest_sha256": result["manifest_sha256"],
        "rolled_back_from": result["rolled_back_from"],
        "rolled_back_to": result["rolled_back_to"],
        "written": [entry["path"] for entry in result["restore"]],
        "removed": list(result["remove"]),
        "verified": True,
    }

