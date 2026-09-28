"""Blob port: resolving the original bytes a content revision references.

W1 stores artifact ownership, hashes and a location reference; the bytes live in
a filesystem-backed blob adapter. This port is the only place Processing reads
original bytes, and it is bounded: a configured root, no traversal above it, no
symlink escape, a size cap and an optional hash check against the recorded
sha256. Nothing here touches the database, so it can never extend a
transaction, and the worker always calls it from a thread executor.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .contracts import Outcome

DEFAULT_MAX_BLOB_BYTES = 32_000_000


@dataclass(frozen=True, slots=True)
class BlobResult:
    """Typed blob read result; ``data`` is None unless the outcome is OK."""

    outcome: Outcome
    data: bytes | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


@runtime_checkable
class BlobResolver(Protocol):
    """Outward port for original bytes."""

    async def read(
        self,
        location: str,
        *,
        expected_sha256: bytes | None = None,
        max_bytes: int = DEFAULT_MAX_BLOB_BYTES,
    ) -> BlobResult:
        """Read one referenced object, or return a typed failure."""


class NullBlobResolver:
    """Default when no blob root is configured: honest unavailability."""

    def __init__(self, detail: str = "no blob resolver is configured"):
        self.detail = detail

    async def read(
        self,
        location: str,
        *,
        expected_sha256: bytes | None = None,
        max_bytes: int = DEFAULT_MAX_BLOB_BYTES,
    ) -> BlobResult:
        return BlobResult(
            outcome=Outcome.SOURCE_BYTES_UNAVAILABLE,
            detail=f"{self.detail}; location reference was not dereferenced",
        )


class FilesystemBlobResolver:
    """Root-confined filesystem blob adapter.

    The location reference is data, not a path instruction: it must be a
    relative POSIX path inside the configured root. Absolute paths, ``..``
    segments, NUL bytes and symlinks that resolve outside the root are refused
    before any read, so a crafted artifact reference cannot reach host files or
    ambient credentials.
    """

    def __init__(self, root: str | os.PathLike[str], *, detail_prefix: str = "blob store"):
        self.root = Path(root)
        self.detail_prefix = detail_prefix
        self._resolved_root: Path | None = None

    def _root(self) -> Path | None:
        if self._resolved_root is None:
            try:
                self._resolved_root = self.root.resolve(strict=True)
            except (OSError, RuntimeError):
                return None
        return self._resolved_root

    def _target(self, location: str) -> Path | None:
        root = self._root()
        if root is None:
            return None
        if not location or "\x00" in location or location.startswith(("/", "\\")):
            return None
        if any(part in ("", ".", "..") for part in location.split("/")[:-1]) or ".." in (
            location.split("/")
        ):
            return None
        candidate = (root / location).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    def _read_sync(
        self, location: str, expected_sha256: bytes | None, max_bytes: int
    ) -> BlobResult:
        target = self._target(location)
        if target is None:
            return BlobResult(
                outcome=Outcome.INPUT_REJECTED,
                detail=f"{self.detail_prefix} location is not a confined relative path",
            )
        try:
            if not target.is_file():
                return BlobResult(
                    outcome=Outcome.SOURCE_BYTES_UNAVAILABLE,
                    detail=f"{self.detail_prefix} object is missing",
                )
            size = target.stat().st_size
            if size > max_bytes:
                return BlobResult(
                    outcome=Outcome.QUOTA_EXCEEDED,
                    detail=(
                        f"{self.detail_prefix} object is {size} bytes, "
                        f"the bound is {max_bytes}"
                    ),
                )
            with target.open("rb") as handle:
                data = handle.read(max_bytes + 1)
        except OSError as exc:
            return BlobResult(
                outcome=Outcome.TEMPORARILY_UNAVAILABLE,
                detail=f"{self.detail_prefix} read failed: {type(exc).__name__}",
            )
        if len(data) > max_bytes:
            return BlobResult(
                outcome=Outcome.QUOTA_EXCEEDED,
                detail=f"{self.detail_prefix} object exceeds the {max_bytes} byte bound",
            )
        if expected_sha256 is not None:
            digest = hashlib.sha256(data).digest()
            if digest != expected_sha256:
                return BlobResult(
                    outcome=Outcome.INPUT_CORRUPT,
                    detail=(
                        f"{self.detail_prefix} bytes do not match the recorded sha256; "
                        "the catalog reference was not trusted"
                    ),
                )
        return BlobResult(outcome=Outcome.OK, data=data)

    async def read(
        self,
        location: str,
        *,
        expected_sha256: bytes | None = None,
        max_bytes: int = DEFAULT_MAX_BLOB_BYTES,
    ) -> BlobResult:
        return await asyncio.to_thread(
            self._read_sync, location, expected_sha256, max_bytes
        )


def resolver_from_root(root: str | os.PathLike[str] | None) -> BlobResolver:
    """Build the configured resolver, or the honest unavailable one."""
    if not root:
        return NullBlobResolver()
    return FilesystemBlobResolver(root)


def expected_digest(payload: dict[str, object]) -> bytes | None:
    """Recorded artifact hash from a W1 payload, when it is a valid sha256."""
    value = payload.get("bytes_sha256")
    if not isinstance(value, str) or len(value) != 64:
        return None
    try:
        return bytes.fromhex(value)
    except ValueError:
        return None


__all__ = [
    "DEFAULT_MAX_BLOB_BYTES",
    "BlobResolver",
    "BlobResult",
    "FilesystemBlobResolver",
    "NullBlobResolver",
    "expected_digest",
    "resolver_from_root",
]
