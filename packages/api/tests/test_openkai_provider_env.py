from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def reader(monkeypatch):
    try:
        module = importlib.import_module("openkai_provider_env")
    except ModuleNotFoundError:
        path = Path(__file__).resolve().parents[1] / "openkai_provider_env.py"
        spec = importlib.util.spec_from_file_location("openkai_provider_env", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    monkeypatch.setenv(
        "OPENKAI_PROVIDER_REGISTRY_FILE",
        str(
            Path(__file__).resolve().parents[3]
            / "redistributable"
            / "config"
            / "openkai-providers.json"
        ),
    )
    return module


def _write_projection(
    reader,
    root: Path,
    data: bytes,
    *,
    revision: str = "a" * 64,
) -> Path:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    env_file = root / reader.PROJECTION_ENV_NAME
    env_file.write_bytes(data)
    env_file.chmod(0o600)
    manifest = {
        "schema_version": reader.PROJECTION_SCHEMA,
        "registry_sha256": reader.REGISTRY_SHA256,
        "authority_existed": True,
        "authority_revision": revision,
        "projection_sha256": hashlib.sha256(data).hexdigest(),
    }
    manifest_file = root / reader.PROJECTION_MANIFEST_NAME
    manifest_file.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    )
    manifest_file.chmod(0o600)
    return root


def test_reads_only_canonical_projection_and_ignores_process_env(
    reader, monkeypatch, tmp_path
):
    secret = "sk-never-return-outside-callsite"
    projection = _write_projection(
        reader,
        tmp_path / "projection",
        f"OPENROUTER_API_KEY={secret}\n".encode(),
        revision="b" * 64,
    )
    monkeypatch.setenv("OPENKAI_PROVIDER_PROJECTION_DIR", str(projection))
    monkeypatch.setenv("OPENAI_API_KEY", "stale-process-key")
    monkeypatch.setenv("OPENKAI_PROVIDER", "stale-process-route")

    resolved, revision = reader.load_provider_projection()

    assert resolved == {"openrouter": secret}
    assert revision == "b" * 64
    assert "openai" not in resolved


