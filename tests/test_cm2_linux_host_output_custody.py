"""Literal dangling aliases must refuse before native build tools."""
import pytest

from test_cm2_linux_host_freeze import product

@pytest.mark.parametrize('name',['bin','cm2-host-archive-inventory.txt'])
def test_dangling_output_alias_is_an_existing_object_and_never_reaches_native_tools(name,tmp_path,monkeypatch):
    module=product(monkeypatch);alias=tmp_path/name;target=tmp_path/'PUBLIC-absent-target'
    alias.symlink_to(target);before=alias.lstat();calls=[]
    def run(*a,**kw):
        calls.append(True)
        raise RuntimeError('PUBLIC intercepted tool call')
    monkeypatch.setattr(module,'run',run)
    with pytest.raises(RuntimeError):module.freeze_linux_cm2_programs(tmp_path)
    assert not calls and alias.is_symlink() and alias.lstat().st_ino==before.st_ino
    assert alias.readlink()==target and not target.exists()
    assert {p.name for p in tmp_path.iterdir()}=={name}
