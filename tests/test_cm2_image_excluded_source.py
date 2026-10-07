"""Excluded runtime material must refuse before any source-content reader."""
import pytest

from test_cm2_image_source_payloads import inputs
from test_linux_builder_contract import load


def test_literal_excluded_secrets_directory_refuses_before_payload_hashing(tmp_path, monkeypatch):
    module = load('linux_build_catalog.py', monkeypatch)
    root = inputs(tmp_path)
    directory = root / 'src/secrets'
    directory.mkdir()
    file = directory / 'PUBLIC-not-a-release-input'
    public = b'PUBLIC synthetic excluded bytes; no credential\n'
    file.write_bytes(public)
    before = file.lstat()
    actual = module.payload_inventory
    reads = []

    def reader(*args, **kwargs):
        reads.append(True)
        return actual(*args, **kwargs)

    monkeypatch.setattr(module, 'payload_inventory', reader)
    with pytest.raises(RuntimeError):
        module.source_payloads(root)
    assert reads == []
    assert file.read_bytes() == public and file.lstat().st_ino == before.st_ino
