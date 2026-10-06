"""Private publication filesystem effects; native readiness is intercepted explicitly."""
import copy
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
import secrets
import stat
import time

import pytest
from test_cm2_linux_whole_readiness import setup
from test_cm2_linux_engine_observation import POLICY


def required():
    module = importlib.import_module('cortex_v2.clients.linux_provisioning')
    assert callable(getattr(module, 'publish_linux_prerequisite', None)), 'private Linux publication missing'
    assert callable(getattr(module, '_publish_private_once', None)), 'private Linux publication missing'
    return module


def publication(tmp_path, monkeypatch):
    module = required()
    journal, args, response, store, context, local, provider, security, catalog, members = setup(module, tmp_path, monkeypatch)
    context['runtime_root'] = str(tmp_path)
    proof = json.loads((Path(__file__).parent / 'fixtures/cm2-revision4/fixtures/local-proof.linux.json').read_text())
    proof.pop('nonce'); proof.pop('descriptor_sha256')
    proof['schema'] = 'cortex.linux-readiness.v1'; proof['status'] = 'READY'
    proof['installation_id'] = journal.installation
    proof['release_manifest_sha256'] = context['release_manifest_sha256']
    proof['helper_sha256'] = context['helper_sha256']
    proof['api_binding']['loopback_port'] = 18602
    proof['project_binding'] = catalog['project_binding']
    snapshots = {role: store.read('fresh', args['create_project']['lead_name'] if role == 'lead' else 'console') for role in ('lead', 'console')}
    calls = []

    def recipients():
        calls.append('members')
        for role, record in snapshots.items():
            current = store.read('fresh', args['create_project']['lead_name'] if role == 'lead' else 'console')
            if current is None or current.metadata != record.metadata or not hmac.compare_digest(current.token, record.token):
                raise module.ProvisionRefusal('cortex_credential_refused')
        return copy.deepcopy(members)

    monkeypatch.setattr(module, 'read_linux_runtime', lambda root, **kw: copy.deepcopy(context))
    monkeypatch.setattr(module, 'read_linux_readiness', lambda *a, **kw: copy.deepcopy(proof))
    connection, descriptor = tmp_path / 'connection.json', tmp_path / 'descriptor.json'
    state = tmp_path / 'descriptor.json.state.json'

    def call(**changes):
        options = dict(kos_policy=POLICY, store=store, recheck_recipients=recipients,
                       connection_file=connection, descriptor_file=descriptor, deadline=time.monotonic() + 5)
        options.update(changes)
        return module.publish_linux_prerequisite(tmp_path, args, response, copy.deepcopy(proof), **options)

    return module, args, response, store, context, proof, members, connection, descriptor, state, calls, call


def test_actual_private_bytes_order_no_secret_and_byte_identical_resume(tmp_path, monkeypatch):
    module, args, response, store, context, proof, members, connection, descriptor, state, calls, call = publication(tmp_path, monkeypatch)
    links = []; original = os.link
    def link(source, target, **kw):
        links.append(target); return original(source, target, **kw)
    monkeypatch.setattr(os, 'link', link)
    result = call()
    assert result == {'connection_file': str(connection), 'descriptor_file': str(descriptor)}
    assert links == [state.name, connection.name, descriptor.name]
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
    monkeypatch.setattr(os, 'link', lambda *a, **kw: pytest.fail('resume published a replacement'))
    assert call() == result
    assert before == {p: (p.read_bytes(), module.custody._file_identity(p.stat())) for p in before}
    assert not list(tmp_path.glob('.*'))


@pytest.mark.parametrize('field,value', [('schema', 'foreign'), ('status', 'not-ready'), ('installation_id', 'foreign'),
    ('release_manifest_sha256', 'e' * 64), ('target', 'macos-arm64'), ('helper_sha256', 'e' * 64),
    ('signed_helper_verified', False), ('source_payload_matches', False), ('migration_checksums_match', False),
    ('required_rls_enabled_and_forced', False), ('app_role_non_superuser_without_bypassrls', False),
    ('app_role_not_migrator_or_table_owner', False), ('database_instance_matches', False), ('cached', True)])
def test_wrong_stale_or_extended_proof_refuses_before_any_publication(field, value, tmp_path, monkeypatch):
    module, args, response, store, context, proof, members, connection, descriptor, state, calls, call = publication(tmp_path, monkeypatch)
    original = copy.deepcopy(proof); proof[field] = value
    monkeypatch.setattr(module, 'read_linux_readiness', lambda *a, **kw: original)
    with pytest.raises(module.PrerequisiteRefusal): call()
    assert not any(p.exists() for p in (state, connection, descriptor))


@pytest.mark.parametrize('case', ['symlink', 'hardlink', 'public', 'directory', 'duplicate', 'conflict', 'parent-public', 'parent-linked', 'same-path', 'state-path', 'package-path'])
@pytest.mark.parametrize('which', ['connection', 'descriptor'])
def test_unsafe_or_overlapping_output_refuses_before_partial_state(case, which, tmp_path, monkeypatch):
    module, args, response, store, context, proof, members, connection, descriptor, state, calls, call = publication(tmp_path, monkeypatch)
    selected = connection if which == 'connection' else descriptor
    if case == 'symlink':
        other = tmp_path / 'other'; other.write_bytes(b'{}'); other.chmod(0o600); selected.symlink_to(other)
    elif case in ('hardlink', 'public', 'duplicate', 'conflict'):
        selected.write_bytes(b'{"schema":1,"schema":2}' if case == 'duplicate' else b'{"foreign":true}')
        selected.chmod(0o644 if case == 'public' else 0o600)
        if case == 'hardlink': os.link(selected, tmp_path / 'other')
    elif case == 'directory': selected.mkdir(mode=0o700)
    changes = {}
    if case in ('parent-public', 'parent-linked'):
        parent = tmp_path / 'parent'; parent.mkdir(mode=0o755 if case == 'parent-public' else 0o700)
        if case == 'parent-linked':
            alias = tmp_path / 'alias'; alias.symlink_to(parent); parent = alias
        changes[which + '_file'] = parent / 'output.json'
    if case == 'same-path': changes[which + '_file'] = descriptor if which == 'connection' else connection
    if case == 'state-path': changes[which + '_file'] = state
    if case == 'package-path':
        package = tmp_path / 'package'; package.mkdir(mode=0o700); changes[which + '_file'] = package / 'release.json'
    before = selected.read_bytes() if selected.is_file() else None
    with pytest.raises(module.PrerequisiteRefusal): call(**changes)
    assert not state.exists()
    if before is not None: assert selected.read_bytes() == before
    assert not list(tmp_path.glob('.*'))


