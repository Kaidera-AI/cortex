#!/usr/bin/env python3
"""Fail closed unless the runtime Qwen cache matches its immutable receipt."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ARTIFACT_ROOT = Path("/opt/kaidera-qwen3")
CACHE_ROOT = ARTIFACT_ROOT / "cache"
PINS_PATH = ARTIFACT_ROOT / "qwen3-model-pins.json"
RECEIPT_PATH = ARTIFACT_ROOT / "kaidera-qwen3-model-receipt.json"
EXPECTED_REPO_ID = "n24q02m/Qwen3-Embedding-0.6B-ONNX"
EXPECTED_REVISION = "dc873d64d6143f27ad68dadbd1f0d9a4371b994e"
EXPECTED_LICENSE = "Apache-2.0"
EXPECTED_PATHS = {
    "config.json",
    "onnx/model_quantized.onnx",
    "tokenizer.json",
    "tokenizer_config.json",
}
EXPECTED_SNAPSHOT_DIRECTORIES = {"onnx"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read qwen3 contract file: {path.name}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"qwen3 contract file is not an object: {path.name}")
    return value


def _load_pin() -> dict[str, Any]:
    pins = _read_json(PINS_PATH)
    if set(pins) != {"schema_version", "model"} or pins.get("schema_version") != 1:
        raise RuntimeError("qwen3 runtime pin has an unsupported shape")
    pin = pins.get("model")
    if not isinstance(pin, dict) or set(pin) != {
        "artifacts",
        "license",
        "repo_id",
        "revision",
    }:
        raise RuntimeError("qwen3 runtime model pin has an unsupported shape")
    if pin.get("repo_id") != EXPECTED_REPO_ID:
        raise RuntimeError("qwen3 runtime pin selects an unexpected repository")
    if pin.get("revision") != EXPECTED_REVISION:
        raise RuntimeError("qwen3 runtime pin differs from the reviewed exact commit")
    if pin.get("license") != EXPECTED_LICENSE:
        raise RuntimeError("qwen3 runtime pin selects an unexpected licence")

    artifacts = pin.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != len(EXPECTED_PATHS):
        raise RuntimeError("qwen3 runtime pin must select exactly four artifacts")
    if not all(
        isinstance(item, dict)
        and set(item) == {"path", "sha256", "size"}
        and isinstance(item["path"], str)
        and isinstance(item["sha256"], str)
        and len(item["sha256"]) == 64
        and all(char in "0123456789abcdef" for char in item["sha256"])
        and isinstance(item["size"], int)
        and item["size"] > 0
        for item in artifacts
    ):
        raise RuntimeError("qwen3 runtime artifact pin is invalid")
    selected = [item["path"] for item in artifacts]
    if set(selected) != EXPECTED_PATHS or len(selected) != len(set(selected)):
        raise RuntimeError(
            "qwen3 runtime artifact allowlist differs from the image contract"
        )
    return pin


def _snapshot_dir() -> Path:
    model_cache = CACHE_ROOT / "models--n24q02m--Qwen3-Embedding-0.6B-ONNX"
    if CACHE_ROOT.is_symlink() or not CACHE_ROOT.is_dir():
        raise RuntimeError("qwen3 runtime cache root is not a real directory")
    if (model_cache / "refs").exists():
        raise RuntimeError("qwen3 runtime cache contains a mutable branch ref")
    cache_entries = list(CACHE_ROOT.iterdir())
    if (
        cache_entries != [model_cache]
        or model_cache.is_symlink()
        or not model_cache.is_dir()
    ):
        raise RuntimeError(
            "qwen3 runtime cache root must contain exactly the expected model directory"
        )
    snapshots = model_cache / "snapshots"
    snapshot = snapshots / EXPECTED_REVISION
    snapshot_entries = list(snapshots.iterdir()) if snapshots.is_dir() else []
    if (
        snapshots.is_symlink()
        or snapshot_entries != [snapshot]
        or snapshot.is_symlink()
        or not snapshot.is_dir()
    ):
        raise RuntimeError(
            "qwen3 runtime cache does not contain exactly the reviewed revision"
        )
    if (CACHE_ROOT / ".locks").exists():
        raise RuntimeError("qwen3 runtime cache retains mutable downloader locks")
    if {path.name for path in model_cache.iterdir()} != {"blobs", "snapshots"}:
        raise RuntimeError(
            "qwen3 runtime cache contains state outside blobs and the exact snapshot"
        )
    return snapshot


def verify() -> None:
    pin = _load_pin()
    snapshot = _snapshot_dir()
    actual = {
        path.relative_to(snapshot).as_posix()
        for path in snapshot.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual != EXPECTED_PATHS:
        raise RuntimeError(
            "qwen3 runtime snapshot inventory mismatch: "
            f"missing={sorted(EXPECTED_PATHS - actual)!r} "
            f"extra={sorted(actual - EXPECTED_PATHS)!r}"
        )
    actual_directories = {
        path.relative_to(snapshot).as_posix()
        for path in snapshot.rglob("*")
        if path.is_dir() and not path.is_symlink()
    }
    if actual_directories != EXPECTED_SNAPSHOT_DIRECTORIES:
        raise RuntimeError("qwen3 runtime snapshot directory inventory mismatch")

    expected = {item["path"]: item for item in pin["artifacts"]}
    cache_root = CACHE_ROOT.resolve(strict=True)
    blobs_dir = snapshot.parents[1] / "blobs"
    if not blobs_dir.is_dir() or any(
        not path.is_file() or path.is_symlink() for path in blobs_dir.iterdir()
    ):
        raise RuntimeError("qwen3 runtime cache blob store has an unsupported shape")
    blob_files = {path.resolve(strict=True) for path in blobs_dir.iterdir()}
    verified: list[dict[str, int | str]] = []
    referenced_blobs: set[Path] = set()
    for relative in sorted(EXPECTED_PATHS):
        path = snapshot / relative
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(cache_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(
                f"qwen3 runtime cache path escapes its root: {relative}"
            ) from exc
        referenced_blobs.add(resolved)
        size = path.stat().st_size
        digest = _sha256(path)
        if size != expected[relative]["size"] or digest != expected[relative]["sha256"]:
            raise RuntimeError(
                f"qwen3 runtime artifact does not match its pin: {relative}"
            )
        verified.append({"path": relative, "sha256": digest, "size": size})
    if blob_files != referenced_blobs:
        raise RuntimeError(
            "qwen3 runtime blob inventory differs from the four pinned artifacts"
        )

    expected_receipt = {
        "model": {
            "cache_dir": str(CACHE_ROOT),
            "files": verified,
            "license": EXPECTED_LICENSE,
            "repo_id": EXPECTED_REPO_ID,
            "revision": EXPECTED_REVISION,
        },
        "schema_version": 1,
    }
    if _read_json(RECEIPT_PATH) != expected_receipt:
        raise RuntimeError("qwen3 runtime receipt does not match the verified snapshot")


if __name__ == "__main__":
    verify()
