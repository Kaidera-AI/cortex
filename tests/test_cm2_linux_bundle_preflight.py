"""Actual private gzip/tar bytes; signature seam intercepted, no installation."""
import copy
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import time

import pytest


CASES = ['valid', 'signature-refused', 'manifest-restore', 'signature-restore',
    'archive-restore', 'archive-replace', 'parent-restore', 'archive-digest', 'archive-size',
    'linked-archive', 'hardlinked-archive', 'public-root', 'public-manifest', 'outer-duplicate',
    'outer-overflow', 'wrong-target', 'wrong-lineage', 'source-mismatch', 'class-mismatch',
    'inner-digest', 'inner-duplicate', 'helper-digest', 'helper-noexec', 'helper-setuid',
    'image-digest', 'extra-file', 'missing-file', 'traversal', 'absolute', 'duplicate-member',
    'symlink-member', 'hardlink-member', 'device-member', 'sparse-member', 'second-root',
    'extra-directory', 'bad-sums', 'bad-gzip', 'trailing-tar', 'archive-bound', 'deadline']


def product():
    assert importlib.util.find_spec('cortex_v2.clients.linux_bundle') is not None, 'Linux signed-bundle preflight missing'
    module = importlib.import_module('cortex_v2.clients.linux_bundle')
    assert callable(getattr(module,'inspect_linux_bundle',None)), 'Linux signed-bundle preflight missing'
    return module


