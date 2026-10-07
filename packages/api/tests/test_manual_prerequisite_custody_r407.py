"""Publication/material custody beyond the immutable 26-case writer contract."""
import json
import os
import stat
from uuid import UUID

import pytest

from test_manual_prerequisite_r407 import required, seed


@pytest.mark.parametrize("changed", ("manifest", "signature", "credential"))
def test_health_cannot_authorize_changed_retained_material(tmp_path, monkeypatch, changed):
    module = required(monkeypatch)
    args, _, _, _, _, _ = seed(tmp_path)
    original = args["http_get"]

    def response(url, headers, **limits):
        result = original(url, headers, **limits)
        target = (args["manifest_path"] if changed == "manifest" else args["signature_path"]
                  if changed == "signature" else args["credential_dir"] / "notes/console.token")
        target.write_text("PUBLIC changed after initial verification\n")
        if changed == "credential": target.chmod(0o600)
        return result

    args["http_get"] = response
    with pytest.raises(module.PrerequisiteRefusal) as failure:
        module.write_prerequisite(**args)
    assert failure.value.code == ("cortex_credential_unavailable" if changed == "credential"
                                  else "cortex_release_signature_invalid")
    assert not (args["home"] / ".cortex").exists()


def test_exact_replay_is_readonly_and_other_instance_is_never_overwritten(tmp_path, monkeypatch):
    module = required(monkeypatch)
    args, _, health, _, _, _ = seed(tmp_path)
    first = module.write_prerequisite(**args)
    target = args["home"] / ".cortex/prerequisite.json"
    before = target.stat()
    body = target.read_bytes()
    assert module.write_prerequisite(**args) == first
    assert target.stat().st_ino == before.st_ino and target.stat().st_mtime_ns == before.st_mtime_ns
    args["installation_id"] = health["installation_id"] = str(UUID(int=999))
    with pytest.raises(module.PrerequisiteRefusal, match="cortex_instance_mismatch"):
        module.write_prerequisite(**args)
    assert target.read_bytes() == body and target.stat().st_ino == before.st_ino


def test_uncertain_directory_fsync_never_leaves_an_accepted_new_descriptor(tmp_path, monkeypatch):
    module = required(monkeypatch)
    args, _, _, _, _, _ = seed(tmp_path)
    actual = os.fsync

    def refuse_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("PUBLIC injected directory durability failure")
        return actual(fd)

    monkeypatch.setattr(module.os, "fsync", refuse_directory)
    with pytest.raises(module.PrerequisiteRefusal):
        module.write_prerequisite(**args)
    target = args["home"] / ".cortex/prerequisite.json"
    if target.exists():
        with pytest.raises((ValueError, UnicodeError)):
            json.loads(target.read_text())
