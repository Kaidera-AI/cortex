"""Public file replacements at actual named-read seams; no credential files."""
from pathlib import Path
import tarfile

import pytest
from test_cm2_linux_outer_assembly import test_literal_cm2_bundle_assembly_binds_producer_inputs_before_unsigned_success as valid_driver, VERSION


class ActiveAssembly:
    def __init__(self, monkeypatch):
        self.monkeypatch=monkeypatch;self.active=False;self.on_activate=lambda:None
    def __getattr__(self,name):return getattr(self.monkeypatch,name)
    def setattr(self,*args,**kwargs):
        if len(args)>=3 and args[1]=='run' and hasattr(args[0],'assemble_cm2'):
            original=args[2]
            def activate(*a,**kw):self.active=True;self.on_activate();return original(*a,**kw)
            args=(args[0],args[1],activate)+args[3:]
        return self.monkeypatch.setattr(*args,**kwargs)


@pytest.mark.parametrize('input_kind',['reader','archive'])
def test_complete_valid_assembly_uses_held_reader_and_archive_instead_of_following_new_alias(input_kind,tmp_path,monkeypatch):
    proxy=ActiveAssembly(monkeypatch)
    foreign=tmp_path/'PUBLIC-foreign-content';foreign.write_bytes(b'PUBLIC synthetic foreign bytes; no credential\n')
    original=Path.open;named_reads=[]
    def open_named(path,*args,**kwargs):
        mode=args[0] if args else kwargs.get('mode','r')
        selected=(path.name.startswith('cortex-key-reader-') and path.suffix=='.zip') if input_kind=='reader' else path.name.endswith('.tar.gz')
        if proxy.active and selected and mode=='rb' and not named_reads and path.is_relative_to(tmp_path):
            path.rename(tmp_path/'PUBLIC-held-original');path.symlink_to(foreign)
            named_reads.append(True)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',open_named)
    valid_driver('valid',tmp_path,proxy)
    assert named_reads==[]
    assert foreign.read_bytes()==b'PUBLIC synthetic foreign bytes; no credential\n'


def test_replaced_payload_alias_refuses_before_foreign_bytes_enter_public_archive(tmp_path,monkeypatch):
    proxy=ActiveAssembly(monkeypatch)
    foreign=tmp_path/'PUBLIC-foreign-content';marker=b'PUBLIC synthetic excluded content; no credential\n';foreign.write_bytes(marker)
    helper=tmp_path/'out'/('cortex-'+VERSION+'-linux-x86_64')/'bin/cortex'
    original=tarfile.TarFile.addfile;changed=[]
    def addfile(stream,member,fileobj=None):
        if proxy.active and member.name.endswith('/bin') and not changed:
            helper.rename(tmp_path/'PUBLIC-held-helper');helper.symlink_to(foreign);changed.append(True)
        return original(stream,member,fileobj)
    monkeypatch.setattr(tarfile.TarFile,'addfile',addfile)
    with pytest.raises(RuntimeError):valid_driver('valid',tmp_path,proxy)
    assert changed and not (tmp_path/'out/unsigned/release.json').exists()
    foreign_embedded=False
    for archive in (tmp_path/'out/unsigned').glob('*.tar.gz'):
        with tarfile.open(archive) as stream:
            for member in stream:
                if member.isfile():
                    with stream.extractfile(member) as content:foreign_embedded|=marker in content.read()
    assert not foreign_embedded
    assert foreign.read_bytes()==marker and helper.is_symlink()


@pytest.mark.parametrize('stage',['image-stage','host-stage','reader-stage'])
def test_undeclared_empty_input_directory_refuses_before_output(stage,tmp_path,monkeypatch):
    proxy=ActiveAssembly(monkeypatch)
    directory=tmp_path/stage/'PUBLIC-undeclared-directory'
    proxy.on_activate=lambda:directory.mkdir()
    with pytest.raises(RuntimeError):valid_driver('valid',tmp_path,proxy)
    assert directory.is_dir() and not (tmp_path/'out').exists()
