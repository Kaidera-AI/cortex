"""Author findings: real private custody, scoped RLS model and composed work counts."""
import asyncio
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import secrets
import sys
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
import uuid

import pytest

from test_cm2_linux_native_adapter import CatalogConnection, INSTALLATION
from test_cm2_linux_installation_binding import fixture as runtime_fixture
from test_cm2_linux_whole_readiness import setup
from test_cm2_linux_delivery_journal import replay
from test_cm2_linux_engine_observation import POLICY


def required():
    host = importlib.import_module('cortex_v2.clients.linux_provisioning')
    native = importlib.import_module('cortex_v2.native_prerequisite')
    custody = host.custody
    assert callable(getattr(native, '_select_readiness_scope', None)), 'CM2 author repair contract missing'
    assert callable(getattr(custody, '_signature_verifier', None)), 'CM2 author repair contract missing'
    assert callable(getattr(host, 'runtime_observation_scope', None)), 'CM2 author repair contract missing'
    return host, native, custody


@pytest.mark.parametrize('case', ['valid', 'unreadable', 'unavailable', 'wrong-kind', 'wrong-scope', 'wrong-installation'])
def test_actual_authentication_and_read_scope_resolution_precede_forced_RLS_catalog(case, monkeypatch):
    _, native, _ = required()
    class Connection(CatalogConnection):
        def __init__(self): super().__init__(); self.config = {}; self.closed = False; self.actions = []
        @asynccontextmanager
        async def transaction(self, **kw):
            assert kw == {'readonly': True}; self.actions.append('transaction'); yield
        async def execute(self, sql, value):
            assert sql.startswith('SELECT set_config(') and ', true)' in sql
            name = sql.split("'")[1]; self.config[name] = value; self.actions.append(name)
        async def fetchrow(self, sql, *args):
            if 'cortex_auth.authenticate' in sql:
                self.actions.append('authenticate')
                return {'principal_id': uuid.UUID('70000000-0000-4000-8000-000000000007'),
                        'installation_id': uuid.UUID(INSTALLATION if case != 'wrong-installation' else '90000000-0000-4000-8000-000000000009'),
                        'expires_at': None, 'is_expired': False}
            if 'project_registry' in sql:
                self.actions.append('project-RLS')
                # The real FORCE RLS policy requires the transaction-local read scope.
                if self.project['scope_id'] not in self.config.get('cortex.read_scope_ids', '').split(','): return None
            return await super().fetchrow(sql, *args)
        async def fetch(self, sql, *args):
            if 'scope_grants AS g' in sql:
                self.actions.append('resolve-scopes'); assert args[1] == ['kos-primary']
                if case == 'unavailable': return []
                return [{'alias': 'kos-primary', 'scope_id': uuid.UUID(self.project['scope_id'] if case != 'wrong-scope' else '90000000-0000-4000-8000-000000000009'),
                         'scope_kind': 'shared' if case == 'wrong-kind' else 'project',
                         'can_read': case != 'unreadable', 'can_write': True, 'can_publish': False}]
            return await super().fetch(sql, *args)
        async def close(self): self.closed = True
        def terminate(self): self.closed = True
    db = Connection(); credential = secrets.token_urlsafe(32)
    settings = SimpleNamespace(database_url='PRIVATE_SYNTHETIC_DSN', token_pepper=b'PRIVATE_SYNTHETIC_PEPPER')
    async def connect(dsn, **kw): assert dsn == settings.database_url; return db
    frame = {'mode': 'readiness', 'installation_id': INSTALLATION, 'credential': credential,
             'project': 'kos-primary', 'project_root': '/physical/project',
             'migrations': {'0001_core.sql': 'a' * 64},
             'expected_relations': [{k: r[k] for k in ('name', 'rls', 'forced', 'app_direct_grant')} for r in db.relations]}
    if case == 'valid':
        value = asyncio.run(native.execute_private(frame, settings=settings, connect=connect))
        assert value['project_binding'] == db.project
        assert db.actions.index('authenticate') < db.actions.index('resolve-scopes') < db.actions.index('project-RLS')
        assert db.config['cortex.write_scope_id'] == ''
        assert db.config['cortex.read_scope_ids'] == db.project['scope_id']
    else:
        with pytest.raises(native.NativeRefusal): asyncio.run(native.execute_private(frame, settings=settings, connect=connect))
    assert db.closed and credential not in str(db.queries) and settings.database_url not in str(db.queries)


