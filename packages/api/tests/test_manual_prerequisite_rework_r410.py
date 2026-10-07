"""Exact existing descriptor types and KOS-compatible path text refusals."""
import json
import os

import pytest

from test_manual_prerequisite_r407 import required, seed


@pytest.mark.parametrize("field,value", (("release_sequence", True), ("release_sequence", 1.0),
                                        ("owner_uid", float(os.getuid()))))
def test_existing_numeric_alias_cannot_return_success_or_change_the_file(tmp_path, monkeypatch, field, value):
    module = required(monkeypatch)
    args, _, _, _, _, _ = seed(tmp_path)
    module.write_prerequisite(**args)
    target = args["home"] / ".cortex/prerequisite.json"
    invalid = json.loads(target.read_text())
    invalid[field] = value
    target.write_text(json.dumps(invalid, indent=2) + "\n")
    original = target.read_bytes()
    before = target.stat()
    with pytest.raises(module.PrerequisiteRefusal, match="cortex_descriptor_invalid"):
        module.write_prerequisite(**args)
    after = target.stat()
    assert target.read_bytes() == original
    assert (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)


@pytest.mark.parametrize("field", ("home", "manifest_path", "signature_path", "credential_dir"))
@pytest.mark.parametrize("control", ("\n", "\x7f"))
def test_safe_pathname_with_control_text_refuses_before_publication(tmp_path, monkeypatch, field, control):
    module = required(monkeypatch)
    args, _, _, _, _, _ = seed(tmp_path)
    old = args[field]
    replacement = old.with_name(old.name + control + "PUBLIC")
    if field == "home":
        relative = {k: args[k].relative_to(old) for k in ("manifest_path", "signature_path", "credential_dir")}
        old.rename(replacement)
        args["home"] = replacement
        for key, value in relative.items(): args[key] = replacement / value
    else:
        old.rename(replacement)
        args[field] = replacement
    with pytest.raises(module.PrerequisiteRefusal):
        module.write_prerequisite(**args)
    assert not (args["home"] / ".cortex").exists()