@pytest.mark.parametrize('role', ['lead', 'console'])
def test_changed_real_private_recipient_refuses_before_file_effects(role, tmp_path, monkeypatch):
    module, args, response, store, context, proof, members, connection, descriptor, state, calls, call = publication(tmp_path, monkeypatch)
    label = args['create_project']['lead_name'] if role == 'lead' else 'console'
    store.put('fresh', label, secrets.token_urlsafe(32), managed_by=response[role]['manager'], expires_at=response[role]['expires_at'])
    with pytest.raises(module.PrerequisiteRefusal): call()
    assert not any(p.exists() for p in (state, connection, descriptor))


@pytest.mark.parametrize('deadline', [True, float('nan'), float('inf'), 0, -1])
def test_invalid_deadline_refuses_without_readiness(deadline, tmp_path, monkeypatch):
    module, args, response, store, context, proof, members, connection, descriptor, state, calls, call = publication(tmp_path, monkeypatch)
    monkeypatch.setattr(module, 'read_linux_readiness', lambda *a, **kw: pytest.fail('invalid deadline reached readiness'))
    with pytest.raises(module.PrerequisiteRefusal): call(deadline=deadline)
    assert not any(p.exists() for p in (state, connection, descriptor))


@pytest.mark.parametrize('stop', ['state', 'connection', 'descriptor', 'end-readiness'])
def test_interruption_preserves_safe_partial_resume_and_never_ready(stop, tmp_path, monkeypatch):
    module, args, response, store, context, proof, members, connection, descriptor, state, calls, call = publication(tmp_path, monkeypatch)
    original = os.link; linked = []; reads = []
    def link(source, target, **kw):
        if target == {'state': state.name, 'connection': connection.name, 'descriptor': descriptor.name}.get(stop):
            raise OSError('PRIVATE-SYNTHETIC-INTERRUPTION')
        result = original(source, target, **kw); linked.append(target); return result
    def readiness(*a, **kw):
        reads.append(True)
        if stop == 'end-readiness' and len(reads) > 1:
            changed = copy.deepcopy(proof); changed['selinux'] = 'Permissive'; return changed
        return copy.deepcopy(proof)
    monkeypatch.setattr(os, 'link', link); monkeypatch.setattr(module, 'read_linux_readiness', readiness)
    with pytest.raises(module.PrerequisiteRefusal) as error: call()
    assert 'PRIVATE-SYNTHETIC' not in str(error.value) and not descriptor.exists()
    assert not list(tmp_path.glob('.*'))
    before = {p: (p.read_bytes(), p.stat().st_ino) for p in (state, connection) if p.exists()}
    monkeypatch.setattr(os, 'link', original)
    monkeypatch.setattr(module, 'read_linux_readiness', lambda *a, **kw: copy.deepcopy(proof))
    assert call() == {'connection_file': str(connection), 'descriptor_file': str(descriptor)}
    assert before == {p: (p.read_bytes(), p.stat().st_ino) for p in before}


@pytest.mark.parametrize('operation', ['link-race', 'fsync', 'write', 'recheck', 'parent-change'])
def test_private_once_races_and_io_failures_close_owned_fds_and_never_overwrite(operation, tmp_path, monkeypatch):
    module = required(); tmp_path.chmod(0o700); target = tmp_path / 'value.json'
    original_open, original_close, original_link = os.open, os.close, os.link
    opened = set(); closed = set(); checks = []
    def opening(*a, **kw):
        fd = original_open(*a, **kw); opened.add(fd); return fd
    def closing(fd): closed.add(fd); return original_close(fd)
    monkeypatch.setattr(os, 'open', opening); monkeypatch.setattr(os, 'close', closing)
    if operation == 'link-race':
        def link(source, destination, **kw):
            target.write_bytes(b'{"foreign":true}'); target.chmod(0o600)
            return original_link(source, destination, **kw)
        monkeypatch.setattr(os, 'link', link)
    if operation in ('fsync', 'write'):
        monkeypatch.setattr(os, operation, lambda *a: (_ for _ in ()).throw(OSError('PRIVATE-SYNTHETIC-IO')))
    def check():
        checks.append(True)
        if operation == 'recheck' and len(checks) >= 2: raise RuntimeError('PRIVATE-SYNTHETIC-CHECK')
        if operation == 'parent-change' and len(checks) >= 2: tmp_path.chmod(0o755)
    with pytest.raises(module.PrerequisiteRefusal) as error: module._publish_private_once(target, {'public': True}, check)
    assert 'PRIVATE-SYNTHETIC' not in str(error.value) and opened <= closed
    if operation == 'link-race': assert target.read_bytes() == b'{"foreign":true}'
    else: assert not target.exists()
    assert not list(tmp_path.glob('.*'))
