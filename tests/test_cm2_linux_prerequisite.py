"""R221 observed version and literal private custody; no native provider calls."""
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path

import pytest

HOME = Path(__file__).parent / 'fixtures/cm2-revision4'
FREEZE = 'e96b69c53e42e97c88bfc2bfc438a78a6d59462be07f8501c7e11730f0590537'
POLICY = json.loads((HOME / 'contract.json').read_text())['podman']

def product():
    path = Path(__file__).parents[1] / 'src/cortex_v2/clients/native_prerequisite.py'
    assert path.is_file(), 'accepted CM2 native prerequisite reader is absent'
    return importlib.import_module('cortex_v2.clients.native_prerequisite')

def report(client='6.1.3', server=None):
    value = {'Client': {'Version': client}}
    if server is not None:
        value['Server'] = {'Version': server}
    return json.dumps(value).encode()

@pytest.mark.parametrize('host,client,server', [('linux','6.0.2',None),('linux','6.1.3',None),('linux','7.0.0',None),('linux','8.1.0',None),('macos','6.0.2','6.0.2'),('macos','6.1.3','7.0.0'),('macos','7.0.0','7.0.1')])
def test_only_actual_required_components_have_no_ceiling(host, client, server):
    p = product()
    connection = None if host == 'linux' else 'podman-machine-default'
    value = p.parse_podman_report(report(client,server), host_os=host, mode='local' if host=='linux' else 'remote', connection_name=connection, expected_connection_name=connection, cortex_policy=POLICY, kos_policy=POLICY)
    assert value['client_version'] == client and value['server_version'] == server
    assert value['mode'] == ('local' if host=='linux' else 'remote')
    assert value['connection_name'] == connection

INVALID = [
    ('linux', report('6.0.1'), 'local', None, None),
    ('linux', report('5.9.9'), 'local', None, None),
    ('linux', report('6.1.0-rc1'), 'local', None, None),
    ('linux', report('garbage'), 'local', None, None),
    ('linux', report(), 'remote', None, None),
    ('linux', report(server='6.1.3'), 'local', None, None),
    ('linux', report(), 'local', 'foreign', None),
    ('linux', b'{}', 'local', None, None),
    ('linux', b'{"Client":null}', 'local', None, None),
    ('linux', b'[]', 'local', None, None),
    ('linux', b'{"Client":{"Version":613}}', 'local', None, None),
    ('linux', b'{"Client":{"Version":"6.1.3","Version":"6.0.1"}}', 'local', None, None),
    ('linux', b'\xff', 'local', None, None),
    ('linux', report()+b'{}', 'local', None, None),
    ('linux', b' '*65537, 'local', None, None),
    ('macos', report(), 'remote', 'named', 'named'),
    ('macos', report(server='6.0.1'), 'remote', 'named', 'named'),
    ('macos', report(server='invalid'), 'remote', 'named', 'named'),
    ('macos', report('6.0.1','6.1.3'), 'remote', 'named', 'named'),
    ('macos', report(server='6.1.3'), 'local', 'named', 'named'),
    ('macos', report(server='6.1.3'), 'remote', 'foreign', 'named'),
    ('macos', report(server='6.1.3'), 'remote', None, None),
]

@pytest.mark.parametrize('host,raw,mode,connection,expected', INVALID)
def test_unobserved_or_malformed_or_wrong_mode_versions_refuse(host,raw,mode,connection,expected):
    p=product()
    with pytest.raises(p.PrerequisiteRefusal) as exc:
        p.parse_podman_report(raw,host_os=host,mode=mode,connection_name=connection,expected_connection_name=expected,cortex_policy=POLICY,kos_policy=POLICY)
    assert exc.value.code == 'cortex_podman_unsupported'

@pytest.mark.parametrize('denier,host,component', [('cortex','linux','client'),('kos','linux','client'),('cortex','macos','server'),('kos','macos','server')])
def test_valid_descriptor_cannot_suppress_either_signed_denial(denier,host,component):
    p=product(); policies={'cortex':copy.deepcopy(POLICY),'kos':copy.deepcopy(POLICY)}
    policies[denier]['denylist']['entries']=[{'version':'6.1.99','date':'2026-10-06','reason':'SYNTHETIC test only'}]
    raw=report('6.1.99' if component=='client' else '6.1.3','6.1.99' if component=='server' else None)
    connection=None if host=='linux' else 'named'
    with pytest.raises(p.PrerequisiteRefusal) as exc:
        p.parse_podman_report(raw,host_os=host,mode='local' if host=='linux' else 'remote',connection_name=connection,expected_connection_name=connection,cortex_policy=policies['cortex'],kos_policy=policies['kos'])
    assert exc.value.code=='cortex_podman_denied' and '2026-10-06' in str(exc.value)

