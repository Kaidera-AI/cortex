"""Actual source/stage/tar bytes; synthetic native/image admissions explicitly inherited."""
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import pytest
from test_cm2_linux_outer_assembly import fixture
from test_cm2_build_catalog_assembly import SOURCE,VERSION

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('defect',['valid','missing','changed','linked','nonexec','receipt-missing','receipt-drift','source-changed'])
def test_boot_enabled_capsule_requires_source_bound_executable_shim(defect,tmp_path,monkeypatch):
    module,images,host,reader,inventory,sources,rehearsal,native,receipt,catalog,calls=fixture(tmp_path,monkeypatch)
    root=tmp_path/'source'
    client=root/'src/cortex_v2/cli/agent_boot.py';client.parent.mkdir(parents=True);client.write_bytes(b'PUBLIC synthetic boot module\n')
    source=root/'scripts/agent-shims/cortex-boot';source.parent.mkdir(parents=True);source.write_bytes((ROOT/'scripts/agent-shims/cortex-boot').read_bytes());source.chmod(0o755)
    shim=host/'bin/cortex-boot';shutil.copyfile(source,shim);shim.chmod(0o755)
    native['shims']={'cortex-boot':{'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'syntax':'PASS','help':'PASS'}}
    from test_cm2_build_catalog_rehearsal import load
    helper=load('linux_build_catalog.py',monkeypatch);payloads=helper.source_payloads(root)
    sources['roles']=payloads
    for role,row in inventory['images'].items():row['source_payload_sha256']=payloads[role]['sha256']
    for row in (rehearsal['build_catalog'],rehearsal['migration']['build_catalog'],rehearsal['migration_replay']['build_catalog']):row['api_source_payload_sha256']=payloads['api']['sha256']
    raw=json.dumps(rehearsal['build_catalog'],sort_keys=True,separators=(',',':')).encode()+b'\n';rehearsal['build_catalog_sha256']=hashlib.sha256(raw).hexdigest()
    if defect=='missing':shim.unlink()
    if defect=='changed':shim.write_bytes(b'PUBLIC changed shim\n')
    if defect=='linked':shim.rename(tmp_path/'public-held');shim.symlink_to(tmp_path/'public-held')
    if defect=='nonexec':shim.chmod(0o600)
    if defect=='receipt-missing':native.pop('shims')
    if defect=='receipt-drift':native['shims']['cortex-boot']['sha256']='f'*64
    if defect=='source-changed':source.write_bytes(b'PUBLIC changed source\n')
    for p,v in [(images/'image-inventory.json',inventory),(images/'source-payload-inventory.json',sources),(images/'rehearsal-receipt.json',rehearsal),(host/'host-inventory.json',native),(reader/'reader.json',receipt)]:p.write_text(json.dumps(v))
    out=tmp_path/'out'
    call=lambda:module.assemble_cm2(out,images,host,reader,SOURCE,VERSION,target='linux-x86_64',release_sequence=7)
    if defect=='valid':
        call();release=json.loads((out/'unsigned/release.json').read_text())
        assert release['files']['bin/cortex-boot']==hashlib.sha256(source.read_bytes()).hexdigest()
        archives=list((out/'unsigned').glob('*.tar.gz'));assert len(archives)==1
        with tarfile.open(archives[0]) as archive:
            member=next(m for m in archive.getmembers() if m.name.endswith('/bin/cortex-boot'))
            assert member.mode==0o755 and archive.extractfile(member).read()==source.read_bytes()
    else:
        with pytest.raises(RuntimeError):call()
        assert not (out/'unsigned/release.linux.json').exists()
