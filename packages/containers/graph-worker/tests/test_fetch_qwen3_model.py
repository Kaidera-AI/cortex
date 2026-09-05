from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil

import pytest


FETCHER_PATH = Path(__file__).resolve().parents[1] / "fetch-qwen3-model.py"
VERIFIER_PATH = Path(__file__).resolve().parents[1] / "verify-qwen3-model.py"
REPO_ID = "n24q02m/Qwen3-Embedding-0.6B-ONNX"
REVISION = "dc873d64d6143f27ad68dadbd1f0d9a4371b994e"
CACHEDIR_TAG_CONTENT = (
    "Signature: 8a477f597d28d172789f06886806bc55\n"
    "# This file is a cache directory tag created by huggingface_hub.\n"
    "# For information about cache directory tags, see:\n"
    "#\thttps://bford.info/cachedir/\n"
)


def _load_fetcher(name: str):
    spec = importlib.util.spec_from_file_location(name, FETCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_verifier(name: str):
    spec = importlib.util.spec_from_file_location(name, VERIFIER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _model_files() -> dict[str, bytes]:
    return {
        "config.json": b"config",
        "onnx/model_quantized.onnx": b"onnx-weights",
        "tokenizer.json": b"tokenizer",
        "tokenizer_config.json": b"tokenizer-config",
    }


def _pins(files: dict[str, bytes] | None = None) -> dict:
    selected = files or _model_files()
    return {
        "schema_version": 1,
        "model": {
            "artifacts": [
                {"path": path, "sha256": _sha256(content), "size": len(content)}
                for path, content in selected.items()
            ],
            "license": "Apache-2.0",
            "repo_id": REPO_ID,
            "revision": REVISION,
        },
    }


def _write_hf_cache(cache_dir: Path, files: dict[str, bytes]) -> Path:
    model_root = cache_dir / "models--n24q02m--Qwen3-Embedding-0.6B-ONNX"
    blobs = model_root / "blobs"
    snapshot = model_root / "snapshots" / REVISION
    blobs.mkdir(parents=True)
    snapshot.mkdir(parents=True)
    for relative, content in files.items():
        blob = blobs / _sha256(content)
        blob.write_bytes(content)
        destination = snapshot / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(Path(os.path.relpath(blob, destination.parent)))
    locks = cache_dir / ".locks"
    locks.mkdir()
    (locks / "build.lock").write_text("transient", encoding="utf-8")
    (cache_dir / "CACHEDIR.TAG").write_text(CACHEDIR_TAG_CONTENT, encoding="utf-8")
    tree_cache = model_root / "trees"
    tree_cache.mkdir()
    (tree_cache / f"{REVISION}.json").write_text("{}", encoding="utf-8")
    negative_cache = model_root / ".no_exist" / REVISION
    negative_cache.mkdir(parents=True)
    (negative_cache / "README.md").write_text("transient", encoding="utf-8")
    return snapshot


def _write_extra_cache_root_entry(cache_root: Path, kind: str) -> None:
    extra = cache_root / "unexpected-root-state"
    if kind == "file":
        extra.write_text("unreviewed", encoding="utf-8")
    elif kind == "directory":
        extra.mkdir()
    else:
        target = cache_root.parent / "outside-cache"
        target.mkdir()
        extra.symlink_to(target, target_is_directory=True)


def _write_extra_snapshot_state(snapshot: Path, kind: str) -> None:
    if kind == "file":
        (snapshot.parent / "unexpected-revision-file").write_text(
            "unreviewed", encoding="utf-8"
        )
    elif kind == "broken-symlink":
        (snapshot.parent / "unexpected-revision-link").symlink_to(
            snapshot.parent / "missing-revision",
            target_is_directory=True,
        )
    elif kind == "nested-broken-symlink":
        (snapshot / "unexpected-broken-link").symlink_to(snapshot / "missing-artifact")
    else:
        (snapshot / "unexpected-empty-directory").mkdir()


def test_fetcher_builds_exact_offline_cache_and_deterministic_receipt(
    monkeypatch, tmp_path
):
    module = _load_fetcher("qwen3_model_receipt_test")
    files = _model_files()
    pins = _pins(files)
    pins_path = tmp_path / "qwen3-model-pins.json"
    pins_path.write_text(json.dumps(pins), encoding="utf-8")
    artifact_root = tmp_path / "artifact"
    cache_root = artifact_root / "cache"
    calls: list[tuple[str, str, tuple[str, ...]]] = []

    def snapshot_download(*, repo_id, revision, cache_dir, allow_patterns):
        calls.append((repo_id, revision, tuple(allow_patterns)))
        return str(_write_hf_cache(Path(cache_dir), files))

    monkeypatch.setattr(module, "PINS_PATH", pins_path)
    monkeypatch.setattr(module, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(module, "CACHE_ROOT", cache_root)
    monkeypatch.setattr(module, "snapshot_download", snapshot_download)
    monkeypatch.setattr(module, "_functional_probe", lambda _pin: None)

    module.main()

    assert calls == [(REPO_ID, REVISION, tuple(files))]
    assert not (cache_root / ".locks").exists()
    assert not (cache_root / "CACHEDIR.TAG").exists()
    assert not (
        cache_root / "models--n24q02m--Qwen3-Embedding-0.6B-ONNX" / ".no_exist"
    ).exists()
    assert not (
        cache_root / "models--n24q02m--Qwen3-Embedding-0.6B-ONNX" / "trees"
    ).exists()
    assert not (cache_root / "models--n24q02m--Qwen3-Embedding-0.6B-ONNX/refs").exists()
    receipt = json.loads(
        (artifact_root / "kaidera-qwen3-model-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["model"]["files"] == [
        {"path": path, "sha256": _sha256(files[path]), "size": len(files[path])}
        for path in sorted(files)
    ]
    assert receipt["model"]["cache_dir"] == str(cache_root)
    assert "generated_at" not in receipt
    assert json.loads((artifact_root / "qwen3-model-pins.json").read_text()) == pins

    verifier = _load_verifier("qwen3_runtime_receipt_test")
    monkeypatch.setattr(verifier, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(verifier, "CACHE_ROOT", cache_root)
    monkeypatch.setattr(verifier, "PINS_PATH", artifact_root / "qwen3-model-pins.json")
    monkeypatch.setattr(
        verifier,
        "RECEIPT_PATH",
        artifact_root / "kaidera-qwen3-model-receipt.json",
    )
    verifier.verify()

    target = (
        cache_root
        / "models--n24q02m--Qwen3-Embedding-0.6B-ONNX"
        / "blobs"
        / _sha256(files["config.json"])
    )
    target.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="does not match its pin"):
        verifier.verify()


def test_fetcher_rejects_any_unpinned_snapshot_file(monkeypatch, tmp_path):
    module = _load_fetcher("qwen3_model_extra_file_test")
    files = _model_files()
    pins = _pins(files)
    pins_path = tmp_path / "qwen3-model-pins.json"
    pins_path.write_text(json.dumps(pins), encoding="utf-8")
    artifact_root = tmp_path / "artifact"
    cache_root = artifact_root / "cache"

    def snapshot_download(**kwargs):
        snapshot = _write_hf_cache(Path(kwargs["cache_dir"]), files)
        (snapshot / "unreviewed.json").write_text("{}", encoding="utf-8")
        return str(snapshot)

    monkeypatch.setattr(module, "PINS_PATH", pins_path)
    monkeypatch.setattr(module, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(module, "CACHE_ROOT", cache_root)
    monkeypatch.setattr(module, "snapshot_download", snapshot_download)

    with pytest.raises(RuntimeError, match="inventory mismatch"):
        module.main()


def test_fetcher_rejects_an_unreferenced_cache_blob(monkeypatch, tmp_path):
    module = _load_fetcher("qwen3_model_extra_blob_test")
    files = _model_files()
    pins = _pins(files)
    pins_path = tmp_path / "qwen3-model-pins.json"
    pins_path.write_text(json.dumps(pins), encoding="utf-8")
    artifact_root = tmp_path / "artifact"
    cache_root = artifact_root / "cache"

    def snapshot_download(**kwargs):
        snapshot = _write_hf_cache(Path(kwargs["cache_dir"]), files)
        (snapshot.parents[1] / "blobs" / "unreferenced").write_text(
            "payload", encoding="utf-8"
        )
        return str(snapshot)

    monkeypatch.setattr(module, "PINS_PATH", pins_path)
    monkeypatch.setattr(module, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(module, "CACHE_ROOT", cache_root)
    monkeypatch.setattr(module, "snapshot_download", snapshot_download)

    with pytest.raises(RuntimeError, match="blob inventory"):
        module.main()


def test_fetcher_rejects_unknown_model_cache_state(monkeypatch, tmp_path):
    module = _load_fetcher("qwen3_model_unknown_cache_state_test")
    files = _model_files()
    pins = _pins(files)
    pins_path = tmp_path / "qwen3-model-pins.json"
    pins_path.write_text(json.dumps(pins), encoding="utf-8")
    artifact_root = tmp_path / "artifact"
    cache_root = artifact_root / "cache"

    def snapshot_download(**kwargs):
        snapshot = _write_hf_cache(Path(kwargs["cache_dir"]), files)
        (snapshot.parents[1] / "unexpected-state").mkdir()
        return str(snapshot)

    monkeypatch.setattr(module, "PINS_PATH", pins_path)
    monkeypatch.setattr(module, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(module, "CACHE_ROOT", cache_root)
    monkeypatch.setattr(module, "snapshot_download", snapshot_download)

    with pytest.raises(RuntimeError, match="state outside"):
        module.main()


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_fetcher_rejects_unrecognised_tree_cache_state(monkeypatch, tmp_path, kind):
    module = _load_fetcher(f"qwen3_model_unrecognised_tree_cache_{kind}_test")
    files = _model_files()
    pins_path = tmp_path / "qwen3-model-pins.json"
    pins_path.write_text(json.dumps(_pins(files)), encoding="utf-8")
    artifact_root = tmp_path / "artifact"
    cache_root = artifact_root / "cache"

    def snapshot_download(**kwargs):
        snapshot = _write_hf_cache(Path(kwargs["cache_dir"]), files)
        trees = snapshot.parents[1] / "trees"
        shutil.rmtree(trees)
        if kind == "file":
            trees.write_text("unreviewed", encoding="utf-8")
        else:
            target = cache_root.parent / "outside-tree-cache"
            target.mkdir()
            trees.symlink_to(target, target_is_directory=True)
        return str(snapshot)

    monkeypatch.setattr(module, "PINS_PATH", pins_path)
    monkeypatch.setattr(module, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(module, "CACHE_ROOT", cache_root)
    monkeypatch.setattr(module, "snapshot_download", snapshot_download)

    with pytest.raises(RuntimeError, match="state outside"):
        module.main()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_fetcher_rejects_unknown_cache_root_state(monkeypatch, tmp_path, kind):
    module = _load_fetcher(f"qwen3_model_unknown_cache_root_{kind}_test")
    files = _model_files()
    pins_path = tmp_path / "qwen3-model-pins.json"
    pins_path.write_text(json.dumps(_pins(files)), encoding="utf-8")
    artifact_root = tmp_path / "artifact"
    cache_root = artifact_root / "cache"

    def snapshot_download(**kwargs):
        snapshot = _write_hf_cache(Path(kwargs["cache_dir"]), files)
        _write_extra_cache_root_entry(cache_root, kind)
        return str(snapshot)

    monkeypatch.setattr(module, "PINS_PATH", pins_path)
    monkeypatch.setattr(module, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(module, "CACHE_ROOT", cache_root)
    monkeypatch.setattr(module, "snapshot_download", snapshot_download)

    with pytest.raises(RuntimeError, match="cache root must contain exactly"):
        module.main()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_fetcher_rejects_unrecognised_cachedir_tag(monkeypatch, tmp_path, kind):
    module = _load_fetcher(f"qwen3_model_unrecognised_cachedir_tag_{kind}_test")
    files = _model_files()
    pins_path = tmp_path / "qwen3-model-pins.json"
    pins_path.write_text(json.dumps(_pins(files)), encoding="utf-8")
    artifact_root = tmp_path / "artifact"
    cache_root = artifact_root / "cache"

    def snapshot_download(**kwargs):
        snapshot = _write_hf_cache(Path(kwargs["cache_dir"]), files)
        tag = cache_root / "CACHEDIR.TAG"
        tag.unlink()
        if kind == "file":
            tag.write_text("unreviewed", encoding="utf-8")
        elif kind == "directory":
            tag.mkdir()
        else:
            target = cache_root.parent / "outside-cache-tag"
            target.write_text(CACHEDIR_TAG_CONTENT, encoding="utf-8")
            tag.symlink_to(target)
        return str(snapshot)

    monkeypatch.setattr(module, "PINS_PATH", pins_path)
    monkeypatch.setattr(module, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(module, "CACHE_ROOT", cache_root)
    monkeypatch.setattr(module, "snapshot_download", snapshot_download)

    with pytest.raises(RuntimeError, match="cache root must contain exactly"):
        module.main()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_runtime_verifier_rejects_unknown_cache_root_state(monkeypatch, tmp_path, kind):
    fetcher = _load_fetcher(f"qwen3_runtime_cache_root_fetch_{kind}_test")
    files = _model_files()
    pins_path = tmp_path / "qwen3-model-pins.json"
    pins_path.write_text(json.dumps(_pins(files)), encoding="utf-8")
    artifact_root = tmp_path / "artifact"
    cache_root = artifact_root / "cache"

    monkeypatch.setattr(fetcher, "PINS_PATH", pins_path)
    monkeypatch.setattr(fetcher, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(fetcher, "CACHE_ROOT", cache_root)
    monkeypatch.setattr(
        fetcher,
        "snapshot_download",
        lambda **kwargs: str(_write_hf_cache(Path(kwargs["cache_dir"]), files)),
    )
    monkeypatch.setattr(fetcher, "_functional_probe", lambda _pin: None)
    fetcher.main()

    verifier = _load_verifier(f"qwen3_runtime_cache_root_verify_{kind}_test")
    monkeypatch.setattr(verifier, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(verifier, "CACHE_ROOT", cache_root)
    monkeypatch.setattr(verifier, "PINS_PATH", artifact_root / "qwen3-model-pins.json")
    monkeypatch.setattr(
        verifier,
        "RECEIPT_PATH",
        artifact_root / "kaidera-qwen3-model-receipt.json",
    )
    _write_extra_cache_root_entry(cache_root, kind)

    with pytest.raises(RuntimeError, match="cache root must contain exactly"):
        verifier.verify()


@pytest.mark.parametrize(
    "kind", ["file", "broken-symlink", "nested-broken-symlink", "empty-directory"]
)
def test_fetcher_rejects_unknown_snapshot_state(monkeypatch, tmp_path, kind):
    module = _load_fetcher(f"qwen3_model_unknown_snapshot_{kind}_test")
    files = _model_files()
    pins_path = tmp_path / "qwen3-model-pins.json"
    pins_path.write_text(json.dumps(_pins(files)), encoding="utf-8")
    artifact_root = tmp_path / "artifact"
    cache_root = artifact_root / "cache"

    def snapshot_download(**kwargs):
        snapshot = _write_hf_cache(Path(kwargs["cache_dir"]), files)
        _write_extra_snapshot_state(snapshot, kind)
        return str(snapshot)

    monkeypatch.setattr(module, "PINS_PATH", pins_path)
    monkeypatch.setattr(module, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(module, "CACHE_ROOT", cache_root)
    monkeypatch.setattr(module, "snapshot_download", snapshot_download)

    with pytest.raises(
        RuntimeError, match="pinned snapshot|snapshot inventory|directory inventory"
    ):
        module.main()


@pytest.mark.parametrize(
    "kind", ["file", "broken-symlink", "nested-broken-symlink", "empty-directory"]
)
def test_runtime_verifier_rejects_unknown_snapshot_state(monkeypatch, tmp_path, kind):
    fetcher = _load_fetcher(f"qwen3_runtime_snapshot_fetch_{kind}_test")
    files = _model_files()
    pins_path = tmp_path / "qwen3-model-pins.json"
    pins_path.write_text(json.dumps(_pins(files)), encoding="utf-8")
    artifact_root = tmp_path / "artifact"
    cache_root = artifact_root / "cache"

    monkeypatch.setattr(fetcher, "PINS_PATH", pins_path)
    monkeypatch.setattr(fetcher, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(fetcher, "CACHE_ROOT", cache_root)
    monkeypatch.setattr(
        fetcher,
        "snapshot_download",
        lambda **kwargs: str(_write_hf_cache(Path(kwargs["cache_dir"]), files)),
    )
    monkeypatch.setattr(fetcher, "_functional_probe", lambda _pin: None)
    fetcher.main()

    verifier = _load_verifier(f"qwen3_runtime_snapshot_verify_{kind}_test")
    monkeypatch.setattr(verifier, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(verifier, "CACHE_ROOT", cache_root)
    monkeypatch.setattr(verifier, "PINS_PATH", artifact_root / "qwen3-model-pins.json")
    monkeypatch.setattr(
        verifier,
        "RECEIPT_PATH",
        artifact_root / "kaidera-qwen3-model-receipt.json",
    )
    snapshot = (
        cache_root
        / "models--n24q02m--Qwen3-Embedding-0.6B-ONNX"
        / "snapshots"
        / REVISION
    )
    _write_extra_snapshot_state(snapshot, kind)

    with pytest.raises(
        RuntimeError, match="reviewed revision|snapshot inventory|directory inventory"
    ):
        verifier.verify()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("repo_id", "attacker/model", "unexpected repository"),
        ("revision", "main", "reviewed exact commit"),
        ("license", "unknown", "unexpected licence"),
    ],
)
def test_fetcher_rejects_mutable_or_unexpected_model_identity(
    monkeypatch, tmp_path, field, value, message
):
    module = _load_fetcher(f"qwen3_model_bad_{field}_test")
    pins = _pins()
    pins["model"][field] = value
    pins_path = tmp_path / "qwen3-model-pins.json"
    pins_path.write_text(json.dumps(pins), encoding="utf-8")
    monkeypatch.setattr(module, "PINS_PATH", pins_path)
    monkeypatch.setattr(
        module,
        "snapshot_download",
        lambda **_kwargs: pytest.fail("invalid pins must fail before network access"),
    )

    with pytest.raises(RuntimeError, match=message):
        module._load_pin()
