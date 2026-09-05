#!/usr/bin/env python3
"""Fetch the exact safe model artifacts used by the local-search worker.

This script runs only in the image's model-fetch stage.  The final image receives
the verified snapshots and a deterministic receipt, not this downloader or a
mutable Hugging Face cache.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import shutil
from typing import Any

from huggingface_hub import snapshot_download


PINS_PATH = Path("/build/model-pins.json")
MODELS_ROOT = Path("/opt/kaidera-models")
RECEIPT_NAME = "kaidera-model-receipt.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_pins() -> dict[str, Any]:
    pins = json.loads(PINS_PATH.read_text(encoding="utf-8"))
    if pins.get("schema_version") != 1 or set(pins.get("models") or {}) != {
        "embedding",
        "rerank",
    }:
        raise RuntimeError("model-pins.json has an unsupported shape")
    return pins


def _fetch_model(role: str, pin: dict[str, Any]) -> dict[str, Any]:
    artifacts = pin.get("artifacts")
    revision = pin.get("revision")
    repo_id = pin.get("repo_id")
    if (
        not isinstance(artifacts, list)
        or not artifacts
        or not all(
            isinstance(item, dict)
            and set(item) == {"path", "sha256", "size"}
            and isinstance(item["path"], str)
            and item["path"]
            and isinstance(item["sha256"], str)
            and len(item["sha256"]) == 64
            and all(char in "0123456789abcdef" for char in item["sha256"])
            and isinstance(item["size"], int)
            and item["size"] > 0
            for item in artifacts
        )
        or not isinstance(repo_id, str)
        or not repo_id
        or not isinstance(revision, str)
        or len(revision) != 40
        or any(char not in "0123456789abcdef" for char in revision)
    ):
        raise RuntimeError(f"invalid immutable model pin for {role}")
    files = [item["path"] for item in artifacts]
    if len(files) != len(set(files)):
        raise RuntimeError(f"duplicate model artifact path selected for {role}")
    for item in files:
        path = PurePosixPath(item)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != item:
            raise RuntimeError(f"unsafe model artifact path selected for {role}")
    if any(PurePosixPath(item).suffix not in {".json", ".safetensors", ".txt"} for item in files):
        raise RuntimeError(f"unsafe or unnecessary model artifact selected for {role}")
    if sum(item.endswith(".safetensors") for item in files) != 1:
        raise RuntimeError(f"exactly one safetensors weight artifact is required for {role}")

    destination = MODELS_ROOT / role
    snapshot_download(
        repo_id=repo_id,
        revision=revision,
        local_dir=destination,
        allow_patterns=files,
    )
    shutil.rmtree(destination / ".cache", ignore_errors=True)

    return _verify_model(role, pin)


def _verify_model(role: str, pin: dict[str, Any]) -> dict[str, Any]:
    artifacts = pin["artifacts"]
    repo_id = pin["repo_id"]
    revision = pin["revision"]
    destination = MODELS_ROOT / role
    files = [item["path"] for item in artifacts]

    actual = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    expected = set(files)
    if actual != expected:
        raise RuntimeError(
            f"model artifact inventory mismatch for {role}: "
            f"missing={sorted(expected - actual)!r} extra={sorted(actual - expected)!r}"
        )
    symlinks = [relative for relative in expected if (destination / relative).is_symlink()]
    if symlinks:
        raise RuntimeError(f"model artifacts may not be symlinks for {role}: {symlinks!r}")

    expected_artifacts = {item["path"]: item for item in artifacts}
    verified_artifacts: list[dict[str, Any]] = []
    for relative in sorted(expected):
        path = destination / relative
        actual_size = path.stat().st_size
        actual_sha256 = _sha256(path)
        expected_artifact = expected_artifacts[relative]
        if actual_size != expected_artifact["size"]:
            raise RuntimeError(f"model artifact size mismatch for {role}/{relative}")
        if actual_sha256 != expected_artifact["sha256"]:
            raise RuntimeError(f"model artifact digest mismatch for {role}/{relative}")
        verified_artifacts.append(
            {"path": relative, "sha256": actual_sha256, "size": actual_size}
        )

    return {
        "files": verified_artifacts,
        "license": pin["license"],
        "repo_id": repo_id,
        "revision": revision,
    }


def _functional_probe() -> None:
    """Prove both exact snapshots load and infer before the image can be built."""
    from sentence_transformers import CrossEncoder, SentenceTransformer

    embedding = SentenceTransformer(
        str(MODELS_ROOT / "embedding"),
        trust_remote_code=False,
        local_files_only=True,
    )
    vectors = embedding.encode(
        ["Kaidera local-search image build probe"],
        batch_size=1,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    if tuple(vectors.shape) != (1, 768):
        raise RuntimeError(f"embedding model probe returned shape {vectors.shape!r}")

    reranker = CrossEncoder(
        str(MODELS_ROOT / "rerank"),
        max_length=512,
        trust_remote_code=False,
        local_files_only=True,
    )
    scores = reranker.predict(
        [("secure appliance restore", "restore uses an authenticated snapshot")],
        batch_size=1,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    if tuple(scores.shape) not in {(1,), (1, 1)}:
        raise RuntimeError(f"rerank model probe returned shape {scores.shape!r}")
    score = float(scores.reshape(-1)[0])
    if not math.isfinite(score):
        raise RuntimeError("rerank model probe returned a non-finite score")


def main() -> None:
    pins = _load_pins()
    MODELS_ROOT.mkdir(parents=True, exist_ok=False)
    for role, pin in sorted(pins["models"].items()):
        _fetch_model(role, pin)
    _functional_probe()
    receipt = {
        "models": {
            role: _verify_model(role, pin)
            for role, pin in sorted(pins["models"].items())
        },
        "schema_version": 1,
    }
    (MODELS_ROOT / RECEIPT_NAME).write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    shutil.copyfile(PINS_PATH, MODELS_ROOT / "model-pins.json")


if __name__ == "__main__":
    main()
