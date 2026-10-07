"""r330 ratified publication revision: original ten cases preserved separately."""
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import time

import pytest
from test_cm2_linux_publication import POLICY, publication, required
from test_cm2_linux_review_repairs import actual_runtime

def test_revision2_actual_final_name_order_no_secret_and_byte_identical_resume(tmp_path, monkeypatch):
    module, args, response, store, context, proof, members, connection, descriptor, state, calls, call = publication(tmp_path, monkeypatch)
    opens = []; original = os.open
    def opening(path, flags, *a, **kw):
        if flags & os.O_CREAT and flags & os.O_EXCL: opens.append(Path(path).name)
        return original(path, flags, *a, **kw)
    monkeypatch.setattr(os, 'open', opening)
    result = call()
    assert result == {'connection_file': str(connection), 'descriptor_file': str(descriptor)}
    assert opens == [state.name, connection.name, descriptor.name]
    for path in (state, connection, descriptor):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_nlink == 1
        assert path.stat().st_uid == os.getuid()
        for role in ('lead', 'console'):
            assert response[role + '_token'].encode() not in path.read_bytes()
    saved = module.custody.read_private_json(state)
    assert set(saved) == {'schema', 'installation_id', 'create_project', 'receipt', 'kos_policy'}
    assert saved['create_project'] == args['create_project'] and saved['kos_policy'] == POLICY
    assert set(saved['receipt']) == {'operation_id', 'project_id', 'project_key', 'delivery_state', 'lead', 'console'}
    assert all(set(saved['receipt'][r]) == {'principal_id', 'manager', 'expires_at'} for r in ('lead', 'console'))
    link_value = module.custody.read_private_json(connection)
    assert module.custody.validate_connection(link_value) == link_value
    assert link_value['actor_id'] == members['console']['actor_id']
    marker = module.custody.read_private_json(descriptor)
    frozen = json.loads((Path(__file__).parent / 'fixtures/cm2-revision4/fixtures/descriptor.linux.json').read_text())
    assert set(marker) == set(frozen) and set(marker['podman']) == set(frozen['podman'])
    assert marker['schema'] == 'cortex.prerequisite.v2' and marker['connection_file'] == str(connection)
    assert marker['release_manifest'] == str(tmp_path / 'signed/release.json')
    assert marker['release_signature'] == str(tmp_path / 'signed/release.json.minisig')
    assert marker['release_manifest_sha256'] == proof['release_manifest_sha256']
    assert marker['podman']['policy_sha256'] == hashlib.sha256(json.dumps(context['release']['podman'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    before = {p: (p.read_bytes(), module.custody._file_identity(p.stat())) for p in (state, connection, descriptor)}
    def resume_open(path, flags, *a, **kw):
        assert not flags & os.O_CREAT, 'resume published a replacement'
        return original(path, flags, *a, **kw)
    monkeypatch.setattr(os, 'open', resume_open)
    assert call() == result
    assert before == {p: (p.read_bytes(), module.custody._file_identity(p.stat())) for p in before}
    assert not list(tmp_path.glob('.*'))


@pytest.mark.parametrize('stop',['state','connection','descriptor','end-readiness'])
def test_revision2_interruption_retains_only_valid_partial_resume_and_invalid_marker(stop,tmp_path,monkeypatch):
    module,args,response,store,context,proof,members,connection,descriptor,state,calls,call=publication(tmp_path,monkeypatch)
    original=os.open;reads=[];created=[]
    def opening(path,flags,*a,**kw):
        if flags & os.O_CREAT and flags & os.O_EXCL:
            name=Path(path).name
            if name=={'state':state.name,'connection':connection.name,'descriptor':descriptor.name}.get(stop):
                raise OSError('PRIVATE-SYNTHETIC-INTERRUPTION')
            created.append(name)
        return original(path,flags,*a,**kw)
    def readiness(*a,**kw):
        reads.append(True)
        if stop=='end-readiness' and len(reads)>1:
            changed=copy.deepcopy(proof);changed['selinux']='Permissive';return changed
        return copy.deepcopy(proof)
    monkeypatch.setattr(os,'open',opening);monkeypatch.setattr(module,'read_linux_readiness',readiness)
    with pytest.raises(module.PrerequisiteRefusal) as error:call()
    assert 'PRIVATE-SYNTHETIC' not in str(error.value) and not list(tmp_path.glob('.*'))
    if stop=='end-readiness':
        assert descriptor.exists() and descriptor.read_bytes()==b''
        assert stat.S_IMODE(descriptor.stat().st_mode)==0o600 and descriptor.stat().st_nlink==1
    else:assert not descriptor.exists()
    before={p:(p.read_bytes(),p.stat().st_ino) for p in (state,connection) if p.exists()}
    monkeypatch.setattr(os,'open',original)
    monkeypatch.setattr(module,'read_linux_readiness',lambda *a,**kw:copy.deepcopy(proof))
    if stop=='end-readiness':
        with pytest.raises(module.PrerequisiteRefusal):call()
        with pytest.raises(module.PrerequisiteRefusal) as refusal:
            module.read_linux_prerequisite_proof(descriptor,'0'*32,store=store)
        assert refusal.value.code=='cortex_descriptor_invalid'
        assert descriptor.read_bytes()==b''
    else:assert call()=={'connection_file':str(connection),'descriptor_file':str(descriptor)}
    assert before=={p:(p.read_bytes(),p.stat().st_ino) for p in before}


@pytest.mark.parametrize('operation',['exclusive-race','fsync','write','recheck','parent-change'])
def test_revision2_private_once_retains_invalid_owned_inode_and_preserves_foreign_name(operation,tmp_path,monkeypatch):
    module=required();tmp_path.chmod(0o700);target=tmp_path/'value.json'
    original_open,original_close=os.open,os.close;opened=set();closed=set();checks=[];foreign=[]
    def opening(path,flags,*a,**kw):
        if operation=='exclusive-race' and Path(path).name==target.name and flags & os.O_CREAT and flags & os.O_EXCL:
            target.write_bytes(b'{"foreign":true}');target.chmod(0o600);foreign.append((target.read_bytes(),target.stat().st_ino))
        fd=original_open(path,flags,*a,**kw);opened.add(fd);return fd
    def closing(fd):closed.add(fd);return original_close(fd)
    monkeypatch.setattr(os,'open',opening);monkeypatch.setattr(os,'close',closing)
    if operation in ('fsync','write'):
        monkeypatch.setattr(os,operation,lambda *a:(_ for _ in ()).throw(OSError('PRIVATE-SYNTHETIC-IO')))
    def check():
        checks.append(True)
        if operation=='recheck' and len(checks)>=2:raise RuntimeError('PRIVATE-SYNTHETIC-CHECK')
        if operation=='parent-change' and len(checks)>=2:tmp_path.chmod(0o755)
    with pytest.raises(module.PrerequisiteRefusal) as error:module._publish_private_once(target,{'public':True},check)
    assert 'PRIVATE-SYNTHETIC' not in str(error.value) and opened<=closed
    if operation=='exclusive-race':
        assert foreign==[(target.read_bytes(),target.stat().st_ino)] and target.read_bytes()==b'{"foreign":true}'
    else:
        assert target.exists() and target.read_bytes()==b'' and target.stat().st_nlink==1
        assert stat.S_IMODE(target.stat().st_mode)==0o600
    assert not list(tmp_path.glob('.*'))


@pytest.mark.parametrize('replacement',['file','symlink','parent'])
def test_literal_final_name_replacement_invalidates_only_held_owned_inode(replacement,tmp_path,monkeypatch):
    module=required();tmp_path.chmod(0o700);parent=tmp_path/'mutable';parent.mkdir(mode=0o700)
    target=parent/'value.json';retired=tmp_path/'retired';held=parent/'retained';victim=tmp_path/'foreign.json'
    victim.write_bytes(b'PUBLIC foreign victim');victim.chmod(0o600);victim_before=(victim.read_bytes(),victim.stat().st_ino)
    original_write,original_link,original_unlink=os.write,os.link,os.unlink;changed=[];foreign=[];unlinks=[]
    def replace_name():
        if target.exists() and not changed:
            changed.append(True)
            if replacement=='parent':
                parent.rename(retired);parent.mkdir(mode=0o700);target.write_bytes(b'PUBLIC foreign basename');target.chmod(0o600)
            else:
                target.rename(held)
                if replacement=='symlink':target.symlink_to(victim)
                else:target.write_bytes(b'PUBLIC foreign basename');target.chmod(0o600)
            foreign.append((target.lstat().st_ino,target.readlink() if target.is_symlink() else target.read_bytes()))
    def writing(fd,value):
        count=original_write(fd,value)
        replace_name()
        return count
    def linking(source,destination,**kw):
        result=original_link(source,destination,**kw)
        if Path(destination).name==target.name:replace_name()
        return result
    def unlinking(*a,**kw):
        unlinks.append(True)
        return original_unlink(*a,**kw)
    monkeypatch.setattr(os,'write',writing)
    monkeypatch.setattr(os,'link',linking);monkeypatch.setattr(os,'unlink',unlinking)
    with pytest.raises(module.PrerequisiteRefusal):module._publish_private_once(target,{'PUBLIC':'new marker'},lambda:None)
    assert changed==[True]
    assert foreign==[(target.lstat().st_ino,target.readlink() if target.is_symlink() else target.read_bytes())]
    assert victim_before==(victim.read_bytes(),victim.stat().st_ino)
    owned=retired/'value.json' if replacement=='parent' else held
    assert owned.read_bytes()==b'' and owned.stat().st_nlink==1
    assert not unlinks


def test_real_runtime_scope_late_payload_failure_retains_invalid_new_marker_and_closes_fd(tmp_path,monkeypatch):
    module,custody,root,package,release,manifest,record,record_path,signatures,calls,local=actual_runtime(tmp_path,monkeypatch)
    marker=root/'new-descriptor.json';created=[];original=os.open
    def opening(path,flags,*a,**kw):
        fd=original(path,flags,*a,**kw)
        if flags & os.O_RDWR and flags & os.O_CREAT and flags & os.O_EXCL:created.append(fd)
        return fd
    monkeypatch.setattr(os,'open',opening)
    with pytest.raises(module.PrerequisiteRefusal):
        with module.runtime_observation_scope(root,kos_policy={'minimum_version':'6.0.2'},deadline=time.monotonic()+5) as observation:
            result=module._publish_private_once(marker,{'PUBLIC':'marker'},observation.check)
            observation.markers.append((marker,result))  # The producer's actual marker registration contract.
            (package/'bin/cortex').write_bytes(b'PUBLIC changed signed helper')
    assert marker.exists() and marker.read_bytes()==b'' and marker.stat().st_nlink==1
    assert stat.S_IMODE(marker.stat().st_mode)==0o600 and created
    for fd in created:
        with pytest.raises(OSError):os.fstat(fd)


@pytest.mark.parametrize('invalid',[b'',b'{"schema":'])
def test_actual_fresh_consumer_refuses_invalid_marker_without_runtime_or_publication(invalid,tmp_path,monkeypatch):
    module,args,response,store,context,proof,members,connection,descriptor,state,calls,publish=publication(tmp_path,monkeypatch)
    publish();descriptor.write_bytes(invalid);before=(descriptor.read_bytes(),descriptor.stat().st_ino)
    for name in ('read_linux_runtime','publish_linux_prerequisite','owned_api_command'):
        monkeypatch.setattr(module,name,lambda *a,**kw:pytest.fail('invalid marker reached native read or publication'))
    with pytest.raises(module.PrerequisiteRefusal) as error:
        module.read_linux_prerequisite_proof(descriptor,'0'*32,store=store)
    assert error.value.code=='cortex_descriptor_invalid' and before==(descriptor.read_bytes(),descriptor.stat().st_ino)