@pytest.mark.parametrize('case',['valid','public','symlink','linked-parent','hardlink','duplicate','utf8','trailing','oversized','file-replaced'])
def test_actual_owned_private_file_custody(case,tmp_path,monkeypatch):
    p=product();tmp_path.chmod(0o700);path=tmp_path/'record.json';path.write_bytes(b'{"schema":"fixture"}');path.chmod(0o600)
    if case=='public':path.chmod(0o644)
    if case=='symlink':
        original=tmp_path/'original';path.rename(original);path.symlink_to(original)
    if case=='linked-parent':
        alias=tmp_path.parent/(tmp_path.name+'-alias');alias.symlink_to(tmp_path);path=alias/'record.json'
    if case=='hardlink':os.link(path,tmp_path/'second')
    if case=='duplicate':path.write_bytes(b'{"schema":"fixture","schema":"other"}')
    if case=='utf8':path.write_bytes(b'\xff')
    if case=='trailing':path.write_bytes(b'{}{}')
    if case=='oversized':path.write_bytes(b' '*8193)
    if case=='file-replaced':
        original_open=os.open;replaced=False
        def replace_after_open(name,flags,*args,**kwargs):
            nonlocal replaced
            fd=original_open(name,flags,*args,**kwargs)
            if not replaced and str(name) in (str(path),path.name):
                replaced=True;path.unlink();path.write_bytes(b'{"schema":"replacement"}');path.chmod(0o600)
            return fd
        monkeypatch.setattr(os,'open',replace_after_open)
    if case=='valid':assert p.read_private_json(path,limit=8192)=={'schema':'fixture'}
    else:
        with pytest.raises(p.PrerequisiteRefusal) as exc:p.read_private_json(path,limit=8192)
        assert exc.value.code=='cortex_descriptor_invalid'

def test_all_copied_revision4_files_and_147_outcomes_remain_frozen():
    # The public freeze is the byte authority; product fixtures have no secrets.
    freeze=Path(__file__).parent/'fixtures/cm2-revision4.freeze.json'
    assert hashlib.sha256(freeze.read_bytes()).hexdigest()==FREEZE
    data=json.loads(freeze.read_text())
    assert len(data['files_sha256'])==29
    for rel,digest in data['files_sha256'].items():assert hashlib.sha256((HOME/rel).read_bytes()).hexdigest()==digest
    vectors=json.loads((HOME/'fixtures/cases.json').read_text())
    assert vectors['case_count']==len(vectors['cases'])==147


@pytest.mark.parametrize('raw', [b'{"x":NaN}', b'{"x":Infinity}', b'{"x":{"a":1,"a":2}}',
    b'{"x":1} trailing', b'[]', b'\xff', b' ' * 65537])
def test_strict_json_rejects_ambiguous_or_unbounded_objects(raw):
    p = product()
    with pytest.raises(p.PrerequisiteRefusal): p.strict_json(raw, limit=65536)


def test_owned_fifo_cannot_block_a_private_read(tmp_path):
    p = product(); tmp_path.chmod(0o700); path = tmp_path / 'record.json'
    os.mkfifo(path, 0o600)
    with pytest.raises(p.PrerequisiteRefusal): p.read_private_json(path, limit=8192)
    assert path.is_fifo()


@pytest.mark.parametrize('kind', ['valid', 'unsafe-parent', 'symlink', 'hardlink', 'public-file'])
def test_atomic_private_publication_has_real_custody(kind, tmp_path):
    p = product(); tmp_path.chmod(0o700); path = tmp_path / 'record.json'
    path.write_bytes(b'{"previous":true}\n'); path.chmod(0o600)
    if kind == 'unsafe-parent': tmp_path.chmod(0o777)
    if kind == 'symlink':
        original = tmp_path / 'original'; path.rename(original); path.symlink_to(original)
    if kind == 'hardlink': os.link(path, tmp_path / 'alias')
    if kind == 'public-file': path.chmod(0o644)
    before = path.read_bytes()
    if kind == 'valid':
        p.atomic_private_json(path, {'published': True})
        assert p.read_private_json(path, limit=8192) == {'published': True}
        assert path.stat().st_mode & 0o777 == 0o600
    else:
        with pytest.raises(p.PrerequisiteRefusal): p.atomic_private_json(path, {'published': True})
        assert path.read_bytes() == before
    assert set(x.name for x in tmp_path.iterdir()) <= {'record.json', 'original', 'alias'}


