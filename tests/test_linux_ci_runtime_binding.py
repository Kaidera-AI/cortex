"""R239 explicit stock runtime binding, private carrier effects, no engine calls."""
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from test_linux_builder_contract import ROOT, load


def setup(tmp_path, monkeypatch):
    p = load('linux_ci_runtime.py', monkeypatch)
    runner = tmp_path / 'runner'; runner.mkdir(mode=0o700)
    storage = load('linux_ci_storage.py', monkeypatch)
    storage.prepare_storage(runner)
    stock = tmp_path / 'brew/Cellar/crun/1.30.1/bin/crun'
    stock.parent.mkdir(parents=True)
    stock.write_bytes(b'public synthetic executable'); stock.chmod(0o755)
    selected = tmp_path / 'brew/bin/crun'; selected.parent.mkdir(); selected.symlink_to(stock)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs['timeout'] == 5 and kwargs['check'] and kwargs['text']
        return SimpleNamespace(stdout=('crun version 1.30.1\n' if argv[0] == str(selected)
                                       else 'crun version 1.4.5\n'))
    return p, runner, selected, stock, run, calls


def test_runtime_binds_buildah_and_podman_and_records_both_actual_versions(tmp_path, monkeypatch):
    p, runner, selected, stock, run, calls = setup(tmp_path, monkeypatch)
    result = p.prepare_runtime(runner, brew_crun=selected, run=run)
    env = result['environment']
    assert set(env) == {'CONTAINERS_CONF', 'BUILDAH_RUNTIME'}
    assert env['BUILDAH_RUNTIME'] == str(selected)
    path = Path(env['CONTAINERS_CONF'])
    assert path == runner / 'cortex-podman/containers.conf'
    import tomllib
    config = tomllib.loads(path.read_text())
    assert config == {'engine': {'runtime': 'crun', 'runtimes': {'crun': [str(selected)]}}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert result['versions']['selected']['path'] == str(selected)
    assert result['versions']['selected']['resolved_path'] == str(stock)
    assert result['versions']['selected']['version'] == 'crun version 1.30.1'
    assert result['versions']['inherited']['path'] == '/usr/bin/crun'
    assert result['versions']['inherited']['version'] == 'crun version 1.4.5'
    assert [a for a, k in calls] == [[str(selected), '--version'], ['/usr/bin/crun', '--version']]


@pytest.mark.parametrize('case', ['missing-storage', 'linked-root', 'reused-config', 'foreign-runtime',
    'non-executable', 'directory-runtime', 'unsafe-runner', 'override', 'unparseable-version', 'oversized-version'])
def test_runtime_refuses_unsafe_or_unbound_inputs(tmp_path, monkeypatch, case):
    p, runner, selected, stock, run, calls = setup(tmp_path, monkeypatch)
    if case == 'missing-storage': (runner / 'cortex-podman/storage.conf').unlink()
    elif case == 'linked-root':
        other = tmp_path / 'linked'; other.symlink_to(runner); runner = other
    elif case == 'reused-config': (runner / 'cortex-podman/containers.conf').write_text('old config')
    elif case == 'foreign-runtime':
        outside = tmp_path / 'foreign-crun'; outside.write_text('foreign'); outside.chmod(0o755)
        selected.unlink(); selected.symlink_to(outside)
    elif case == 'non-executable': stock.chmod(0o600)
    elif case == 'directory-runtime': stock.unlink(); stock.mkdir()
    elif case == 'unsafe-runner': runner = Path('relative')
    elif case == 'override': monkeypatch.setenv('CONTAINERS_CONF_OVERRIDE', '/inherited/override')
    else:
        run = lambda *a, **k: SimpleNamespace(stdout='unknown' if case == 'unparseable-version' else 'x' * 8193)
    with pytest.raises((RuntimeError, OSError)): p.prepare_runtime(runner, brew_crun=selected, run=run)


def test_missing_distro_runtime_is_an_explicit_observation(tmp_path, monkeypatch):
    p, runner, selected, stock, run, calls = setup(tmp_path, monkeypatch)
    def observe(argv, **kwargs):
        if argv[0] == '/usr/bin/crun': raise FileNotFoundError
        return run(argv, **kwargs)
    result = p.prepare_runtime(runner, brew_crun=selected, run=observe)
    assert result['versions']['inherited'] == {'path': '/usr/bin/crun', 'status': 'not-installed'}


def test_runtime_publication_is_two_literal_fields_after_existing_entry(tmp_path, monkeypatch):
    p, runner, selected, stock, run, calls = setup(tmp_path, monkeypatch)
    values = p.prepare_runtime(runner, brew_crun=selected, run=run)['environment']
    carrier = tmp_path / 'github-env'; carrier.write_text('PREVIOUS=literal\n')
    p.publish_runtime(carrier, values)
    assert carrier.read_text() == 'PREVIOUS=literal\n' + ''.join(k + '=' + v + '\n' for k, v in values.items())


@pytest.mark.parametrize('case', ['symlink', 'hardlink', 'directory', 'fifo', 'unterminated', 'extra-field', 'newline'])
def test_runtime_carrier_refuses_before_append(tmp_path, monkeypatch, case):
    p, runner, selected, stock, run, calls = setup(tmp_path, monkeypatch)
    values = p.prepare_runtime(runner, brew_crun=selected, run=run)['environment']
    target = tmp_path / 'target'; target.write_bytes(b'UNCHANGED\n')
    carrier = tmp_path / 'github-env'; carrier.write_bytes(b'PREVIOUS=value\n')
    if case == 'symlink': carrier.unlink(); carrier.symlink_to(target)
    elif case == 'hardlink': carrier.unlink(); os.link(target, carrier)
    elif case == 'directory': carrier.unlink(); carrier.mkdir()
    elif case == 'fifo': carrier.unlink(); os.mkfifo(carrier)
    elif case == 'unterminated': carrier.write_bytes(b'PREVIOUS<<UNFINISHED')
    elif case == 'extra-field': values['CONTAINERS_CONF_OVERRIDE'] = '/poison'
    else: values['BUILDAH_RUNTIME'] += '\nINJECTED=value'
    before = None if case in ('directory', 'fifo') else carrier.read_bytes()
    with pytest.raises((RuntimeError, OSError)): p.publish_runtime(carrier, values)
    assert target.read_bytes() == b'UNCHANGED\n'
    if before is not None: assert carrier.read_bytes() == before


def test_linux_workflow_installs_binds_then_observes_and_preserves_failed_bytes():
    import yaml
    data = yaml.load((ROOT / '.github/workflows/cortex-linux-candidate.yml').read_text(), Loader=yaml.BaseLoader)
    steps = data['jobs']['images']['steps']
    bind = next((i for i, s in enumerate(steps) if 'linux_ci_runtime.py' in s.get('run', '')), None)
    assert bind is not None, 'current stock crun is not bound in the Linux job'
    version = next(i for i, s in enumerate(steps) if 'podman --remote=false version' in s.get('run', ''))
    assert bind < version
    assert not any('podman --remote=false ' in s.get('run', '') for s in steps[:bind])
    body = (ROOT / '.github/workflows/cortex-linux-candidate.yml').read_text()
    assert 'brew install crun --force-bottle' in body
    host = data['jobs']['host']['steps']
    freeze = next(s for s in host if 'build-candidate.py host' in s.get('run', ''))
    assert freeze['env']['LD_LIBRARY_PATH'].endswith('/openssl/lib:${{ runner.temp }}/cortex-native-runtime/python/lib')
    assert any('linux_runtime_closure.py' in s.get('run', '') for s in host)
    retained = [s for s in host if s.get('if') == 'failure()' and s.get('with', {}).get('retention-days') == '3']
    assert len(retained) == 1 and '/cortex-host/bin' in retained[0]['with']['path']
