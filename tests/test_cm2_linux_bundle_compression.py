"""Literal PAX/gzip bounds and payload-byte checks, using real stdlib streams."""
import hashlib
import io
import json
import tarfile

import pytest

from test_cm2_linux_bundle_preflight import product
from test_cm2_linux_installed_custody import fixture


@pytest.mark.parametrize('case',['valid-pax','payload-bytes','gzip-crc','pax-bound','pax-sparse'])
def test_actual_pax_metadata_and_gzip_closure_are_bounded_before_interpretation(case,tmp_path,monkeypatch):
    module=product()
    custody,root,package,release,manifest,archive,calls=fixture(tmp_path,monkeypatch)
    manifest.write_bytes(json.dumps(release).encode());manifest.chmod(0o600)
    if case=='payload-bytes':(package/'bin/cortex').write_bytes(b'PUBLIC foreign helper bytes')
    with tarfile.open(archive,'w:gz') as stream:
        stream.add(package,arcname='package')  # Actual floating mtime creates valid PAX metadata.
        if case=='pax-sparse':
            entry=tarfile.TarInfo('package/foreign-sparse');entry.size=2
            entry.pax_headers={'GNU.sparse.major':'1','GNU.sparse.minor':'0','GNU.sparse.realsize':'1024'}
            stream.addfile(entry,io.BytesIO(b'0\n'))
    if case=='gzip-crc':
        value=bytearray(archive.read_bytes());value[-8]^=1;archive.write_bytes(value)
    release['archive'].update(sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),size_bytes=archive.stat().st_size)
    manifest.write_bytes(json.dumps(release).encode());manifest.chmod(0o600)
    if case=='pax-bound':
        monkeypatch.setattr(module,'MAX_METADATA_BYTES',10)
        monkeypatch.setattr(tarfile.TarInfo,'_proc_pax',lambda *a:pytest.fail('oversized PAX reached interpretation'))
    if case=='pax-sparse':
        monkeypatch.setattr(tarfile.TarInfo,'_proc_gnusparse_10',lambda *a:pytest.fail('sparse payload reached map interpretation'))
    baseline={p:(p.read_bytes(),p.stat().st_ino) for p in (manifest,archive,root/'signed/release.json.minisig')}
    if case=='valid-pax':
        value=module.inspect_linux_bundle(root/'signed')
        assert value['release']==release and value['archive_root']=='package' and calls==['signature']
    else:
        with pytest.raises(custody.PrerequisiteRefusal):module.inspect_linux_bundle(root/'signed')
    assert baseline=={p:(p.read_bytes(),p.stat().st_ino) for p in baseline}