def test_missing_projection_fails_closed_without_process_env_fallback(
    reader, monkeypatch
):
    monkeypatch.delenv("OPENKAI_PROVIDER_PROJECTION_DIR", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-be-read")

    with pytest.raises(reader.OpenKaiProviderEnvError, match="unavailable"):
        reader.load_provider_keys()


def test_rootful_container_uid_mismatch_is_accepted_for_read_only_mount(
    reader, monkeypatch, tmp_path
):
    projection = _write_projection(
        reader,
        tmp_path / "projection",
        b"OPENAI_API_KEY=host-owner-secret\n",
    )
    monkeypatch.setenv("OPENKAI_PROVIDER_PROJECTION_DIR", str(projection))
    real_fstat = reader.os.fstat

    def foreign_owner(fd):
        metadata = list(real_fstat(fd))
        metadata[4] = metadata[4] + 1000
        return os.stat_result(metadata)

    monkeypatch.setattr(reader.os, "fstat", foreign_owner)

    assert reader.load_provider_keys() == {"openai": "host-owner-secret"}


def test_pending_drift_extra_file_and_noncanonical_key_fail_closed(
    reader, monkeypatch, tmp_path
):
    projection = _write_projection(
        reader,
        tmp_path / "projection",
        b"OPENROUTER_API_KEY=secret\n",
    )
    monkeypatch.setenv("OPENKAI_PROVIDER_PROJECTION_DIR", str(projection))

    pending = projection / reader.PROJECTION_PENDING_NAME
    pending.write_text("{}\n")
    pending.chmod(0o600)
    with pytest.raises(reader.OpenKaiProviderEnvError, match="incomplete"):
        reader.load_provider_keys()
    pending.unlink()

    (projection / reader.PROJECTION_ENV_NAME).write_text(
        "OPENROUTER_API_KEY=changed-after-manifest\n"
    )
    with pytest.raises(reader.OpenKaiProviderEnvError, match="manifest mismatch"):
        reader.load_provider_keys()

    projection = tmp_path / "projection-noncanonical"
    _write_projection(reader, projection, b"ANTHROPIC_AUTH_TOKEN=oauth-alias\n")
    monkeypatch.setenv("OPENKAI_PROVIDER_PROJECTION_DIR", str(projection))
    with pytest.raises(reader.OpenKaiProviderEnvError, match="noncanonical"):
        reader.load_provider_keys()

    projection = tmp_path / "projection-extra"
    _write_projection(reader, projection, b"OPENAI_API_KEY=secret\n")
    (projection / "auth.json").write_text("must-not-be-visible")
    (projection / "auth.json").chmod(0o600)
    monkeypatch.setenv("OPENKAI_PROVIDER_PROJECTION_DIR", str(projection))
    with pytest.raises(reader.OpenKaiProviderEnvError, match="extra files"):
        reader.load_provider_keys()


def test_modes_symlink_duplicate_and_tampered_registry_fail_closed(
    reader, monkeypatch, tmp_path
):
    projection = _write_projection(
        reader,
        tmp_path / "projection",
        b"OPENAI_API_KEY=secret\n",
    )
    monkeypatch.setenv("OPENKAI_PROVIDER_PROJECTION_DIR", str(projection))
    env_file = projection / reader.PROJECTION_ENV_NAME
    env_file.chmod(0o644)
    with pytest.raises(reader.OpenKaiProviderEnvError, match="permissions"):
        reader.load_provider_keys()

    env_file.chmod(0o600)
    real_env = tmp_path / "real-provider.env"
    real_env.write_bytes(env_file.read_bytes())
    real_env.chmod(0o600)
    env_file.unlink()
    env_file.symlink_to(real_env)
    with pytest.raises(reader.OpenKaiProviderEnvError, match="unsafe"):
        reader.load_provider_keys()

    projection = tmp_path / "projection-duplicate"
    _write_projection(
        reader,
        projection,
        b"OPENAI_API_KEY=one\nOPENAI_API_KEY=two\n",
    )
    monkeypatch.setenv("OPENKAI_PROVIDER_PROJECTION_DIR", str(projection))
    with pytest.raises(reader.OpenKaiProviderEnvError, match="duplicate"):
        reader.load_provider_keys()

    registry = tmp_path / "providers.json"
    source = Path(os.environ["OPENKAI_PROVIDER_REGISTRY_FILE"])
    registry.write_bytes(source.read_bytes() + b" ")
    monkeypatch.setenv("OPENKAI_PROVIDER_REGISTRY_FILE", str(registry))
    with pytest.raises(reader.OpenKaiProviderEnvError, match="hash mismatch"):
        reader.load_provider_keys()



def test_quoted_secret_decodes_exactly_and_unbalanced_quote_fails_closed(
    reader, monkeypatch, tmp_path
):
    """A quoted projection value must survive byte-for-byte; a stray quote must
    fail closed, never resolve corrupted.

    Fixed-semantics grammar: one optional matching quote layer, no escape
    sequences, so backslashes inside the quotes are DATA. An unbalanced quote
    is a hard error: silently accepting it would hand a corrupted secret to a
    provider call.
    """
    bs = chr(92)
    line = 'OPENROUTER_API_KEY="a b' + bs + 'c' + bs + bs + 'd"' + chr(10)
    projection = _write_projection(
        reader, tmp_path / "projection", line.encode("utf-8")
    )
    monkeypatch.setenv("OPENKAI_PROVIDER_PROJECTION_DIR", str(projection))

    resolved, _revision = reader.load_provider_projection()
    assert resolved["openrouter"] == 'a b' + bs + 'c' + bs + bs + 'd'

    unbalanced = _write_projection(
        reader,
        tmp_path / "projection-unbalanced",
        b'OPENROUTER_API_KEY="only-opening' + chr(10).encode(),
    )
    monkeypatch.setenv("OPENKAI_PROVIDER_PROJECTION_DIR", str(unbalanced))
    with pytest.raises(reader.OpenKaiProviderEnvError, match="grammar"):
        reader.load_provider_projection()
