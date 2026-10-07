"""Actual ELF/archive/file observations; native freezer/readelf/help intercepted."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_linux_builder_contract import archive, elf, load, versions

CASES=['valid','freeze-cortex','freeze-agent','qualify-cortex','qualify-agent',
       'viewer-cortex','viewer-agent','empty-cortex','empty-agent','change-cortex',
       'change-agent','change-first-during-second','linked-cortex','hardlinked-cortex',
       'nonexec-cortex','preexisting-output']

def product(monkeypatch):
    module=load('build-candidate.py',monkeypatch)
    assert callable(getattr(module,'freeze_linux_cm2_programs',None)), 'CM-2 native helper freezer missing'
    return module

@pytest.mark.parametrize('case',CASES)
def test_exact_cm2_program_bytes_qualify_before_any_final_inventory(case,tmp_path,monkeypatch):
    module=product(monkeypatch);helper=load('linux_binaries.py',monkeypatch)
    monkeypatch.setitem(sys.modules,'linux_binaries',helper)
    calls=[];qualified=[]
    def run(args,*,read=False):
        if 'PyInstaller' in args:
            name=next(a.split('=',1)[1] for a in args if a.startswith('--name='))
            assert name in ('cortex','cortex-agent') and '--onefile' in args and '--target-arch=arm64' not in args
            assert args[args.index('--paths')+1]==str(module.ROOT/'src')
            assert args[-1]==str(module.ROOT/'scripts/release'/('cortex_native.py' if name=='cortex' else 'agent_request.py'))
            calls.append(('freeze',name))
            if case==('freeze-cortex' if name=='cortex' else 'freeze-agent'):raise subprocess.CalledProcessError(1,args)
            binary=tmp_path/'bin'/name;binary.parent.mkdir(exist_ok=True)
            binary.write_bytes(archive([('libpython3.12.so.1.0',elf())]));binary.chmod(0o700)
            if name=='cortex' and case=='linked-cortex':
                original=tmp_path/'original';binary.rename(original);binary.symlink_to(original)
            if name=='cortex' and case=='hardlinked-cortex':os.link(binary,tmp_path/'alias')
            if name=='cortex' and case=='nonexec-cortex':binary.chmod(0o600)
            return ''
        assert 'PyInstaller.utils.cliutils.archive_viewer' in args and read
        binary=Path(args[-1]);calls.append(('viewer',binary.name))
        assert qualified==['cortex','cortex-agent']
        assert not (tmp_path/'cm2-host-archive-inventory.txt').exists()
        assert not (tmp_path/'agent-archive-inventory.txt').exists()
        if case==('viewer-cortex' if binary.name=='cortex' else 'viewer-agent'):raise subprocess.CalledProcessError(1,args)
        if case==('empty-cortex' if binary.name=='cortex' else 'empty-agent'):return ''
        if case==('change-cortex' if binary.name=='cortex' else 'change-agent'):binary.write_bytes(b'PUBLIC replaced executable')
        if case=='change-first-during-second' and binary.name=='cortex-agent':(tmp_path/'bin/cortex').write_bytes(b'PUBLIC changed first')
        return 'PUBLIC embedded native archive '+binary.name
    def native(args,*,read=False):
        if args[0]=='readelf':
            is_outer=Path(args[-1]).parent==tmp_path/'bin'
            if is_outer and case==('qualify-cortex' if Path(args[-1]).name=='cortex' else 'qualify-agent'):return versions('2.36')
            return versions('2.35')
        binary=Path(args[0]);assert args==[str(binary),'--help'] and not read
        assert binary.read_bytes()==archive([('libpython3.12.so.1.0',elf())])
        calls.append(('help',binary.name));qualified.append(binary.name);return ''
    monkeypatch.setattr(module,'run',run);monkeypatch.setattr(module,'run_linux_qualification',native)
    if case=='preexisting-output':
        (tmp_path/'bin').mkdir();(tmp_path/'bin/cortex').write_bytes(b'PUBLIC foreign preexisting')
    if case=='valid':
        result=module.freeze_linux_cm2_programs(tmp_path)
        assert set(result)=={'cortex','cortex-agent'}
        assert [c for c in calls if c[0]=='help']==[('help','cortex'),('help','cortex-agent')]
        for name,inventory in [('cortex','cm2-host-archive-inventory.txt'),('cortex-agent','agent-archive-inventory.txt')]:
            assert result[name]['sha256']==hashlib.sha256((tmp_path/'bin'/name).read_bytes()).hexdigest()
            assert result[name]['archive_inventory']==inventory and result[name]['embedded_elf_count']==1
            assert (tmp_path/inventory).read_text()=='PUBLIC embedded native archive '+name+'\n'
    else:
        with pytest.raises((RuntimeError,subprocess.CalledProcessError)):module.freeze_linux_cm2_programs(tmp_path)
        assert not (tmp_path/'cm2-host-archive-inventory.txt').exists() and not (tmp_path/'agent-archive-inventory.txt').exists()
        if case=='preexisting-output':assert not calls and (tmp_path/'bin/cortex').read_bytes()==b'PUBLIC foreign preexisting'
    assert not (tmp_path/'host-inventory.json').exists()  # Only the complete host stage may publish this.

@pytest.mark.parametrize('stage',['cm2-host','host'])
def test_explicit_cli_routes_the_two_host_contracts_separately(stage,tmp_path,monkeypatch):
    module=product(monkeypatch);calls=[]
    monkeypatch.setattr(module,'host',lambda *a,**kw:calls.append((a,kw)))
    monkeypatch.setattr(sys,'argv',['build-candidate.py','--target','linux-x86_64',stage,'--source-sha','a'*40,
        '--version','0.2.001-test.20261007.1','--output',str(tmp_path/'out'),'--runtime',str(tmp_path/'runtime')])
    module.main();assert len(calls)==1 and calls[0][1].get('cm2',False)==(stage=='cm2-host')
    assert calls[0][1]['target']=='linux-x86_64'

def test_cm2_host_refuses_mac_target_before_output_or_tools(tmp_path,monkeypatch):
    module=product(monkeypatch)
    monkeypatch.setattr(module,'run',lambda *a,**kw:pytest.fail('foreign target reached tools'))
    with pytest.raises(RuntimeError):module.host(tmp_path/'out','a'*40,'fixture',tmp_path/'runtime',target='macos-arm64',cm2=True)
    assert not (tmp_path/'out').exists()
