"""Public payload/JSON bytes with all native builder effects intercepted."""
import copy
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CATALOG_CASES = ['valid', 'source', 'bool-source', 'schema', 'payload', 'ledger', 'missing-ledger',
                 'duplicate-ledger', 'ledger-fields', 'fixture', 'instance', 'status',
                 'absent-relations', 'duplicate-relations', 'unsorted-relations', 'name', 'field',
                 'nonbool', 'unforced', 'direct-grant', 'extra-migration', 'changed-source', 'oversized']
JSON_CASES = ['valid', 'duplicate', 'linked', 'fifo', 'oversized', 'unknown-json', 'non-object']
PROVISION_CASES = ['valid', 'ordinary', 'revision', 'bool-revision', 'not-ci', 'darwin',
                   'arm64', 'wrong-engine', 'record-source', 'empty-source']
REHEARSAL_CASES = ['linux', 'mac', 'missing-catalog', 'replay-different', 'catalog-source',
                   'ledger', 'malformed', 'nonbool']


def load(name, monkeypatch):
    path = ROOT / 'scripts/release' / name
    assert path.is_file(), 'native build catalog binding is missing: ' + name
    monkeypatch.syspath_prepend(str(path.parent)); monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('cm2_catalog_' + name.replace('.', '_'), path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def fixture(tmp_path):
    from cortex_v2.native_prerequisite import payload_inventory
    source = tmp_path / 'src'; source.mkdir(mode=0o700)
    (source / 'public.py').write_bytes(b'PUBLIC_PAYLOAD = 1\n')
    migrations = tmp_path / 'migrations'; migrations.mkdir(mode=0o700)
    hashes = {}
    for index in range(1, 16):
        name = str(index).zfill(4) + '_core.sql'; raw = ('SELECT ' + str(index) + ';\n').encode()
        (migrations / name).write_bytes(raw); hashes[name] = hashlib.sha256(raw).hexdigest()
    inventory = {'schema': 'cortex.rls-inventory.v1', 'source_revision': '1' * 40,
                 'api_source_payload_sha256': payload_inventory(source, migrations)['sha256'],
                 'migrations': hashes, 'relations': [
                     {'name': 'cortex_auth.credentials', 'rls': False, 'forced': False, 'app_direct_grant': False},
                     {'name': 'cortex_core.records', 'rls': True, 'forced': True, 'app_direct_grant': True}]}
    receipt = {'instance': 'cortex_v2_package_test', 'fixture': 'verified', 'status': 'applied',
               'migrations': [{'migration': name, 'checksum': digest} for name, digest in hashes.items()],
               'build_catalog': inventory}
    return source, migrations, receipt


@pytest.mark.parametrize('case', CATALOG_CASES)
def test_actual_source_ledger_and_strict_inventory_bind_canonical_catalog_bytes(case, tmp_path, monkeypatch):
    module = load('linux_build_catalog.py', monkeypatch)
    source, migrations, receipt = fixture(tmp_path)
    catalog = receipt['build_catalog']; revision = '1' * 40
    if case == 'source': revision = 'f' * 40
    if case == 'bool-source': revision = True
    if case == 'schema': catalog['schema'] = 'foreign'
    if case == 'payload': catalog['api_source_payload_sha256'] = 'f' * 64
    if case == 'ledger': receipt['migrations'][0]['checksum'] = 'f' * 64
    if case == 'missing-ledger': receipt['migrations'].pop()
    if case == 'duplicate-ledger': receipt['migrations'].append(copy.deepcopy(receipt['migrations'][0]))
    if case == 'ledger-fields': receipt['migrations'][0]['unexpected'] = True
    if case == 'fixture': receipt['fixture'] = 'none'
    if case == 'instance': receipt['instance'] = 'foreign'
    if case == 'status': receipt['status'] = 'cached'
    if case == 'absent-relations': catalog['relations'] = []
    if case == 'duplicate-relations': catalog['relations'].append(copy.deepcopy(catalog['relations'][1]))
    if case == 'unsorted-relations': catalog['relations'].reverse()
    if case == 'name': catalog['relations'][1]['name'] = 'public.foreign'
    if case == 'field': catalog['unexpected'] = True
    if case == 'nonbool': catalog['relations'][1]['rls'] = 1
    if case == 'unforced': catalog['relations'][1]['forced'] = False
    if case == 'direct-grant': catalog['relations'][0]['app_direct_grant'] = True
    if case == 'extra-migration': (migrations / '0016_extra.sql').write_bytes(b'SELECT 16;\n')
    if case == 'changed-source': (source / 'public.py').write_bytes(b'PUBLIC_PAYLOAD = 2\n')
    if case == 'oversized':
        catalog['relations'] = [{'name': 'cortex_core.relation_' + str(index).zfill(4),
            'rls': True, 'forced': True, 'app_direct_grant': True} for index in range(800)]
    call = lambda: module.catalog_bytes(receipt, source_sha=revision, source_root=source, migration_root=migrations)
    if case == 'valid':
        raw = call()
        assert raw == json.dumps(catalog, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        assert len(raw) <= 65536 and json.loads(raw) == catalog
    else:
        with pytest.raises(RuntimeError) as error: call()
        assert str(error.value) == 'native build catalog missing or mismatched'


@pytest.mark.parametrize('case', JSON_CASES)
def test_actual_public_json_refuses_duplicates_links_fifo_and_unbounded_input(case, tmp_path, monkeypatch):
    module = load('linux_build_catalog.py', monkeypatch)
    path = tmp_path / 'public.json'; path.write_bytes(b'{"PUBLIC":true}\n')
    if case == 'duplicate': path.write_bytes(b'{"PUBLIC":true,"PUBLIC":false}\n')
    if case == 'linked':
        actual = tmp_path / 'actual.json'; path.rename(actual); path.symlink_to(actual)
    if case == 'fifo': path.unlink(); os.mkfifo(path, 0o600)
    if case == 'oversized': path.write_bytes(b' ' * (1048576 + 1))
    if case == 'unknown-json': path.write_bytes(b'{invalid')
    if case == 'non-object': path.write_bytes(b'[]\n')
    if case == 'valid': assert module.read_json(path) == {'PUBLIC': True}
    else:
        with pytest.raises(RuntimeError) as error: module.read_json(path)
        assert str(error.value) == 'native build catalog missing or mismatched'


@pytest.mark.parametrize('case', PROVISION_CASES)
def test_only_native_linux_ci_adds_literal_public_catalog_flag_to_finite_migrator(case, tmp_path, monkeypatch):
    load('linux_build_catalog.py', monkeypatch)
    module = load('install_candidate.py', monkeypatch)
    assert 'build_catalog_source' in inspect.signature(module.provision).parameters
    calls = []; records = []
    class Engine:
        architecture = 'arm64' if case == 'wrong-engine' else 'amd64'
        def run(self, args, **kwargs): calls.append((list(args), kwargs)); return ''
    engine = Engine()
    monkeypatch.setattr(module, 'SUFFIXES', {})
    monkeypatch.setattr(module, 'write_record', lambda root, record: records.append(copy.deepcopy(record)))
    monkeypatch.setenv('GITHUB_ACTIONS', 'false' if case == 'not-ci' else 'true')
    monkeypatch.setattr(module.platform, 'system', lambda: 'Darwin' if case == 'darwin' else 'Linux')
    monkeypatch.setattr(module.platform, 'machine', lambda: 'aarch64' if case == 'arm64' else 'x86_64')
    record = {'installation': '10000000-0000-4000-8000-000000000001', 'source_sha': '1' * 40, 'port': 18602}
    if case == 'record-source': record['source_sha'] = 'f' * 40
    manifest = {'images': {role: {'config_id': 'sha256:' + str(index) * 64}
                          for index, role in enumerate(module.ROLES, 1)}}
    revision = None if case == 'ordinary' else 'invalid' if case == 'revision' else True if case == 'bool-revision' else '' if case == 'empty-source' else '1' * 40
    call = lambda: module.provision(engine, manifest, tmp_path, record, build_catalog_source=revision)
    if case in ('valid', 'ordinary'):
        call(); assert len(calls) == 8 and len(records) == 1 and record['stage'] == 'created'
        migrate = next(args for args, kwargs in calls if '--entrypoint=python' in args)
        expected = module.container_args('migrate', record); expected[0] = 'create'
        expected += ['--entrypoint=python', '--env', 'CORTEX_V2_MIGRATOR_DATABASE_URL_FILE=/run/secrets/database-url-migrator',
                     '--env', 'CORTEX_V2_FIXTURE_FILE=/run/secrets/fixture']
        expected += module.secret_args(record, 'database-url-migrator', 'database-url-migrator') + module.secret_args(record, 'fixture', 'fixture')
        if case == 'valid': expected += ['--env', 'GITHUB_ACTIONS=true']
        expected += [manifest['images']['api']['config_id'], '-m', 'cortex_v2.migrate']
        if case == 'valid': expected += ['--linux-ci-build-catalog-source', '1' * 40]
        assert migrate == expected
        assert all(kwargs == {} for args, kwargs in calls)
    else:
        with pytest.raises(module.Refusal) as error: call()
        assert str(error.value) == 'native build catalog invocation refused'
        assert calls == records == [] and 'stage' not in record


@pytest.mark.parametrize('case', REHEARSAL_CASES)
def test_native_rehearsal_requires_exact_initial_and_replay_catalog_and_always_cleans(case, tmp_path, monkeypatch):
    helper = load('linux_build_catalog.py', monkeypatch)
    module = load('package_rehearsal.py', monkeypatch)
    source, migrations, receipt = fixture(tmp_path)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module.Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setitem(__import__('sys').modules, 'linux_build_catalog', helper)
    calls = []; starts = []
    class Engine:
        architecture = 'arm64' if case == 'mac' else 'amd64'
        def exists(self, *args): return False
        def run(self, args, **kwargs): calls.append(('engine', list(args))); return '6.1.3'
    engine = Engine(); monkeypatch.setattr(module, 'NativeBuilder', lambda target: engine)
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def bind(self, address): assert address == ('127.0.0.1', 0)
        def getsockname(self): return ('127.0.0.1', 18602)
    monkeypatch.setattr(module.socket, 'socket', Socket)
    monkeypatch.setattr(module, 'private_root', lambda path, create: path.mkdir(parents=True, mode=0o700))
    monkeypatch.setattr(module, 'write_record', lambda *args: None)
    monkeypatch.setattr(module, 'prepare', lambda *args: None)
    def provision(engine, manifest, root, record, **kwargs):
        assert kwargs == ({} if case == 'mac' else {'build_catalog_source': '1' * 40})
        calls.append(('provision', kwargs))
    monkeypatch.setattr(module, 'provision', provision)
    def start(engine, manifest, record, root):
        starts.append(True); value = copy.deepcopy(receipt)
        if case in ('mac', 'missing-catalog'): value.pop('build_catalog')
        if case == 'catalog-source': value['build_catalog']['source_revision'] = 'f' * 40
        if case == 'ledger': value['migrations'][0]['checksum'] = 'f' * 64
        if case == 'nonbool': value['build_catalog']['relations'][1]['rls'] = 1
        if len(starts) == 2:
            value['status'] = 'verified'
            if case == 'replay-different': value['build_catalog']['relations'][1]['app_direct_grant'] = False
        (root / 'migration-receipt.json').write_bytes(b'{invalid' if case == 'malformed' else json.dumps(value).encode())
    monkeypatch.setattr(module, 'start_stack', start)
    def smoke(root, record):
        calls.append(('smoke',))
        (root / 'smoke-receipt.json').write_text(json.dumps({'status': 'PASS'}))
    monkeypatch.setattr(module, 'smoke', smoke)
    monkeypatch.setattr(module, 'erase', lambda *args: calls.append(('erase',)) or {'status': 'erased'})
    entries = {role: {'config_id': 'sha256:' + str(index) * 64} for index, role in enumerate(module.ROLES, 1)}
    target = 'macos-arm64' if case == 'mac' else 'linux-x86_64'
    call = lambda: module.rehearse(entries, '1' * 40, '0.2.001-test.20261006.1', target=target)
    if case in ('linux', 'mac'):
        value = call(); assert value['status'] == 'PASS' and value['cleanup']['status'] == 'erased'
        assert len(starts) == 2 and calls.count(('smoke',)) == 2
        if case == 'linux':
            raw = helper.catalog_bytes(receipt, source_sha='1' * 40, source_root=source, migration_root=migrations)
            assert value['build_catalog'] == receipt['build_catalog']
            assert value['build_catalog_sha256'] == hashlib.sha256(raw).hexdigest()
        else: assert 'build_catalog' not in value and 'build_catalog_sha256' not in value
    else:
        with pytest.raises((RuntimeError, module.Refusal)): call()
        assert len(starts) == (2 if case == 'replay-different' else 1)
        assert calls.count(('smoke',)) == (1 if case == 'replay-different' else 0)
    assert calls[-1] == ('erase',) and calls.count(('erase',)) == 1