@pytest.mark.parametrize('case', ['valid', 'bad-digest', 'linked', 'hardlink', 'writable', 'wrong-owner', 'parent-public', 'changed-tool', 'changed-parent', 'ambient-unavailable'])
def test_signature_verifier_is_pinned_held_and_sterile_with_no_ambient_fallback(case, tmp_path, monkeypatch):
    _, _, custody = required(); tmp_path.chmod(0o700)
    manifest, signature = tmp_path / 'release.json', tmp_path / 'signature'
    for path in (manifest, signature): path.write_bytes(b'PUBLIC fixture'); path.chmod(0o600)
    tools = tmp_path / 'tools'; tools.mkdir(mode=0o700)
    tool = tools / 'minisign'; tool.write_bytes(b'PUBLIC verifier fixture'); tool.chmod(0o500)
    digest = hashlib.sha256(tool.read_bytes()).hexdigest()
    selected = (str(tool), os.getuid(), digest, ())
    monkeypatch.setattr(custody, '_MINISIGN_VERIFIERS', {sys.platform: selected})
    monkeypatch.setattr(custody.shutil, 'which', lambda *a: pytest.fail('ambient PATH verifier was selected'))
    monkeypatch.setenv('LD_PRELOAD', 'PRIVATE-UNTRUSTED'); monkeypatch.setenv('DYLD_INSERT_LIBRARIES', 'PRIVATE-UNTRUSTED')
    calls = []
    if case == 'bad-digest': monkeypatch.setattr(custody, '_MINISIGN_VERIFIERS', {sys.platform: (str(tool), os.getuid(), '0' * 64, ())})
    if case == 'linked': original = tools / 'original'; tool.rename(original); tool.symlink_to(original)
    if case == 'hardlink': os.link(tool, tools / 'alias')
    if case == 'writable': tool.chmod(0o522)
    if case == 'wrong-owner': monkeypatch.setattr(custody, '_MINISIGN_VERIFIERS', {sys.platform: (str(tool), os.getuid() + 1, digest, ())})
    if case == 'parent-public': tools.chmod(0o777)
    if case == 'ambient-unavailable': monkeypatch.setattr(custody, '_MINISIGN_VERIFIERS', {})
    def run(argv, **kw):
        calls.append(True); assert len(kw['pass_fds']) == 1
        fd = kw['pass_fds'][0]; assert argv[0].endswith('/' + str(fd))
        assert os.read(fd, 65536) == b'PUBLIC verifier fixture'; os.lseek(fd, 0, os.SEEK_SET)
        assert kw['env'] == {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'}
        assert kw['stdin'] == custody.subprocess.DEVNULL and kw['close_fds'] is True
        assert 'PRIVATE-UNTRUSTED' not in str(kw) and 0 < kw['timeout'] <= 5
        if case == 'changed-tool': tool.write_bytes(b'CHANGED verifier fixture')
        if case == 'changed-parent':
            temporary = tools / 'extra'; temporary.write_bytes(b'x'); temporary.unlink()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(custody.subprocess, 'run', run)
    if case == 'valid': custody.verify_signature(manifest, signature)
    else:
        with pytest.raises(custody.PrerequisiteRefusal) as error: custody.verify_signature(manifest, signature)
        assert error.value.code == 'cortex_release_signature_invalid'
        assert 'PRIVATE-UNTRUSTED' not in str(error.value)
    assert len(calls) == (1 if case in ('valid', 'changed-tool', 'changed-parent') else 0)


def actual_runtime(tmp_path, monkeypatch):
    host, _, custody = required()
    _, _, root, package, release, manifest, record, record_path, signatures, _ = runtime_fixture(tmp_path, monkeypatch)
    calls = []; local = {'provider': 'linuxbrew', 'linked_keg': '6.1.3', 'executable': '/PUBLIC/podman', 'environment': {}, 'identity': (1, 2)}
    monkeypatch.setattr(host, '_local_engine_context', lambda: copy.deepcopy(local))
    monkeypatch.setattr(host, 'observe_linux_engine', lambda **kw: {'PUBLIC': 'engine'})
    def inspect(command, **kw):
        kind, target = command[2], command[-1]; calls.append((kind, target))
        if kind == 'container':
            role = next(r for r in custody.ROLES if target == record['namespace'] + '_' + r)
            return {'name': target, 'id': format(list(custody.ROLES).index(role) + 1, '064x'),
                    'image_id': release['images'][role]['config_id'].removeprefix('sha256:'),
                    'owner': record['installation'], 'deployment_class': 'TEST', 'running': True,
                    'networks': {record['namespace'] + '_net': {}},
                    'ports': {'8601/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(record['port'])}]} if role == 'api' else None}
        if kind == 'image':
            image = next(v for v in release['images'].values() if v['config_id'] == target)
            return {'id': target.removeprefix('sha256:'), 'digest': image['manifest_digest'], 'os': 'linux',
                    'architecture': 'amd64', 'source': release['source_revision'],
                    'version': release['release_id'].removeprefix('v'), 'deployment_class': 'TEST'}
        return {'name': target, 'id': 'a' * 64, 'owner': record['installation'], 'internal': True}
    monkeypatch.setattr(host, '_engine_json', inspect)
    return host, custody, root, package, release, manifest, record, record_path, signatures, calls, local


@pytest.mark.parametrize('case', ['valid', 'helper', 'archive', 'signature', 'record', 'same-size-mtime', 'extra-payload', 'root', 'policy', 'deadline', 'final-runtime'])
def test_one_operation_bounds_full_runtime_measurements_and_detects_custody_changes(case, tmp_path, monkeypatch):
    host, custody, root, package, release, manifest, record, record_path, signatures, calls, local = actual_runtime(tmp_path, monkeypatch)
    policy = {'minimum_version': '6.0.2'}; deadline = time.monotonic() + 5
    def operation():
        with host.runtime_observation_scope(root, kos_policy=policy, deadline=deadline):
            initial = host.read_linux_runtime(root, kos_policy=policy, deadline=deadline)
            for _ in range(189): assert host.read_linux_runtime(root, kos_policy=policy, deadline=deadline) == initial
            assert len(signatures) == 2 and len(calls) == 11
            paths = {'helper': package / 'bin/cortex', 'archive': root / 'signed' / release['archive']['name'],
                     'signature': root / 'signed/release.json.minisig', 'record': record_path}
            if case in paths:
                path = paths[case]; replacement = root / 'replacement'; replacement.write_bytes(path.read_bytes())
                replacement.chmod(path.stat().st_mode & 0o777); replacement.replace(path)
            if case == 'same-size-mtime':
                path = package / 'bin/cortex'; before = path.stat(); path.write_bytes(b'x' * before.st_size)
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            if case == 'extra-payload': extra = package / 'extra'; extra.write_bytes(b'x'); extra.chmod(0o600)
            if case == 'root': host.read_linux_runtime(tmp_path, kos_policy=policy, deadline=deadline)
            if case == 'policy': host.read_linux_runtime(root, kos_policy={'minimum_version': '9.0.0'}, deadline=deadline)
            if case == 'deadline': monkeypatch.setattr(host.time, 'monotonic', lambda: deadline + 1)
            if case == 'final-runtime':
                old = host._engine_json
                def changed(argv, **kw):
                    value = old(argv, **kw)
                    if argv[2] == 'network': value['id'] = 'b' * 64
                    return value
                monkeypatch.setattr(host, '_engine_json', changed)
            host.read_linux_runtime(root, kos_policy=policy, deadline=deadline)
    if case == 'valid':
        operation(); assert len(signatures) == 4 and len(calls) == 22
        # The observation scope is closed; a later operation must measure again.
        host.read_linux_runtime(root, kos_policy=policy, deadline=deadline)
        assert len(signatures) == 6 and len(calls) == 33
    else:
        with pytest.raises(custody.PrerequisiteRefusal): operation()


def test_concrete_recipient_rechecks_and_whole_readiness_use_two_full_runtime_reads(tmp_path, monkeypatch):
    host, custody, root, package, release, manifest, record, record_path, signatures, calls, local = actual_runtime(tmp_path, monkeypatch)
    journal, args, response, store, context, whole_local, provider, security, catalog, members = setup(host, tmp_path, monkeypatch)
    record['installation'] = journal.installation
    record['namespace'] = 'cortex_v2_package_test_' + uuid.UUID(journal.installation).hex
    record['service_label'] = 'ai.kaidera.cortex.TEST-v2.' + uuid.UUID(journal.installation).hex
    record_path.write_text(json.dumps(record)); record_path.chmod(0o600)
    local.update(whole_local)
    monkeypatch.setattr(host, 'observe_linux_engine', lambda **kw: copy.deepcopy(context['engine']))
    monkeypatch.setattr(host, 'observe_linuxbrew_provider', lambda **kw: copy.deepcopy(provider))
    monkeypatch.setattr(host, 'read_linux_host_security', lambda *a, **kw: copy.deepcopy(security))
    monkeypatch.setattr(host, 'read_linux_catalog', lambda *a, **kw: copy.deepcopy(catalog))
    reads = []
    class Transport:
        def __init__(self, origin): assert origin == context['origin']
        def get(self, url, *, headers, timeout, max_bytes):
            role = next(r for r in ('lead', 'console') if headers['Authorization'] == 'Bearer ' + response[r + '_token'])
            reads.append(role)
            if url.endswith('/health/ready'): value = {'status': 'ready'}
            elif url.endswith('/v1/auth/principal'):
                value = {'data': {'installation_id': journal.installation, 'principal_id': response[role]['principal_id'],
                         'scopes': [{'scope_id': response['project_id'], 'primary_alias': 'fresh', 'scope_kind': 'project',
                                     'can_read': True, 'can_write': True, 'can_publish': role == 'lead'}]}}
            else:
                member = members[role]
                value = {'data': {'scope_id': response['project_id'], 'roster_revision': 3,
                         'entries': [{'actor_id': member['actor_id'], 'principal_id': member['principal_id'],
                                      'display_name': member['member_name'], 'actor_kind': member['actor_kind'],
                                      'role': member['role'], 'status': 'active'}]}}
            return 200, json.dumps(value).encode()
    monkeypatch.setattr(host, 'LoopbackTransport', Transport)
    deadline = time.monotonic() + 10
    with host.runtime_observation_scope(root, kos_policy=POLICY, deadline=deadline):
        with host.admitted_recipients(root, args, replay(response), kos_policy=POLICY, journal=journal, deadline=deadline) as recheck:
            proof = host.read_linux_readiness(root, args, replay(response), kos_policy=POLICY, store=store,
                                             recheck_recipients=recheck, deadline=deadline)
            assert proof['status'] == 'READY' and proof['installation_id'] == journal.installation
            assert len(signatures) == 2 and len(calls) == 11
    assert len(signatures) == 4 and len(calls) == 22
    assert len(reads) >= 60  # Both real recipient HTTP admissions remain fresh.
