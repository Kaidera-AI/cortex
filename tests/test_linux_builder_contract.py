"""R198 additive Linux regressions: literal ELF/CArchive bytes, no native-build claim."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import zlib

import pytest

ROOT = Path(__file__).resolve().parents[1]

def load(name, monkeypatch):
    path = ROOT / 'scripts/release' / name
    assert path.is_file(), 'R198 Linux component is not implemented: ' + name
    monkeypatch.syspath_prepend(str(path.parent))
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('cx198_' + name.replace('.', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def elf(machine=62, elfclass=2):
    data=bytearray(64);data[:4]=b'\x7fELF';data[4:7]=bytes((elfclass,1,1))
    struct.pack_into('<HHI',data,16,3,machine,1)
    return bytes(data)

def archive(payloads, *, pyver=312):
    data=b'';toc=b''
    for name,value in payloads:
        compressed=zlib.compress(value);encoded=name.encode()+b'\0'
        size=18+len(encoded);size=(size+15)//16*16
        toc+=struct.pack('!IIIIBc',size,len(data),len(compressed),len(value),1,b'b')+encoded+b'\0'*(size-18-len(encoded))
        data+=compressed
    cookie=struct.pack('!8sIIII64s',b'MEI\014\013\012\013\016',len(data)+len(toc)+88,len(data),len(toc),pyver,b'libpython3.12.so.1.0')
    return elf()+data+toc+cookie

def versions(*needed, definition=None):
    text=''
    if definition:text="Version definition section '.gnu.version_d' contains 1 entry:\n  Name: GLIBC_"+definition+'\n'
    if needed:
        text+="Version needs section '.gnu.version_r' contains 1 entry:\n  File: libc.so.6\n"
        text+=''.join('  Name: GLIBC_'+v+'  Flags: none Version: 2\n' for v in needed)
    else:text+='No version information found in this file.\n'
    return text

@pytest.mark.parametrize('required',['2.2.5','2.17','2.28','2.35'])
def test_numeric_glibc_floor_admits_valid_needed_versions(monkeypatch,required):
    module=load('linux_binaries.py',monkeypatch)
    result=module.elf_requirements(elf(),versions(required))
    assert result['architecture']=='x86_64' and result['maximum_required_glibc']==required

@pytest.mark.parametrize('required',['2.36','2.39','2.35.1','PRIVATE'])
def test_higher_or_private_glibc_requirements_refuse(monkeypatch,required):
    module=load('linux_binaries.py',monkeypatch)
    with pytest.raises(RuntimeError,match='GLIBC'):
        module.elf_requirements(elf(),versions(required))

@pytest.mark.parametrize('data',[b'not ELF',elf(183),elf(62,1),elf()[:20]])
def test_wrong_outer_or_embedded_elf_header_refuses(monkeypatch,data):
    module=load('linux_binaries.py',monkeypatch)
    with pytest.raises(RuntimeError,match='ELF'):
        module.elf_requirements(data,versions('2.35'))

def test_definition_is_not_a_requirement_and_static_has_none(monkeypatch):
    module=load('linux_binaries.py',monkeypatch)
    assert module.elf_requirements(elf(),versions('2.17',definition='2.99'))['maximum_required_glibc']=='2.17'
    assert module.elf_requirements(elf(),versions())['maximum_required_glibc'] is None

def test_unknown_version_report_does_not_claim_a_floor(monkeypatch):
    module=load('linux_binaries.py',monkeypatch)
    with pytest.raises(RuntimeError):module.elf_requirements(elf(),'unknown tool output')

def test_carchive_extracts_all_real_compressed_fixture_bytes(tmp_path,monkeypatch):
    module=load('linux_binaries.py',monkeypatch);binary=tmp_path/'binary'
    expected=[('libpython3.12.so.1.0',elf()),('nested/not-a-library-name',elf()),('modules.pyz',b'PYZ DATA')]
    binary.write_bytes(archive(expected))
    assert list(module.embedded_payloads(binary))==expected

@pytest.mark.parametrize('kind',['duplicate','traversal','wrong-python','truncated','corrupt-zlib'])
def test_invalid_embedded_archive_refuses_instead_of_hiding_a_library(tmp_path,monkeypatch,kind):
    module=load('linux_binaries.py',monkeypatch);binary=tmp_path/'binary'
    pairs=[('libpython3.12.so.1.0',elf())]
    if kind=='duplicate':pairs*=2
    if kind=='traversal':pairs=[('../escape.so',elf())]
    value=archive(pairs,pyver=313 if kind=='wrong-python' else 312)
    if kind=='truncated':value=value[:-1]
    if kind=='corrupt-zlib':value=value[:64]+b'BROKEN'+value[70:]
    binary.write_bytes(value)
    with pytest.raises(RuntimeError):list(module.embedded_payloads(binary))

@pytest.mark.parametrize('bad',['embedded-architecture','embedded-glibc','no-embedded-elf','help-refused'])
def test_full_program_inspection_has_no_partial_admission(tmp_path,monkeypatch,bad):
    module=load('linux_binaries.py',monkeypatch);binary=tmp_path/'cortex-agent'
    payload=elf(183) if bad=='embedded-architecture' else elf()
    binary.write_bytes(archive([('odd-name',b'not ELF' if bad=='no-embedded-elf' else payload)]))
    calls=[]
    def run(args,*,read=False):
        calls.append(args)
        if args[0]=='readelf':return versions('2.36' if bad=='embedded-glibc' and args[-1]!=str(binary) else '2.35')
        if args==[str(binary),'--help']:
            if bad=='help-refused':raise subprocess.CalledProcessError(9,args)
            return ''
        pytest.fail('unexpected command: '+repr(args))
    with pytest.raises((RuntimeError,subprocess.CalledProcessError)):module.verify_program(binary,run=run)
    if bad!='help-refused':assert [str(binary),'--help'] not in calls

def test_scan_unknown_named_elf_and_execute_help_before_inventory(tmp_path,monkeypatch):
    module=load('linux_binaries.py',monkeypatch);binary=tmp_path/'cortex-test'
    binary.write_bytes(archive([('nested/odd-name',elf()),('data.txt',b'public fixture')]))
    calls=[]
    def run(args,*,read=False):calls.append(args);return versions('2.35') if args[0]=='readelf' else ''
    result=module.verify_program(binary,run=run)
    assert result['sha256']==hashlib.sha256(binary.read_bytes()).hexdigest()
    assert result['embedded_elf_count']==1 and calls[-1]==[str(binary),'--help']
    assert len([a for a in calls if a[0]=='readelf'])==2

@pytest.mark.parametrize('system,machine',[('Darwin','arm64'),('Linux','aarch64'),('Windows','x86_64')])
def test_bootstrap_refuses_non_native_target_before_creating_output(tmp_path,monkeypatch,system,machine):
    module=load('bootstrap-linux-runtime.py',monkeypatch)
    monkeypatch.setattr(module.platform,'system',lambda:system);monkeypatch.setattr(module.platform,'machine',lambda:machine)
    output=tmp_path/'never'
    with pytest.raises(RuntimeError):module.build(output)
    assert not output.exists()

def test_linux_bootstrap_pins_and_environment_are_independent_of_ambient_libraries(tmp_path,monkeypatch):
    module=load('bootstrap-linux-runtime.py',monkeypatch)
    assert module.INPUTS['target']=='linux-x86_64' and module.INPUTS['maximum_glibc']=='2.35'
    assert module.INPUTS['python']['version']=='3.12.14' and module.INPUTS['openssl']['version']=='3.5.8'
    inherited={name:'poison' for name in ['PYTHONPATH','PYTHONHOME','VIRTUAL_ENV','LD_PRELOAD','LD_LIBRARY_PATH','CPATH','LIBRARY_PATH','CONFIG_SITE','CFLAGS','LDFLAGS']}
    environment=module.build_environment(tmp_path,inherited)
    assert 'poison' not in environment.values()
    assert environment['PATH']=='/usr/bin:/bin:/usr/sbin:/sbin'
    assert environment['SSL_CERT_FILE']=='/etc/ssl/certs/ca-certificates.crt'
    assert environment['LD_LIBRARY_PATH']==str(tmp_path/'openssl/lib')+':'+str(tmp_path/'python/lib')

def test_same_version_amd64_recipes_are_parity_checked_before_build(monkeypatch,tmp_path):
    module=load('linux_recipe_parity.py',monkeypatch)
    result=module.verify_recipe_parity(ROOT)
    assert result['python']['architecture']=='amd64' and result['postgres']['architecture']=='amd64'
    import shutil
    for path in ['Dockerfile','deploy/release/Containerfile.db','deploy/release/Dockerfile.linux-amd64','deploy/release/Containerfile.db.linux-amd64','deploy/release/linux-amd64-bases.json']:
        target=tmp_path/path;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/path,target)
    path=tmp_path/'deploy/release/Dockerfile.linux-amd64';path.write_text(path.read_text().replace('USER 10001:10001','USER 0:0'))
    with pytest.raises(RuntimeError,match='parity'):module.verify_recipe_parity(tmp_path)

def test_linux_workflow_uses_x64_floor_builder_and_distinct_push_prefix(monkeypatch):
    path=ROOT/'.github/workflows/cortex-linux-candidate.yml'
    assert path.is_file(),'R198 Linux workflow absent'
    import yaml
    data=yaml.load(path.read_text(),Loader=yaml.BaseLoader)
    assert data['on']['push']['branches']==['ren-cx/linux-package-build-admitted-*']
    assert {name: job['runs-on'] for name, job in data['jobs'].items()} == {
        'identity': 'ubuntu-22.04', 'images': 'ubuntu-24.04', 'host': 'ubuntu-22.04', 'package': 'ubuntu-22.04'}
    body=path.read_text();assert 'bootstrap-linux-runtime.py' in body and '--target linux-x86_64' in body
    assert 'brew install podman' in body and 'apt-get install -y podman' not in body
    assert 'formula-commit' not in body and 'GITHUB_RUN_ATTEMPT' in body

@pytest.mark.parametrize('failure',['none','cortex-agent'])
def test_linux_freezer_validates_both_before_final_inventory(tmp_path,monkeypatch,failure):
    module=load('build-candidate.py',monkeypatch)
    assert callable(getattr(module,'freeze_linux_programs',None)),'Linux freeze branch is absent'
    helper=load('linux_binaries.py',monkeypatch);calls=[]
    def run(args,*,read=False):
        if 'PyInstaller' in args:
            name=next(a.split('=',1)[1] for a in args if a.startswith('--name='));binary=tmp_path/'bin'/name;binary.parent.mkdir(exist_ok=True);binary.write_bytes(archive([('libpython3.12.so.1.0',elf())]));calls.append(('freeze',name))
            assert '--target-arch=arm64' not in args
            return ''
        if 'PyInstaller.utils.cliutils.archive_viewer' in args:return 'public archive inventory'
        pytest.fail('unexpected freeze command: '+repr(args))
    def verify(binary,*,run):
        calls.append(('verify',binary.name))
        if binary.name==failure:raise RuntimeError('binary refusal')
        return {'sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'dependencies':[],'embedded_elf_count':1}
    monkeypatch.setattr(module,'run',run);monkeypatch.setattr(helper,'verify_program',verify)
    # The production module resolves the canonical helper once from the release directory.
    monkeypatch.setitem(sys.modules,'linux_binaries',helper)
    if failure!='none':
        with pytest.raises(RuntimeError):module.freeze_linux_programs(tmp_path)
        assert not (tmp_path/'host-archive-inventory.txt').exists()
    else:
        programs=module.freeze_linux_programs(tmp_path)
        assert set(programs)=={'cortex-test','cortex-agent'}
        assert [c for c in calls if c[0]=='verify']==[('verify','cortex-test'),('verify','cortex-agent')]