@pytest.mark.parametrize('case', CASES)
def test_actual_signed_bundle_bytes_are_checked_before_any_installation_write(case, tmp_path, monkeypatch):
    module = product()
    custody = importlib.import_module('cortex_v2.clients.native_prerequisite')
    tmp_path.chmod(0o700)
    bundle = tmp_path/'bundle';bundle.mkdir(mode=0o700)
    release = json.loads((Path(__file__).parent/'fixtures/cm2-revision4/fixtures/release.linux.json').read_text())
    payload = {'bin/cortex':b'PUBLIC helper fixture', 'bin/cortex-agent':b'PUBLIC agent fixture'}
    payload.update({'images/'+role+'.oci.tar':('PUBLIC '+role+' image fixture').encode() for role in custody.ROLES})
    files = {name:hashlib.sha256(raw).hexdigest() for name,raw in payload.items()}
    release['files'] = copy.deepcopy(files)
    for role in custody.ROLES:release['images'][role]['archive_sha256']=files['images/'+role+'.oci.tar']
    inner = {'schema':'cortex.test-package.v1','target':'linux-x86_64','deployment_class':'TEST',
             'source_sha':release['source_revision'],'files':copy.deepcopy(files),'images':copy.deepcopy(release['images'])}
    if case=='source-mismatch':inner['source_sha']='2'*40
    if case=='class-mismatch':inner['deployment_class']='PRODUCTION'
    if case=='helper-digest':inner['files']['bin/cortex']='0'*64
    if case=='image-digest':inner['images']['api']['archive_sha256']='0'*64
    raw=json.dumps(inner,sort_keys=True).encode()+b'\n'
    if case=='inner-duplicate':raw=b'{"schema":"foreign",'+raw[1:]
    release['payload_manifest_sha256']=hashlib.sha256(raw).hexdigest()
    if case=='inner-digest':release['payload_manifest_sha256']='0'*64
    contents=dict(payload,**{'release.json':raw})
    if case=='extra-file':contents['undeclared']=b'PUBLIC extra'
    if case=='missing-file':contents.pop('bin/cortex-agent')
    if case=='bad-sums':contents['SHA256SUMS']=b'PUBLIC wrong checksum list\n'
    members=[('package',b'',tarfile.DIRTYPE,0o700),('package/bin',b'',tarfile.DIRTYPE,0o700),
             ('package/images',b'',tarfile.DIRTYPE,0o700)]
    members += [('package/'+name,value,tarfile.REGTYPE,0o700 if name.startswith('bin/') else 0o600)
                for name,value in contents.items()]
    if case in ('helper-noexec','helper-setuid'):
        members=[(n,v,t,0o600 if case=='helper-noexec' else 0o4700) if n=='package/bin/cortex' else (n,v,t,m) for n,v,t,m in members]
    if case in ('traversal','absolute','second-root','extra-directory'):
        name={'traversal':'package/../escape','absolute':'/escape','second-root':'foreign/escape','extra-directory':'package/undeclared-dir'}[case]
        members.append((name,b'',tarfile.DIRTYPE if case=='extra-directory' else tarfile.REGTYPE,0o600))
    if case=='duplicate-member':members.append(('package/bin/cortex',payload['bin/cortex'],tarfile.REGTYPE,0o700))
    if case.endswith('-member'):
        kind={'symlink-member':tarfile.SYMTYPE,'hardlink-member':tarfile.LNKTYPE,
              'device-member':tarfile.CHRTYPE,'sparse-member':tarfile.GNUTYPE_SPARSE}.get(case)
        if kind:members.append(('package/foreign-special',b'',kind,0o600))
    archive=bundle/release['archive']['name']
    with tarfile.open(archive,'w:gz') as stream:
        for name,value,kind,mode in members:
            entry=tarfile.TarInfo(name);entry.type=kind;entry.mode=mode;entry.size=len(value)
            if kind in (tarfile.SYMTYPE,tarfile.LNKTYPE):entry.linkname='package/bin/cortex'
            stream.addfile(entry,io.BytesIO(value) if kind==tarfile.REGTYPE else None)
    if case=='bad-gzip':archive.write_bytes(b'PUBLIC invalid gzip archive')
    if case=='trailing-tar':
        extra=io.BytesIO()
        with tarfile.open(fileobj=extra,mode='w:gz') as stream:
            entry=tarfile.TarInfo('package/hidden-after-end');stream.addfile(entry)
        with archive.open('ab') as stream:stream.write(extra.getvalue())
    archive.chmod(0o600)
    release['archive'].update(sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),size_bytes=archive.stat().st_size)
    if case=='archive-digest':release['archive']['sha256']='0'*64
    if case=='archive-size':release['archive']['size_bytes']+=1
    if case=='wrong-target':release['target']='macos-arm64'
    if case=='wrong-lineage':release['release_lineage']='legacy'
    if case=='outer-overflow':release['release_id']='x'*1048577
    manifest=bundle/'release.json';signature=bundle/'release.json.minisig'
    manifest_raw=json.dumps(release).encode()+b'\n'
    if case=='outer-duplicate':manifest_raw=b'{"schema":"foreign",'+manifest_raw[1:]
    manifest.write_bytes(manifest_raw);manifest.chmod(0o600)
    signature.write_bytes(b'PUBLIC synthetic signature');signature.chmod(0o600)
    if case=='linked-archive':
        held=bundle/'original';archive.rename(held);archive.symlink_to(held)
    if case=='hardlinked-archive':os.link(archive,bundle/'alias')
    if case=='public-root':bundle.chmod(0o755)
    if case=='public-manifest':manifest.chmod(0o644)
    if case=='archive-bound':monkeypatch.setattr(module,'MAX_ARCHIVE_BYTES',10)
    clock=[0.0];monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    calls=[]
    def verify(selected,detached,*,trusted_public_key=custody.RELEASE_PUBLIC_KEY,timeout=5):
        calls.append(True)
        assert selected==manifest and detached==signature and trusted_public_key==custody.RELEASE_PUBLIC_KEY and 0<timeout<=5
        if case=='signature-refused':raise custody.PrerequisiteRefusal('cortex_release_signature_invalid')
        if case in ('manifest-restore','signature-restore','archive-restore'):
            p={'manifest-restore':manifest,'signature-restore':signature,'archive-restore':archive}[case]
            before=p.stat();original=p.read_bytes();p.write_bytes(b'PUBLIC changed input');p.write_bytes(original)
            os.utime(p,ns=(before.st_atime_ns,before.st_mtime_ns))
        if case=='archive-replace':
            replacement=bundle/'replacement';replacement.write_bytes(archive.read_bytes());replacement.chmod(0o600);replacement.replace(archive)
        if case=='parent-restore':
            held=tmp_path/'held';bundle.rename(held);bundle.mkdir(mode=0o700);bundle.rmdir();held.rename(bundle)
        if case=='deadline':clock[0]=31
    monkeypatch.setattr(custody,'verify_signature',verify)
    monkeypatch.setattr(tarfile.TarFile,'extract',lambda *a,**kw:pytest.fail('preflight extracted payload'))
    monkeypatch.setattr(tarfile.TarFile,'extractall',lambda *a,**kw:pytest.fail('preflight extracted payload'))
    baseline={str(p.relative_to(tmp_path)):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    if case=='valid':
        result=module.inspect_linux_bundle(bundle,deadline=30)
        assert set(result)=={'release','payload_manifest','archive_root','release_manifest_sha256'}
        assert result['release']==release and result['payload_manifest']==inner and result['archive_root']=='package'
        assert result['release_manifest_sha256']==hashlib.sha256(manifest_raw).hexdigest() and calls==[True]
    else:
        with pytest.raises(custody.PrerequisiteRefusal):module.inspect_linux_bundle(bundle,deadline=30)
    assert baseline=={str(p.relative_to(tmp_path)):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert not (tmp_path/'runtime').exists() and not (tmp_path/'install.json').exists()
