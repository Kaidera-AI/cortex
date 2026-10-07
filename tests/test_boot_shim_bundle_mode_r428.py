"""Actual assembled tar consumer; signature/native admissions are public test seams."""
import copy
import hashlib
import io
import json
import tarfile
from pathlib import Path
import pytest
from test_boot_shim_assembly_r428 import test_boot_enabled_capsule_requires_source_bound_executable_shim as build_capsule
from cortex_v2.clients import linux_bundle,native_prerequisite


@pytest.mark.parametrize('defect',['valid','nonexec'])
def test_signed_bundle_requires_boot_shim_execution_mode(defect,tmp_path,monkeypatch):
    build_capsule('valid',tmp_path,monkeypatch)
    bundle=tmp_path/'out/unsigned';manifest=bundle/'release.json';release=json.loads(manifest.read_bytes())
    signature=bundle/'release.json.minisig';signature.write_bytes(b'PUBLIC synthetic signature');signature.chmod(0o600)
    monkeypatch.setattr(native_prerequisite,'verify_signature',lambda *a,**kw:None)
    if defect=='nonexec':
        archive=bundle/release['archive']['name']
        with tarfile.open(archive,'r:gz') as stream:
            entries=[(copy.copy(member),stream.extractfile(member).read() if member.isfile() else None) for member in stream.getmembers()]
        with tarfile.open(archive,'w:gz') as stream:
            for member,raw in entries:
                if member.name.endswith('/bin/cortex-boot'):member.mode=0o600
                stream.addfile(member,io.BytesIO(raw) if raw is not None else None)
        archive.chmod(0o600)
        release['archive']['sha256']=hashlib.sha256(archive.read_bytes()).hexdigest();release['archive']['size_bytes']=archive.stat().st_size
        manifest.write_text(json.dumps(release))
        with pytest.raises(native_prerequisite.PrerequisiteRefusal):linux_bundle.inspect_linux_bundle(bundle)
    else:
        result=linux_bundle.inspect_linux_bundle(bundle)
        assert result['payload_manifest']['files']['bin/cortex-boot']==release['files']['bin/cortex-boot']