def test_parent_replacement_after_directory_open_refuses_before_write(tmp_path, monkeypatch):
    p = product(); parent = tmp_path / 'private'; parent.mkdir(mode=0o700)
    path = parent / 'record.json'; path.write_bytes(b'{"previous":true}'); path.chmod(0o600)
    original_open = os.open; moved = tmp_path / 'original-parent'; replaced = False
    def replace(name, flags, *args, **kwargs):
        nonlocal replaced
        fd = original_open(name, flags, *args, **kwargs)
        if not replaced and str(name) == str(parent):
            replaced = True; parent.rename(moved); parent.mkdir(mode=0o700)
        return fd
    monkeypatch.setattr(os, 'open', replace)
    with pytest.raises(p.PrerequisiteRefusal): p.atomic_private_json(path, {'published': True})
    assert (moved / 'record.json').read_bytes() == b'{"previous":true}'
    assert list(parent.iterdir()) == []


def member_fixture(tmp_path):
    c = json.loads((HOME / 'fixtures/connection.linux.json').read_text())
    c['project_root'] = str(tmp_path)
    bodies = [b'{"status":"ready"}', (HOME / 'fixtures/principal.linux.json').read_bytes(),
              (HOME / 'fixtures/roster.linux.json').read_bytes()]
    return c, bodies


class LiteralTransport:
    def __init__(self, bodies): self.bodies, self.calls = bodies, []
    def get(self, url, *, headers, timeout, max_bytes):
        self.calls.append((url, dict(headers), timeout, max_bytes))
        return 200, self.bodies[len(self.calls) - 1]


class SnapshotReader:
    def __init__(self): self.calls = 0; self.rotate_at = None; self.fail_at = None
    def headers(self):
        self.calls += 1
        if self.calls == self.fail_at: raise OSError('synthetic private record unavailable')
        return {'Authorization': 'Bearer ' + ('a' if self.calls != self.rotate_at else 'b') * 43,
                'X-Cortex-Scope': 'kos-primary'}


def test_actual_member_read_sequence_is_bounded_and_discards_credentials(tmp_path):
    p = product(); connection, bodies = member_fixture(tmp_path); io = LiteralTransport(bodies); reader = SnapshotReader()
    value = p.read_member_admission(connection, reader=reader, transport=io)
    assert reader.calls == len(io.calls) == 3
    assert [c[0] for c in io.calls] == [connection['origin'] + '/health/ready',
        connection['origin'] + '/v1/auth/principal', connection['origin'] + '/v1/scopes/kos-primary/roster']
    assert all(0 < c[2] <= 5 and c[3] == 65536 for c in io.calls)
    assert value['principal_id'] == connection['principal_id'] and value['actor_id'] == connection['actor_id']
    assert value['can_read'] is True and value['can_write'] is True and value['can_publish'] is False
    assert 'Bearer' not in json.dumps(value) and 'a' * 43 not in json.dumps(value)


@pytest.mark.parametrize('step', [1, 2, 3])
def test_missing_credential_at_every_request_refuses_before_that_transport(step, tmp_path):
    p = product(); connection, bodies = member_fixture(tmp_path); io = LiteralTransport(bodies); reader = SnapshotReader(); reader.fail_at = step
    with pytest.raises(p.PrerequisiteRefusal): p.read_member_admission(connection, reader=reader, transport=io)
    assert reader.calls == step and len(io.calls) == step - 1


@pytest.mark.parametrize('step', [2, 3])
def test_rotation_at_each_later_step_refuses_without_token_hash(step, tmp_path):
    p = product(); connection, bodies = member_fixture(tmp_path); io = LiteralTransport(bodies); reader = SnapshotReader(); reader.rotate_at = step
    with pytest.raises(p.PrerequisiteRefusal) as exc: p.read_member_admission(connection, reader=reader, transport=io)
    assert len(io.calls) == step - 1 and 'a' * 43 not in str(exc.value) and 'b' * 43 not in str(exc.value)


@pytest.mark.parametrize('case', ['principal-installation', 'principal-id', 'two-grants', 'publish-grant',
    'inactive', 'actor', 'principal', 'lead', 'two-console', 'wrong-scope', 'duplicate-json', 'invalid-utf8'])
