"""Real signed-model archive names cannot be both file and directory."""
import hashlib
import io
import json
import tarfile

import pytest

from test_cm2_linux_bundle_preflight import product
from test_cm2_linux_installed_custody import fixture


@pytest.mark.parametrize('order',['directory-first','file-first'])
def test_file_directory_alias_refuses_regardless_of_actual_header_order(order,tmp_path,monkeypatch):
    module=product()
    custody,root,package,release,manifest,archive,calls=fixture(tmp_path,monkeypatch)
    inner=json.loads((package/'release.json').read_bytes())
    extra={'collision':b'PUBLIC file', 'collision/child':b'PUBLIC child'}
    for name,raw in extra.items():inner['files'][name]=hashlib.sha256(raw).hexdigest()
    raw=json.dumps(inner).encode()+b'\n';(package/'release.json').write_bytes(raw)
    release['payload_manifest_sha256']=hashlib.sha256(raw).hexdigest()
    entries=[('collision',b'',tarfile.DIRTYPE),('collision',extra['collision'],tarfile.REGTYPE)]
    if order=='file-first':entries.reverse()
    entries.append(('collision/child',extra['collision/child'],tarfile.REGTYPE))
    with tarfile.open(archive,'w:gz') as stream:
        stream.add(package,arcname='package')
        for name,value,kind in entries:
            entry=tarfile.TarInfo('package/'+name);entry.type=kind;entry.size=len(value);entry.mode=0o700 if kind==tarfile.DIRTYPE else 0o600
            stream.addfile(entry,io.BytesIO(value) if kind==tarfile.REGTYPE else None)
    release['archive'].update(sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),size_bytes=archive.stat().st_size)
    manifest.write_text(json.dumps(release));manifest.chmod(0o600)
    before={p:(p.read_bytes(),p.stat().st_ino) for p in (manifest,archive,root/'signed/release.json.minisig')}
    with pytest.raises(custody.PrerequisiteRefusal):module.inspect_linux_bundle(root/'signed')
    assert calls==['signature']
    assert before=={p:(p.read_bytes(),p.stat().st_ino) for p in before}
