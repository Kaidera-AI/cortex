"""Selected native build route must reach the actual product stages."""
import hashlib
import io
import json
import tarfile
from pathlib import Path
import pytest
from test_linux_builder_contract import load

@pytest.mark.parametrize('stage',['images','host'])
@pytest.mark.parametrize('system,machine',[('Darwin','arm64'),('Linux','aarch64'),('Windows','x86_64')])
def test_linux_stage_refuses_foreign_host_before_output(tmp_path,monkeypatch,stage,system,machine):
    m=load('build-candidate.py',monkeypatch);monkeypatch.setattr(m.platform,'system',lambda:system);monkeypatch.setattr(m.platform,'machine',lambda:machine)
    with pytest.raises(RuntimeError):
        if stage=='images':m.images(tmp_path/'out','a'*40,'fixture',target='linux-x86_64')
        else:m.host(tmp_path/'out','a'*40,'fixture',tmp_path/'runtime',target='linux-x86_64')
    assert not (tmp_path/'out').exists()

@pytest.mark.parametrize('arch,admit',[('amd64',True),('arm64',False)])
def test_linux_oci_identity_checks_literal_config_platform(tmp_path,monkeypatch,arch,admit):
    m=load('build-candidate.py',monkeypatch)
    def blob(value):
        data=json.dumps(value).encode();return data,'sha256:'+hashlib.sha256(data).hexdigest()
    config,cid=blob({'os':'linux','architecture':arch});manifest,mid=blob({'config':{'digest':cid},'layers':[]})
    files={'index.json':json.dumps({'manifests':[{'digest':mid}]}).encode(),'blobs/sha256/'+mid[7:]:manifest,'blobs/sha256/'+cid[7:]:config}
    archive=tmp_path/'image.tar'
    with tarfile.open(archive,'w') as stream:
        for name,data in files.items():entry=tarfile.TarInfo(name);entry.size=len(data);stream.addfile(entry,io.BytesIO(data))
    if admit:assert m.oci_identity(archive,architecture='amd64')['architecture']=='amd64'
    else:
        with pytest.raises(RuntimeError):m.oci_identity(archive,architecture='amd64')