def test_literal_profile_roster_refusals(case, tmp_path):
    p = product(); connection, bodies = member_fixture(tmp_path)
    principal = json.loads(bodies[1]); roster = json.loads(bodies[2])
    if case == 'principal-installation': principal['data']['installation_id'] = 'foreign'
    elif case == 'principal-id': principal['data']['principal_id'] = 'foreign'
    elif case == 'two-grants': principal['data']['scopes'].append(copy.deepcopy(principal['data']['scopes'][0]))
    elif case == 'publish-grant': principal['data']['scopes'][0]['can_publish'] = True
    elif case == 'inactive': roster['data']['entries'][0]['status'] = 'inactive'
    elif case == 'actor': roster['data']['entries'][0]['actor_id'] = 'foreign'
    elif case == 'principal': roster['data']['entries'][0]['principal_id'] = 'foreign'
    elif case == 'lead': roster['data']['entries'][0]['role'] = 'lead'
    elif case == 'two-console': roster['data']['entries'].append(copy.deepcopy(roster['data']['entries'][0]))
    elif case == 'wrong-scope': roster['data']['scope_id'] = 'foreign'
    bodies[1] = json.dumps(principal).encode(); bodies[2] = json.dumps(roster).encode()
    if case == 'duplicate-json': bodies[1] = b'{"data":{},"data":{}}'
    elif case == 'invalid-utf8': bodies[2] = b'\xff'
    io = LiteralTransport(bodies)
    with pytest.raises(p.PrerequisiteRefusal): p.read_member_admission(connection, reader=SnapshotReader(), transport=io)
    assert len(io.calls) <= 3


@pytest.mark.parametrize('case', ['valid', 'opaque-new-label', 'next-sequence', 'wrong-lineage', 'zero-sequence',
    'boolean-sequence', 'wrong-contract', 'wrong-target', 'missing-helper', 'traversal-archive', 'missing-image',
    'bad-payload-hash', 'extra-field'])
def test_signed_release_compatibility_uses_lineage_contract_sequence_and_bound_bytes(case):
    p = product(); release = json.loads((HOME / 'fixtures/release.linux.json').read_text())
    if case == 'opaque-new-label': release['release_id'] = 'opaque-compatible-new-release'
    elif case == 'next-sequence': release['release_sequence'] = 99
    elif case == 'wrong-lineage': release['release_lineage'] = 'legacy'
    elif case == 'zero-sequence': release['release_sequence'] = 0
    elif case == 'boolean-sequence': release['release_sequence'] = True
    elif case == 'wrong-contract': release['api_contract'] = 'foreign'
    elif case == 'wrong-target': release['target'] = 'macos-arm64'
    elif case == 'missing-helper': release['files'].pop('bin/cortex')
    elif case == 'traversal-archive': release['archive']['name'] = '../foreign.tar.gz'
    elif case == 'missing-image': release['images'].pop('doc')
    elif case == 'bad-payload-hash': release['images']['api']['source_payload_sha256'] = 'bad'
    elif case == 'extra-field': release['trusted_public_key'] = 'descriptor-supplied-trust'
    if case in ('valid', 'opaque-new-label', 'next-sequence'):
        assert p.validate_release_manifest(release, target='linux-x86_64') == release
    else:
        with pytest.raises(p.PrerequisiteRefusal): p.validate_release_manifest(release, target='linux-x86_64')


@pytest.mark.parametrize('case', ['valid', 'signature-failure', 'timeout', 'changed-manifest', 'changed-signature'])
def test_signature_verification_uses_independent_key_and_rechecks_actual_files(case, tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    p = product(); tmp_path.chmod(0o700); manifest = tmp_path / 'release.json'; signature = tmp_path / 'release.json.minisig'
    manifest.write_bytes(b'{"signed":"fixture"}\n'); manifest.chmod(0o600)
    signature.write_bytes(b'SYNTHETIC detached signature'); signature.chmod(0o600)
    key = 'RW' + 'A' * 54; calls = []
    def verify(argv, **kwargs):
        calls.append((argv, kwargs)); assert kwargs['timeout'] <= 5 and kwargs.get('shell', False) is False
        if case == 'timeout': raise subprocess.TimeoutExpired(argv, 5)
        if case == 'changed-manifest': manifest.write_bytes(b'{"changed":true}')
        if case == 'changed-signature': signature.write_bytes(b'changed signature')
        return SimpleNamespace(returncode=1 if case == 'signature-failure' else 0, stdout=b'', stderr=b'')
    monkeypatch.setattr(subprocess, 'run', verify)
    if case == 'valid': p.verify_signature(manifest, signature, trusted_public_key=key)
    else:
        with pytest.raises(p.PrerequisiteRefusal): p.verify_signature(manifest, signature, trusted_public_key=key)
    assert len(calls) == 1
    assert calls[0][0][1:] == ['-V', '-P', key, '-m', str(manifest), '-x', str(signature)]
