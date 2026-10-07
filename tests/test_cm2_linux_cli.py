"""Native CLI boundary; native effects are intercepted, input FDs/files are real."""
import copy
import importlib
import io
import json
import os
from pathlib import Path
import secrets
import time

import pytest

from test_cm2_linux_engine_observation import POLICY


CASES = ['proof', 'provision', 'legacy', 'unknown-flag', 'duplicate-flag', 'missing-flag',
    'relative-descriptor', 'bad-nonce', 'nonce-uppercase', 'nonce-newline', 'mac', 'root',
    'request-public', 'request-symlink', 'request-duplicate', 'policy-duplicate',
    'request-oversize', 'request-invalid', 'relative-output', 'same-output',
    'owner-standard-fd', 'owner-closed', 'owner-short', 'owner-long', 'owner-extra',
    'owner-public-file', 'owner-newline', 'typed-refusal', 'private-exception',
    'invalid-proof', 'foreign-nonce', 'extra-proof', 'proof-oversize',
    'invalid-provision', 'late-result', 'producer-noise', 'proof-noise']


def product():
    found = importlib.util.find_spec('cortex_v2.cli.native')
    assert found is not None, 'packaged native CLI missing'
    return importlib.import_module('cortex_v2.cli.native')


@pytest.mark.parametrize('case', CASES)
def test_native_cli_validates_before_effects_and_publishes_only_bounded_public_schema(case, tmp_path, monkeypatch):
    cli = product()
    host = importlib.import_module('cortex_v2.clients.linux_provisioning')
    custody = importlib.import_module('cortex_v2.clients.native_prerequisite')
    from test_cm2_linux_publication import publication
    host, args, response, store, context, proof, members, connection, descriptor, state, _, _ = publication(tmp_path, monkeypatch)
    tmp_path.chmod(0o700)
    owner = secrets.token_urlsafe(32).encode()
    owner_path = tmp_path/'attached-owner'; owner_path.write_bytes(owner); owner_path.chmod(0o600)
    request = tmp_path/'request.json'; request.write_text(json.dumps(args['create_project'])); request.chmod(0o600)
    policy = tmp_path/'policy.json'; policy.write_text(json.dumps(POLICY)); policy.chmod(0o600)
    nonce='0123456789abcdef0123456789abcdef'
    proof.pop('status'); proof.update(schema='cortex.prerequisite-proof.v2', nonce=nonce, descriptor_sha256='a'*64)
    receipt={'status':'READY','operation_id':response['operation_id'],'installation_id':context['installation_id'],
        'principal_id':members['console']['principal_id'],'actor_id':members['console']['actor_id'],
        'scope_id':members['console']['scope_id'],'connection_file':str(connection),'descriptor_file':str(descriptor),
        'expires_at':response['console']['expires_at']}
    events=[]; start=time.monotonic()
    def read(desc, challenge, **kw):
        events.append('proof'); assert desc==descriptor and challenge==nonce
        assert set(kw)=={'deadline'} and 0 < kw['deadline']-start <=30.1
        if case=='typed-refusal': raise custody.PrerequisiteRefusal('cortex_credential_refused',http_status=403)
        if case=='private-exception': raise RuntimeError(owner.decode())
        if case=='proof-noise': print(owner.decode()); print(owner.decode(),file=__import__('sys').stderr)
        value=copy.deepcopy(proof)
        if case=='invalid-proof': value['schema']='foreign'
        if case=='foreign-nonce': value['nonce']='b'*32
        if case=='extra-proof': value['credential']=owner.decode()
        if case=='proof-oversize': value['images']['api']['payload_manifest_sha256']='a'*70000
        if case=='late-result': monkeypatch.setattr(cli.time,'monotonic',lambda:start+31)
        return value
    def provision(root, incoming, **kw):
        events.append('provision'); assert root==tmp_path and incoming['create_project']==args['create_project']
        assert incoming['idempotency_key']=='explicit-intent' and incoming['operator_explicit'] is True and incoming['manager_explicit'] is True
        assert kw['owner_token']==owner and kw['kos_policy']==POLICY and kw['connection_file']==connection and kw['descriptor_file']==descriptor
        assert set(kw)=={'owner_token','kos_policy','connection_file','descriptor_file','deadline'} and 0 < kw['deadline']-start <=30.1
        if case=='producer-noise': print(owner.decode()); print(owner.decode(),file=__import__('sys').stderr)
        value=copy.deepcopy(receipt)
        if case=='invalid-provision': value['owner_token']=owner.decode()
        return value
    monkeypatch.setattr(host,'read_linux_prerequisite_proof',read); monkeypatch.setattr(host,'provision_linux',provision)
    monkeypatch.setattr(cli.platform,'system',lambda:'Darwin' if case=='mac' else 'Linux')
    real_uid=os.getuid();monkeypatch.setattr(cli.os,'getuid',lambda:0 if case=='root' else real_uid)
    human_calls=[]
    def human(argv, *, out, err): human_calls.append(argv);out.write('PUBLIC HUMAN\n');return 0
    monkeypatch.setattr(cli.keys,'human_main',human)
    is_provision=case in ('provision','request-public','request-symlink','request-duplicate','policy-duplicate','request-oversize',
        'request-invalid','relative-output','same-output','owner-standard-fd','owner-closed','owner-short','owner-long','owner-extra',
        'owner-public-file','owner-newline','invalid-provision','producer-noise')
    if case=='request-public': request.chmod(0o644)
    if case=='request-symlink': saved=tmp_path/'saved-request';request.rename(saved);request.symlink_to(saved)
    if case=='request-duplicate': request.write_bytes(b'{"source_project":null,"source_project":null}')
    if case=='policy-duplicate': policy.write_bytes(b'{"minimum_version":"6.0.2","minimum_version":"6.0.2"}')
    if case=='request-oversize': request.write_bytes(b' '*65537)
    if case=='request-invalid': value=copy.deepcopy(args['create_project']);value['source_project']='old';request.write_text(json.dumps(value))
    if case=='owner-short':owner_path.write_bytes(owner[:-1])
    if case=='owner-long':owner_path.write_bytes(owner+b'a'*1000)
    if case=='owner-extra':owner_path.write_bytes(owner+b'\nmore')
    if case=='owner-newline':owner_path.write_bytes(owner+b'\n')
    if case=='owner-public-file':owner_path.chmod(0o644)
    fd=os.open(owner_path,os.O_RDONLY)
    try:
        argv=['provision-console','--runtime-root',str(tmp_path),'--request',str(request),'--kos-policy',str(policy),
            '--owner-fd',str(fd),'--idempotency-key','explicit-intent','--connection',str(connection),'--descriptor',str(descriptor)] if is_provision else ['prerequisite-proof','--descriptor',str(descriptor),'--nonce',nonce]
        if case=='legacy': argv=['status','--installation','public-fixture']
        if case=='unknown-flag':argv+=['--PRIVATE',owner.decode()]
        if case=='duplicate-flag':argv+=['--nonce',nonce]
        if case=='missing-flag':argv=argv[:-2]
        if case=='relative-descriptor':argv[2]='relative.json'
        if case=='bad-nonce':argv[-1]='0'*31
        if case=='nonce-uppercase':argv[-1]='A'*32
        if case=='nonce-newline':argv[-1]+='\n'
        if case=='relative-output':argv[argv.index('--connection')+1]='relative.json'
        if case=='same-output':argv[argv.index('--connection')+1]=str(descriptor)
        if case=='owner-standard-fd':argv[argv.index('--owner-fd')+1]='0'
        if case=='owner-closed':closed=os.dup(fd);os.close(closed);argv[argv.index('--owner-fd')+1]=str(closed)
        monkeypatch.setenv('CORTEX_OWNER_TOKEN',owner.decode());monkeypatch.setenv('CORTEX_API_URL','http://foreign.invalid')
        out=io.StringIO();err=io.StringIO();rc=cli.main(argv,out=out,err=err)
        os.fstat(fd) # the caller's attached descriptor stays open
    finally:os.close(fd)
    assert owner.decode() not in out.getvalue()+err.getvalue()
    valid=case in ('proof','provision','owner-newline','producer-noise','proof-noise')
    if case=='legacy':assert rc==0 and out.getvalue()=='PUBLIC HUMAN\n' and err.getvalue()=='' and human_calls==[argv] and events==[]
    elif valid:
        assert rc==0 and err.getvalue()=='' and out.getvalue().endswith('\n') and out.getvalue().count('\n')==1
        assert len(out.getvalue().encode())<=65536
        assert json.loads(out.getvalue())==(receipt if is_provision else proof)
        assert events==['provision' if is_provision else 'proof']
    else:
        assert rc==2 and out.getvalue()=='' and len(err.getvalue().encode())<=4096 and err.getvalue().count('\n')==1
        value=json.loads(err.getvalue());assert set(value)<= {'code','safe_message','install_guide','utc','http_status_if_observed'}
        assert value['code'].startswith('cortex_') and value['install_guide']=='INSTALL-linux.md'
        assert set(value)>= {'code','safe_message','install_guide','utc'}
        if case=='typed-refusal': assert value['http_status_if_observed']==403
        if case not in ('typed-refusal','private-exception','invalid-proof','foreign-nonce','extra-proof','proof-oversize','invalid-provision','late-result'):assert events==[]


@pytest.mark.parametrize('case',['pipe-valid','pipe-held-eof','pipe-oversize'])
def test_owner_attached_pipe_is_finite_and_keeps_the_callers_fd(case):
    cli=product();read,write=os.pipe();owner=secrets.token_urlsafe(32).encode()
    try:
        os.write(write,owner if case!='pipe-oversize' else owner+b'a'*1000)
        if case=='pipe-valid':os.close(write);write=None
        start=time.monotonic()
        if case=='pipe-valid':assert cli._read_owner(read,start+0.15)==owner
        else:
            with pytest.raises(Exception) as caught:cli._read_owner(read,start+0.15)
            assert owner.decode() not in str(caught.value)
        assert time.monotonic()-start<1 and os.fstat(read)
    finally:
        os.close(read)
        if write is not None:os.close(write)


def test_freezer_entry_and_project_script_select_the_new_cli_without_editing_human_source():
    product()
    import tomllib
    root=Path(__file__).parents[1]
    assert tomllib.loads((root/'pyproject.toml').read_text())['project']['scripts']['cortex']=='cortex_v2.cli.native:main'
    text=(root/'scripts/release/cortex_native.py').read_text()
    assert 'from cortex_v2.cli.native import main' in text and 'raise SystemExit(main())' in text
