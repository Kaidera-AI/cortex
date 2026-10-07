"""Literal unsigned CM-2 artifacts; synthetic host admission, signature intercepted."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile

import pytest
from test_cm2_build_catalog_assembly import observation, image, SOURCE, VERSION
from test_linux_builder_contract import load

REPO = Path(__file__).resolve().parents[1]
CASES = ['valid', 'target', 'sequence-zero', 'sequence-bool', 'sequence-missing',
 'image-source', 'image-target', 'image-role', 'image-path', 'image-digest', 'image-label', 'image-layer',
 'payload-map', 'payload-digest', 'payload-source', 'db-payload', 'replay', 'catalog',
 'host-source', 'host-contract', 'host-programs', 'host-help', 'host-glibc', 'host-digest', 'host-lock',
 'reader-source', 'reader-digest', 'reader-bytes', 'linked-input', 'hardlinked-input', 'fifo-input',
 'writable-input-parent', 'extra-input', 'existing-output', 'dangling-output', 'archive-change', 'archive-replace']


def fixture(tmp_path, monkeypatch):
    source_root = tmp_path/'source';source_root.mkdir()
    module, _, rehearsal, _ = observation(source_root, monkeypatch)
    assert callable(getattr(module, 'assemble_cm2', None)), 'CM-2 outer assembler missing'
    # Copy only the six public reader source modules; never credential material.
    reader_builder = load('build-key-reader.py', monkeypatch)
    for relative in reader_builder.MODULES:
        name = 'cortex_v2/'+relative
        destination = source_root/'src'/name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO/'src'/name, destination)
    script = source_root/'deploy/release/10-create-roles.sh'
    script.parent.mkdir(parents=True); script.write_bytes(b'#!/bin/sh\n# PUBLIC synthetic initialization\n')
    for name in ['LICENSE', 'scripts/release/requirements-linux-host-build.txt']:
        dest = source_root/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(REPO/name,dest)
    catalog = load('linux_build_catalog.py', monkeypatch)
    payloads = catalog.source_payloads(source_root)
    for row in [rehearsal['build_catalog'],rehearsal['migration']['build_catalog'],rehearsal['migration_replay']['build_catalog']]:
        row['api_source_payload_sha256'] = payloads['api']['sha256']
    raw = json.dumps(rehearsal['build_catalog'],sort_keys=True,separators=(',',':')).encode()+b'\n'
    rehearsal['build_catalog_sha256'] = hashlib.sha256(raw).hexdigest()
    images = tmp_path/'image-stage';images.mkdir();(images/'images').mkdir();(images/'sbom').mkdir()
    entries={}
    for role in payloads:
        path=images/'images'/f'{role}.oci.tar';entry=image(path,role=role)
        entry.update(archive_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),source_payload_sha256=payloads[role]['sha256'])
        entries[role]=entry
        (images/'sbom'/f'{role}.spdx.json').write_text(json.dumps({'spdxVersion':'SPDX-2.3','SPDXID':'SPDXRef-DOCUMENT','name':'PUBLIC synthetic '+role,'packages':[]}))
    rehearsal['image_ids']={r:e['config_id'] for r,e in entries.items()}
    inventory={'source_sha':SOURCE,'version':VERSION,'target':'linux-x86_64','images':entries,'builder':{'scope':'PUBLIC synthetic build admission'}}
    sources={'schema':'cortex.image-source-payloads.v1','source_revision':SOURCE,'roles':payloads}
    host=tmp_path/'host-stage';host.mkdir();(host/'bin').mkdir();(host/'licenses').mkdir()
    programs={}
    for name,listing in [('cortex','cm2-host-archive-inventory.txt'),('cortex-agent','agent-archive-inventory.txt')]:
        binary=host/'bin'/name;binary.write_bytes(('PUBLIC synthetic host '+name).encode());binary.chmod(0o755)
        (host/listing).write_bytes(b'PUBLIC synthetic archive qualification\n')
        programs[name]={'sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'architecture':'x86_64','help':'PASS','maximum_glibc':'2.35','embedded_elf_count':1,'dependencies':['libc.so.6'],'archive_inventory':listing}
    notice=host/'licenses/PUBLIC-LICENSE';notice.write_bytes(b'PUBLIC synthetic native license\n')
    bootstrap={'schema':'cortex.native-ci-runtime.v1','inputs':{'public':'synthetic native admission'},'runtime':{'python':'3.12.14'},'licenses':{'PUBLIC-LICENSE':hashlib.sha256(notice.read_bytes()).hexdigest()}}
    native={'source_sha':SOURCE,'version':VERSION,'target':'linux-x86_64','host_contract':'linux-cm2','maximum_glibc':'2.35','python':'3.12.14','programs':programs,'native_runtime_bootstrap':bootstrap,'build_lock_sha256':hashlib.sha256((source_root/'scripts/release/requirements-linux-host-build.txt').read_bytes()).hexdigest(),'runtime_packages':[],'builder_packages':[]}
    (host/'host.spdx.json').write_text(json.dumps({'spdxVersion':'SPDX-2.3','SPDXID':'SPDXRef-DOCUMENT','name':'PUBLIC synthetic host','packages':[]}))
    reader=tmp_path/'reader-stage'
    with monkeypatch.context() as context:
        context.setattr(reader_builder,'ROOT',source_root)
        context.setattr(reader_builder.subprocess,'check_output',lambda args,**kw:SOURCE+'\n' if 'rev-parse' in args else '')
        context.setattr(sys,'argv',['build-key-reader.py','--output',str(reader)])
        reader_builder.main()
    reader_receipt=json.loads((reader/'reader.json').read_text())
    calls=[];monkeypatch.setattr(module,'source_identity',lambda sha:calls.append(sha))
    monkeypatch.setattr(module,'run',lambda args,**kw:json.dumps(bootstrap['inputs']))
    return module,images,host,reader,inventory,sources,rehearsal,native,reader_receipt,raw,calls


@pytest.mark.parametrize('case', CASES)
def test_literal_cm2_bundle_assembly_binds_producer_inputs_before_unsigned_success(case,tmp_path,monkeypatch):
    module,images,host,reader,inventory,sources,rehearsal,native,receipt,catalog,calls=fixture(tmp_path,monkeypatch)
    target='linux-x86_64';sequence=7;output=tmp_path/'out'
    if case=='target':target='macos-arm64'
    if case=='sequence-zero':sequence=0
    if case=='sequence-bool':sequence=True
    if case=='sequence-missing':sequence=None
    if case=='image-source':inventory['source_sha']='f'*40
    if case=='image-target':inventory['target']='macos-arm64'
    if case=='image-role':inventory['images'].pop('graph')
    if case=='image-path':inventory['images']['api']['archive']='../foreign'
    if case=='image-digest':inventory['images']['api']['archive_sha256']='0'*64
    if case in ('image-label','image-layer'):image(images/'images/api.oci.tar',role='api',defect='source' if case=='image-label' else 'layer')
    if case=='payload-map':sources['roles']['doc']['files']['PUBLIC-fake']='0'*64
    if case=='payload-digest':inventory['images']['doc']['source_payload_sha256']='0'*64
    if case=='payload-source':sources['source_revision']='f'*40
    if case=='db-payload':sources['roles']['db']=copy.deepcopy(sources['roles']['api'])
    if case=='replay':rehearsal['migration_replay']['status']='applied'
    if case=='catalog':rehearsal['build_catalog_sha256']='0'*64
    if case=='host-source':native['source_sha']='f'*40
    if case=='host-contract':native['host_contract']='foundation'
    if case=='host-programs':native['programs']['cortex-test']=native['programs'].pop('cortex')
    if case=='host-help':native['programs']['cortex']['help']='FAIL'
    if case=='host-glibc':native['maximum_glibc']='2.40'
    if case=='host-digest':(host/'bin/cortex').write_bytes(b'PUBLIC wrong host')
    if case=='host-lock':native['build_lock_sha256']='0'*64
    if case=='reader-source':receipt['source_sha']='f'*40
    if case=='reader-digest':receipt['sha256']='0'*64
    if case=='reader-bytes':(reader/receipt['archive']).write_bytes(b'PUBLIC wrong zip')
    for path,value in [(images/'image-inventory.json',inventory),(images/'source-payload-inventory.json',sources),(images/'rehearsal-receipt.json',rehearsal),(host/'host-inventory.json',native),(reader/'reader.json',receipt)]:path.write_text(json.dumps(value))
    chosen=host/'bin/cortex'
    if case=='linked-input':chosen.rename(tmp_path/'PUBLIC-held');chosen.symlink_to(tmp_path/'PUBLIC-held')
    if case=='hardlinked-input':os.link(chosen,tmp_path/'PUBLIC-alias')
    if case=='fifo-input':chosen.unlink();os.mkfifo(chosen,0o600)
    if case=='writable-input-parent':(host/'bin').chmod(0o777)
    if case=='extra-input':(images/'PUBLIC-undeclared').write_bytes(b'PUBLIC unexpected input')
    if case=='existing-output':output.mkdir();(output/'retained').write_bytes(b'PUBLIC retained')
    if case=='dangling-output':output.symlink_to(tmp_path/'PUBLIC-absent')
    changed=[]
    if case in ('archive-change','archive-replace'):
        actual=tarfile.TarFile.addfile
        def addfile(stream,member,fileobj=None):
            result=actual(stream,member,fileobj)
            if not changed and member.name.endswith('/bin/cortex'):
                changed.append(True)
                if case=='archive-change':chosen.write_bytes(b'PUBLIC changed during archive')
                else:
                    held=tmp_path/'PUBLIC-replacement';held.write_bytes(chosen.read_bytes());held.chmod(0o755);held.replace(chosen)
            return result
        monkeypatch.setattr(tarfile.TarFile,'addfile',addfile)
    call=lambda:module.assemble_cm2(output,images,host,reader,SOURCE,VERSION,release_sequence=sequence,target=target)
    if case=='valid':
        release=call();bundle=output/'unsigned';manifest=bundle/'release.json'
        assert release==json.loads(manifest.read_bytes()) and release['release_sequence']==7
        assert release['source_revision']==SOURCE and release['deployment_class']=='TEST'
        assert release['schema']=='cortex.release.v2' and release['release_lineage']=='cortex-v2-native'
        assert sorted(p.name for p in bundle.iterdir())==sorted(['release.json',release['archive']['name']])
        assert not (bundle/'release.json.minisig').exists()
        assert release['member_reader_archive_sha256']==receipt['sha256']
        assert release['rls_inventory']['sha256']==hashlib.sha256(catalog).hexdigest()
        assert release['images']==inventory['images']
        from cortex_v2.clients import linux_bundle,native_prerequisite
        signature=bundle/'release.json.minisig';signature.write_bytes(b'PUBLIC synthetic signature');signature.chmod(0o600)
        verified=[];monkeypatch.setattr(native_prerequisite,'verify_signature',lambda *a,**kw:verified.append(True))
        result=linux_bundle.inspect_linux_bundle(bundle)
        assert result['release']==release and verified==[True]
        assert result['payload_manifest']['files']==release['files']
        assert 'native/rls-inventory.json' in release['files'] and 'native/member-reader.zip' in release['files']
        assert all((bundle/n).stat().st_mode&0o777==0o600 for n in [release['archive']['name'],'release.json','release.json.minisig'])
        assert bundle.stat().st_mode&0o777==0o700 and calls==[SOURCE]
    else:
        with pytest.raises(RuntimeError):call()
        assert not (output/'unsigned/release.json').exists()
        if case=='existing-output':assert (output/'retained').read_bytes()==b'PUBLIC retained'
        elif case=='dangling-output':assert output.is_symlink() and output.readlink()==tmp_path/'PUBLIC-absent'
        elif case.startswith('archive-'):assert changed
        else:assert not output.exists()


@pytest.mark.parametrize('stage',['cm2-assemble','assemble'])
def test_explicit_cm2_assembly_route_and_unchanged_foundation_route(stage,tmp_path,monkeypatch):
    module=load('build-candidate.py',monkeypatch);calls=[]
    monkeypatch.setattr(module,'assemble_cm2',lambda *a,**kw:calls.append(('cm2',a,kw)),raising=False)
    monkeypatch.setattr(module,'assemble',lambda *a,**kw:calls.append(('foundation',a,kw)))
    arguments=['build-candidate.py','--target','linux-x86_64',stage,'--source-sha',SOURCE,'--version',VERSION,'--output',str(tmp_path/'out'),'--images',str(tmp_path/'images'),'--host',str(tmp_path/'host')]
    if stage=='cm2-assemble':arguments+=['--reader',str(tmp_path/'reader'),'--release-sequence','7']
    monkeypatch.setattr(sys,'argv',arguments);module.main()
    assert len(calls)==1 and calls[0][0]==('cm2' if stage=='cm2-assemble' else 'foundation')
    if stage=='cm2-assemble':assert calls[0][2]['release_sequence']==7 and calls[0][2]['target']=='linux-x86_64'
