from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


FETCHER_PATH = Path(__file__).resolve().parents[1] / "fetch-models.py"


def load_fetcher(name: str):
    spec = importlib.util.spec_from_file_location(name, FETCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pin(repo_id: str, revision: str, weights: bytes) -> dict:
    return {
        "artifacts": [
            {
                "path": "config.json",
                "sha256": _sha256(
                    b"embed-config" if repo_id.endswith("embed") else b"rerank-config"
                ),
                "size": len(
                    b"embed-config" if repo_id.endswith("embed") else b"rerank-config"
                ),
            },
            {
                "path": "model.safetensors",
                "sha256": _sha256(weights),
                "size": len(weights),
            },
        ],
        "license": "Apache-2.0",
        "repo_id": repo_id,
        "revision": revision,
    }


def test_fetcher_verifies_inventory_and_writes_every_file_hash(monkeypatch, tmp_path):
    module = load_fetcher("local_search_model_fetch_receipt_test")
    model_bytes = {
        "embedding": {"config.json": b"embed-config", "model.safetensors": b"embed"},
        "rerank": {"config.json": b"rerank-config", "model.safetensors": b"rerank"},
    }
    pins = {
        "schema_version": 1,
        "models": {
            "embedding": _pin("owner/embed", "a" * 40, b"embed"),
            "rerank": _pin("owner/rerank", "b" * 40, b"rerank"),
        },
    }
    pins_path = tmp_path / "model-pins.json"
    pins_path.write_text(json.dumps(pins), encoding="utf-8")
    models_root = tmp_path / "models"
    calls = []

    def snapshot_download(*, repo_id, revision, local_dir, allow_patterns):
        role = Path(local_dir).name
        calls.append((repo_id, revision, tuple(allow_patterns)))
        for relative, content in model_bytes[role].items():
            destination = Path(local_dir) / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        cache = Path(local_dir) / ".cache"
        cache.mkdir()
        (cache / "mutable-metadata").write_text("not-runtime-input", encoding="utf-8")

    monkeypatch.setattr(module, "PINS_PATH", pins_path)
    monkeypatch.setattr(module, "MODELS_ROOT", models_root)
    monkeypatch.setattr(module, "snapshot_download", snapshot_download)
    monkeypatch.setattr(module, "_functional_probe", lambda: None)

    module.main()

    assert calls == [
        ("owner/embed", "a" * 40, ("config.json", "model.safetensors")),
        ("owner/rerank", "b" * 40, ("config.json", "model.safetensors")),
    ]
    assert not list(models_root.rglob(".cache"))
    receipt = json.loads(
        (models_root / "kaidera-model-receipt.json").read_text(encoding="utf-8")
    )
    for role, files in model_bytes.items():
        assert receipt["models"][role]["files"] == [
            {"path": relative, "sha256": _sha256(content), "size": len(content)}
            for relative, content in sorted(files.items())
        ]
    assert json.loads((models_root / "model-pins.json").read_text()) == pins
    assert "generated_at" not in receipt, "a timestamp would make the image non-reproducible"


def test_fetcher_rejects_any_unpinned_extra_file(monkeypatch, tmp_path):
    module = load_fetcher("local_search_model_fetch_extra_file_test")
    weights = b"weights"
    pin = _pin("owner/embed", "a" * 40, weights)
    models_root = tmp_path / "models"

    def snapshot_download(*, local_dir, **_kwargs):
        destination = Path(local_dir)
        destination.mkdir(parents=True)
        (destination / "config.json").write_bytes(b"config")
        (destination / "model.safetensors").write_bytes(weights)
        (destination / "unreviewed.txt").write_bytes(b"extra")

    monkeypatch.setattr(module, "MODELS_ROOT", models_root)
    monkeypatch.setattr(module, "snapshot_download", snapshot_download)

    models_root.mkdir()
    with pytest.raises(RuntimeError, match="inventory mismatch"):
        module._fetch_model("embedding", pin)


@pytest.mark.parametrize("unsafe_path", ["pytorch_model.bin", "weights.pt", "model.py"])
def test_fetcher_rejects_every_non_safetensors_weight_format(
    monkeypatch, tmp_path, unsafe_path
):
    module = load_fetcher("local_search_model_fetch_unsafe_format_test")
    pin = _pin("owner/embed", "a" * 40, b"weights")
    pin["artifacts"].append(
        {"path": unsafe_path, "sha256": "0" * 64, "size": 1}
    )
    monkeypatch.setattr(module, "MODELS_ROOT", tmp_path / "models")
    monkeypatch.setattr(
        module,
        "snapshot_download",
        lambda **_kwargs: pytest.fail("an unsafe pin must fail before network access"),
    )

    with pytest.raises(RuntimeError, match="unsafe or unnecessary"):
        module._fetch_model("embedding", pin)
