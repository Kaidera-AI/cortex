#!/usr/bin/env python3
"""Fetch and receipt the exact Qwen3 ONNX cache used by BCRG.

This runs only in the networked image builder. The runtime image receives the
standard Hugging Face cache layout at one immutable commit, without downloader
locks or a branch ref, and forces qwen3-embed to consume it offline.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
from typing import Any

from huggingface_hub import snapshot_download


PINS_PATH = Path("/build/qwen3-model-pins.json")
ARTIFACT_ROOT = Path("/opt/kaidera-qwen3")
CACHE_ROOT = ARTIFACT_ROOT / "cache"
RECEIPT_NAME = "kaidera-qwen3-model-receipt.json"
PIN_COPY_NAME = "qwen3-model-pins.json"
EXPECTED_REPO_ID = "n24q02m/Qwen3-Embedding-0.6B-ONNX"
EXPECTED_LICENSE = "Apache-2.0"
EXPECTED_REVISION = "dc873d64d6143f27ad68dadbd1f0d9a4371b994e"
EXPECTED_PATHS = {
    "config.json",
    "onnx/model_quantized.onnx",
    "tokenizer.json",
    "tokenizer_config.json",
}
EXPECTED_SNAPSHOT_DIRECTORIES = {"onnx"}
EXPECTED_CACHEDIR_TAG_CONTENT = (
    "Signature: 8a477f597d28d172789f06886806bc55\n"
    "# This file is a cache directory tag created by huggingface_hub.\n"
    "# For information about cache directory tags, see:\n"
    "#\thttps://bford.info/cachedir/\n"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_pin() -> dict[str, Any]:
    pins = json.loads(PINS_PATH.read_text(encoding="utf-8"))
    if set(pins) != {"schema_version", "model"} or pins.get("schema_version") != 1:
        raise RuntimeError("qwen3-model-pins.json has an unsupported shape")
    pin = pins.get("model")
    if not isinstance(pin, dict) or set(pin) != {
        "artifacts",
        "license",
        "repo_id",
        "revision",
    }:
        raise RuntimeError("qwen3 model pin has an unsupported shape")
    _validate_pin(pin)
    return pin


def _validate_pin(pin: dict[str, Any]) -> None:
    artifacts = pin.get("artifacts")
    revision = pin.get("revision")
    if pin.get("repo_id") != EXPECTED_REPO_ID:
        raise RuntimeError("qwen3 model pin selects an unexpected repository")
    if pin.get("license") != EXPECTED_LICENSE:
        raise RuntimeError("qwen3 model pin selects an unexpected licence")
    if revision != EXPECTED_REVISION:
        raise RuntimeError(
            "qwen3 model revision differs from the reviewed exact commit"
        )
    if not isinstance(artifacts, list) or len(artifacts) != len(EXPECTED_PATHS):
        raise RuntimeError("qwen3 model pin must select exactly four artifacts")
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
        raise RuntimeError("qwen3 model artifact pin is invalid")
    selected = [item["path"] for item in artifacts]
    if set(selected) != EXPECTED_PATHS or len(selected) != len(set(selected)):
        raise RuntimeError(
            "qwen3 model artifact allowlist differs from the runtime contract"
        )
    for relative in selected:
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != relative:
            raise RuntimeError("qwen3 model artifact path is unsafe")


def _model_cache_root(pin: dict[str, Any]) -> Path:
    return CACHE_ROOT / f"models--{pin['repo_id'].replace('/', '--')}"


def _snapshot_dir(pin: dict[str, Any]) -> Path:
    return _model_cache_root(pin) / "snapshots" / pin["revision"]


def _strip_downloader_state(pin: dict[str, Any]) -> None:
    """Remove only known Hugging Face downloader metadata from the runtime cache."""
    shutil.rmtree(CACHE_ROOT / ".locks", ignore_errors=True)
    shutil.rmtree(_model_cache_root(pin) / ".no_exist", ignore_errors=True)
    shutil.rmtree(_model_cache_root(pin) / "trees", ignore_errors=True)
    cachedir_tag = CACHE_ROOT / "CACHEDIR.TAG"
    try:
        is_expected_tag = (
            cachedir_tag.is_file()
            and not cachedir_tag.is_symlink()
            and cachedir_tag.read_text(encoding="utf-8")
            == EXPECTED_CACHEDIR_TAG_CONTENT
        )
    except (OSError, UnicodeError):
        is_expected_tag = False
    if is_expected_tag:
        cachedir_tag.unlink()


def _verify_snapshot(pin: dict[str, Any]) -> list[dict[str, int | str]]:
    model_cache = _model_cache_root(pin)
    snapshot = _snapshot_dir(pin)
    if CACHE_ROOT.is_symlink() or not CACHE_ROOT.is_dir():
        raise RuntimeError("qwen3 cache root is not a real directory")
    cache_entries = list(CACHE_ROOT.iterdir())
    if (
        cache_entries != [model_cache]
        or model_cache.is_symlink()
        or not model_cache.is_dir()
    ):
        raise RuntimeError(
            "qwen3 cache root must contain exactly the expected model directory"
        )
    if not snapshot.is_dir():
        raise RuntimeError("pinned qwen3 snapshot is absent from the cache")

    snapshots = model_cache / "snapshots"
    snapshot_entries = list(snapshots.iterdir()) if snapshots.is_dir() else []
    if (
        snapshots.is_symlink()
        or snapshot_entries != [snapshot]
        or snapshot.is_symlink()
        or not snapshot.is_dir()
    ):
        raise RuntimeError(
            "qwen3 cache must contain exactly the pinned snapshot revision"
        )

    actual = {
        path.relative_to(snapshot).as_posix()
        for path in snapshot.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual != EXPECTED_PATHS:
        raise RuntimeError(
            "qwen3 snapshot inventory mismatch: "
            f"missing={sorted(EXPECTED_PATHS - actual)!r} "
            f"extra={sorted(actual - EXPECTED_PATHS)!r}"
        )
    actual_directories = {
        path.relative_to(snapshot).as_posix()
        for path in snapshot.rglob("*")
        if path.is_dir() and not path.is_symlink()
    }
    if actual_directories != EXPECTED_SNAPSHOT_DIRECTORIES:
        raise RuntimeError("qwen3 snapshot directory inventory mismatch")
    if (model_cache / "refs").exists():
        raise RuntimeError("qwen3 cache must not contain a mutable branch ref")
    if {path.name for path in model_cache.iterdir()} != {"blobs", "snapshots"}:
        raise RuntimeError(
            "qwen3 cache contains state outside blobs and the exact snapshot"
        )

    blobs_dir = model_cache / "blobs"
    if not blobs_dir.is_dir() or any(
        not path.is_file() or path.is_symlink() for path in blobs_dir.iterdir()
    ):
        raise RuntimeError("qwen3 cache blob store has an unsupported shape")
    blob_files = {path.resolve(strict=True) for path in blobs_dir.iterdir()}

    expected = {item["path"]: item for item in pin["artifacts"]}
    cache_root = CACHE_ROOT.resolve()
    verified: list[dict[str, int | str]] = []
    referenced_blobs: set[Path] = set()
    for relative in sorted(EXPECTED_PATHS):
        path = snapshot / relative
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(cache_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(
                f"qwen3 cache path escapes its artifact root: {relative}"
            ) from exc
        referenced_blobs.add(resolved)
        size = path.stat().st_size
        digest = _sha256(path)
        if size != expected[relative]["size"]:
            raise RuntimeError(f"qwen3 model artifact size mismatch: {relative}")
        if digest != expected[relative]["sha256"]:
            raise RuntimeError(f"qwen3 model artifact digest mismatch: {relative}")
        verified.append({"path": relative, "sha256": digest, "size": size})
    if blob_files != referenced_blobs:
        raise RuntimeError(
            "qwen3 cache blob inventory differs from the four pinned artifacts"
        )
    return verified


def _functional_probe(pin: dict[str, Any]) -> None:
    """Prove the exact cached model loads and infers without network fallback."""
    from qwen3_embed import TextEmbedding

    model = TextEmbedding(
        model_name=pin["repo_id"],
        cache_dir=str(CACHE_ROOT),
        local_files_only=True,
    )
    vectors = list(model.embed(["Kaidera graph image build probe"], dim=768))
    if len(vectors) != 1 or tuple(vectors[0].shape) != (768,):
        raise RuntimeError("qwen3 offline probe returned an unexpected embedding shape")


def main() -> None:
    pin = _load_pin()
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=False)
    result = Path(
        snapshot_download(
            repo_id=pin["repo_id"],
            revision=pin["revision"],
            cache_dir=CACHE_ROOT,
            allow_patterns=[item["path"] for item in pin["artifacts"]],
        )
    )
    expected_snapshot = _snapshot_dir(pin)
    if result.resolve() != expected_snapshot.resolve():
        raise RuntimeError(
            "Hugging Face returned a snapshot outside the pinned cache path"
        )

    # Locks and negative-cache markers are downloader state, not runtime inputs. Exact
    # commit lookup does not need a refs/main file and local_files_only resolves the sole
    # snapshots/<sha> entry. Unknown model-cache entries still fail closed below.
    _strip_downloader_state(pin)
    files = _verify_snapshot(pin)
    _functional_probe(pin)
    _strip_downloader_state(pin)
    files = _verify_snapshot(pin)

    receipt = {
        "model": {
            "cache_dir": str(CACHE_ROOT),
            "files": files,
            "license": pin["license"],
            "repo_id": pin["repo_id"],
            "revision": pin["revision"],
        },
        "schema_version": 1,
    }
    (ARTIFACT_ROOT / RECEIPT_NAME).write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    shutil.copyfile(PINS_PATH, ARTIFACT_ROOT / PIN_COPY_NAME)


if __name__ == "__main__":
    main()
