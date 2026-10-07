"""Closed qualification route and real-query seam; public transport fixtures."""
import importlib.util
import json
import re
from pathlib import Path
import pytest
import yaml
ROOT = Path(__file__).resolve().parents[1]

def load(monkeypatch):
    path = ROOT / 'scripts/release/database_version_receipt.py'
    assert path.is_file(), 'observed database version receipt helper missing'
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location('version_r428', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

def test_native_rehearsal_retains_arm64_and_adds_closed_amd64_leg():
    doc = yaml.load((ROOT / '.github/workflows/cortex-package-rehearsal.yml').read_text(), Loader=yaml.BaseLoader)
    assert set(doc['jobs']) == {'rehearse', 'rehearse_amd64'}
    assert doc['jobs']['rehearse']['runs-on'] == 'ubuntu-24.04-arm'
    assert doc['jobs']['rehearse_amd64']['runs-on'] == 'ubuntu-22.04'
    for job, machine, target in [('rehearse', 'aarch64', None), ('rehearse_amd64', 'x86_64', 'linux-x86_64')]:
        value = doc['jobs'][job]; steps = value['steps']; runs = '\n'.join(x.get('run', '') for x in steps)
        assert 'ren-cx/package-rehearsal-$REHEARSAL_SHA' in runs
        assert 'test "$GITHUB_RUN_ATTEMPT" = 1' in runs
        assert 'test "$(uname -m)" = ' + machine in runs
        assert value['env']['CORTEX_CI_DATABASE_VERSION_RECEIPT'].endswith('/database-version-receipt.json')
        upload = next(x for x in steps if x.get('uses', '').startswith('actions/upload-artifact@'))
        assert 'database-version-receipt.json' in upload['with']['path']
        assert 'gh release' not in runs and 'git tag' not in runs
        if target:
            assert '--target ' + target in runs
            assert 'scripts/release/linux_ci_storage.py' in runs
            assert 'brew install podman --force-bottle' in runs
            assert 'podman --remote=false version' in runs
    assert set(doc['on']) == {'push'}

@pytest.mark.parametrize('target', ['macos-arm64', 'linux-x86_64'])
@pytest.mark.parametrize('case', ['valid', 'vector-drift', 'server-major', 'missing-vector', 'duplicate-json', 'wrong-image'])
def test_observed_versions_are_readonly_source_pinned_and_exclusive(target, case, tmp_path, monkeypatch):
    module = load(monkeypatch)
    recipe = ROOT / 'deploy/release' / ('Containerfile.db.linux-amd64' if target == 'linux-x86_64' else 'Containerfile.db')
    tag, digest = re.search(r'pgvector/pgvector:([^@]+)@(sha256:[a-f0-9]{64})', recipe.read_text()).groups()
    vector, pg = tag.split('-pg'); major = int(pg)
    value = {'server_version': pg + '.4', 'server_version_num': str(major * 10000 + 4), 'vector': vector}
    if case == 'vector-drift': value['vector'] += '.drift'
    if case == 'server-major': value['server_version_num'] = str((major + 1) * 10000)
    if case == 'missing-vector': value['vector'] = None
    raw = json.dumps(value)
    if case == 'duplicate-json': raw = raw[:-1] + ',"vector":"foreign"}'
    calls = []
    class Engine:
        def run(self, args, **kwargs): calls.append((args, kwargs)); return raw
    destination = tmp_path / 'receipt.json'
    record = {'installation': '10000000-0000-4000-8000-000000000001'}
    image = 'sha256:' + '1' * 64
    entries = {'db': {'config_id': image}}
    if case == 'wrong-image': entries['db']['config_id'] = 'foreign'
    call = lambda: module.observe(Engine(), record, entries, target=target, source_root=ROOT, source_sha='1' * 40, destination=destination)
    if case == 'valid':
        result = call(); assert json.loads(destination.read_text()) == result
        assert result['base_digest'] == digest and result['image_config_id'] == image
        assert result['vector'] == vector and result['server_version'] == value['server_version']
        assert result['target'] == target and result['source_sha'] == '1' * 40
        assert len(calls) == 1 and calls[0][1] == {'read': True}
        command = calls[0][0]; sql = command[-1]
        assert command[:1] == ['exec'] and 'psql' in command
        assert 'BEGIN READ ONLY' in sql and 'pg_extension' in sql and 'server_version_num' in sql
        assert 'PASSWORD' not in ' '.join(command)
        with pytest.raises(RuntimeError): call()
        assert json.loads(destination.read_text()) == result
    else:
        with pytest.raises(RuntimeError): call()
        assert not destination.exists()

def test_rehearsal_observes_versions_before_smoke_and_cleanup():
    source = (ROOT / 'scripts/release/package_rehearsal.py').read_text()
    assert 'CORTEX_CI_DATABASE_VERSION_RECEIPT' in source
    assert source.index('observe(') < source.index('smoke(root, record)') < source.index('outcome["cleanup"]')

def test_rehearsal_imports_from_clean_native_builder_without_site_packages():
    import subprocess
    import sys
    code = "import sys; sys.path[:0] = " + repr([str(ROOT / 'scripts/release'), str(ROOT / 'scripts')]) + "; import package_rehearsal"
    result = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
