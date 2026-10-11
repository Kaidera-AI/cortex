"""Source controls for the canonical standalone layout; no engine/native claims."""
import ast
import os
from pathlib import Path
import subprocess
import tempfile

import pytest
import yaml
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / 'packages/deploy/docker-compose.yml'
AUTHORITY = '${CORTEX_PROJECTS_ROOT:?installer must set authoritative projects root}'


def layout():
    return yaml.safe_load(COMPOSE.read_text())


@pytest.mark.parametrize('name', ['embed', 'pdf', 'graph'])
def test_original_worker_home_reaches_terminal_from_config(name, tmp_path):
    service = layout()['services']['cortex-' + name + '-worker']
    assert service['read_only'] is True
    assert service['cap_drop'] == ['ALL']
    assert service['security_opt'] == ['no-new-privileges:true']
    assert 'size=128m' in service['tmpfs'][0]
    # Map the shipped read-only home and writable /tmp into a local fixture.
    home = tmp_path / 'image-home'; home.mkdir(); home.chmod(0o555)
    scratch = tmp_path / 'tmp'; scratch.mkdir()
    settings = service.get('environment', {})
    configured = settings.get('HOME', '/home/kaidera')
    mapped = str(scratch / 'kaidera') if configured == '/tmp/kaidera' else str(home)
    entry = ROOT / 'packages/containers' / (name + '-worker') / 'worker-entrypoint'
    source = entry.read_text()
    if name == 'graph':
        # Exact write predicate only; absolute image verifier is native-only.
        start = source.index('require_writable_dir()')
        end = source.index('\nrequire_readable_dir()', start)
        source = 'set -eu\nfail(){ echo "$*" >&2; exit 70; }\n' + source[start:end] + '\nrequire_writable_dir "worker home" "$HOME"\necho TERMINAL\n'
    env = dict(os.environ, HOME=mapped, TMPDIR=str(scratch), HF_HOME=str(scratch/'hf'),
               SENTENCE_TRANSFORMERS_HOME=str(scratch/'st'))
    try:
        result = subprocess.run(['sh', '-c', source, 'entrypoint', 'sh', '-c', 'echo TERMINAL'],
                                env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f'exit {result.returncode}: {result.stderr}'
        assert 'TERMINAL' in result.stdout
        assert configured == '/tmp/kaidera'
    finally:
        home.chmod(0o755)


def test_embed_scratch_stays_inside_bounded_tmpfs():
    env = layout()['services']['cortex-embed-worker'].get('environment', {})
    assert env.get('HF_HOME') == '/tmp/kaidera/hf'
    assert env.get('SENTENCE_TRANSFORMERS_HOME') == '/tmp/kaidera/sentence-transformers'
    image = (ROOT/'packages/containers/embed-worker/Dockerfile').read_text()
    assert 'HF_HUB_OFFLINE=1' in image and 'TRANSFORMERS_OFFLINE=1' in image
    assert 'immutable image under /opt' in image


def resolver(tmp_path, host='', registered=''):
    source = ROOT/'packages/containers/graph-worker/worker.py'
    names = {'_external_relative_candidate', '_resolved_projects_root', '_safe_repo_candidate', '_resolve_exact_host_repo'}
    tree = ast.parse(source.read_text())
    tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = dict(os=os, Path=Path, HTTPException=HTTPException, PROJECTS_DIR=tmp_path,
              HOST_PROJECTS_ROOT=host, REGISTERED_PROJECTS_ROOT=registered)
    exec(compile(tree, str(source), 'exec'), ns)
    return ns['_resolve_exact_host_repo']


def test_authoritative_mapping_and_all_containment_refusals(tmp_path):
    project = tmp_path/'fixture'; project.mkdir()
    api = layout()['services']['cortex-api']['environment']
    graph = layout()['services']['cortex-graph-worker'].get('environment', {})
    # No authority in the shipped layout reproduces its 403 before source edit.
    configured = '/registered' if graph.get('REGISTERED_PROJECTS_ROOT') == AUTHORITY else ''
    resolve = resolver(tmp_path, configured, configured)
    assert resolve('/registered/fixture', field_name='repo') == project
    assert api['HOST_PROJECTS_ROOT'] == api['REGISTERED_PROJECTS_ROOT'] == AUTHORITY
    assert graph['HOST_PROJECTS_ROOT'] == graph['REGISTERED_PROJECTS_ROOT'] == AUTHORITY
    assert graph['PROJECTS_DIR'] == '/projects'
    assert f'{AUTHORITY}:/projects:ro' in layout()['services']['cortex-graph-worker']['volumes']
    assert f'{AUTHORITY}:{AUTHORITY}:ro' in layout()['services']['cortex-api']['volumes']
    for path in ['/foreign/fixture', '/registered/../fixture']:
        with pytest.raises(HTTPException) as e: resolve(path, field_name='repo')
        assert e.value.status_code == 403
    (tmp_path/'alias').symlink_to(project, target_is_directory=True)
    with pytest.raises(HTTPException) as e: resolve('/registered/alias', field_name='repo')
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        resolver(tmp_path, '/one', '/two')('/one/fixture', field_name='repo')
    assert e.value.status_code == 503


@pytest.fixture
def safe_authority_dir():
    # /tmp is now an API reserved namespace; test a valid owner-home project tree.
    with tempfile.TemporaryDirectory(prefix='source-authority-', dir=Path.home()) as directory:
        yield Path(directory).resolve()


def test_installer_authority_is_required_physical_and_readable(safe_authority_dir, monkeypatch):
    tmp_path = safe_authority_dir
    source = ROOT/'packages/deploy/cortex-runtime'
    tree = ast.parse(source.read_text())
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'validated_projects_root']
    assert len(selected) == 1, 'installer has no authoritative projects-root validator'
    ns = dict(os=os, Path=Path)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), ns)
    validate = ns['validated_projects_root']
    monkeypatch.delenv('CORTEX_PROJECTS_ROOT', raising=False)
    with pytest.raises(ValueError): validate()
    for value in ['/', 'relative', str(tmp_path/'missing'), str(tmp_path/'..')]:
        monkeypatch.setenv('CORTEX_PROJECTS_ROOT', value)
        with pytest.raises(ValueError): validate()
    root = tmp_path/'projects'; root.mkdir()
    alias = tmp_path/'alias'; alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setenv('CORTEX_PROJECTS_ROOT', str(alias))
    with pytest.raises(ValueError): validate()
    monkeypatch.setenv('CORTEX_PROJECTS_ROOT', str(root))
    assert validate() == str(root)
    root.chmod(0o000)
    try:
        with pytest.raises(ValueError): validate()
    finally: root.chmod(0o755)
