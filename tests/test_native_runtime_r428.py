"""Runtime custody and pinned base execution; public transport fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import pytest
import yaml
ROOT = Path(__file__).resolve().parents[1]

def load(monkeypatch):
    path = ROOT / 'scripts/release/linux_ci_runtime.py'
    assert path.is_file(), 'job-owned native runtime qualifier missing'
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location('runtime_r428', path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m

def brew_fixture(tmp_path):
    prefix = tmp_path / 'brew'; (prefix / 'opt').mkdir(parents=True)
    for formula, paths in {'crun':['bin/crun'], 'conmon':['bin/conmon'], 'podman':['libexec/podman/rootlessport','libexec/podman/netavark','libexec/podman/aardvark-dns'], 'passt':['bin/pasta'], 'fuse-overlayfs':['bin/fuse-overlayfs']}.items():
        home = prefix / 'Cellar' / formula / 'public-fixture'; home.mkdir(parents=True)
        (prefix / 'opt' / formula).symlink_to(home)
        for name in paths:
            path = home / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('#!/bin/sh\nexit 0\n'); path.chmod(0o755)
    runner = tmp_path / 'runner'; runner.mkdir(mode=0o700); (runner / 'cortex-podman').mkdir(mode=0o700)
    return prefix, runner

@pytest.mark.parametrize('case', ['valid','missing-crun','missing-conmon','escape','nonexec','existing-config','linked-parent'])
def test_configuration_is_exclusive_job_owned_and_stable_opt(case,tmp_path,monkeypatch):
    m=load(monkeypatch); prefix,runner=brew_fixture(tmp_path)
    config=runner/'cortex-podman/containers.conf'
    if case=='missing-crun': (prefix/'opt/crun/bin/crun').unlink()
    if case=='missing-conmon': (prefix/'opt/conmon/bin/conmon').unlink()
    if case=='escape':
        (prefix/'opt/crun').unlink(); (prefix/'opt/crun').symlink_to(prefix/'Cellar/conmon/public-fixture')
    if case=='nonexec': (prefix/'opt/crun/bin/crun').chmod(0o600)
    if case=='existing-config': config.write_bytes(b'KEEP_PUBLIC\n')
    if case=='linked-parent':
        actual=tmp_path/'actual'; (runner/'cortex-podman').rename(actual); (runner/'cortex-podman').symlink_to(actual)
    call=lambda:m.configure(runner, brew_prefix=prefix)
    if case=='valid':
        assert call()==config
        import tomllib
        value=tomllib.loads(config.read_text())['engine']
        assert value['runtime']=='crun'
        assert value['conmon_path']==[str(prefix/'opt/conmon/bin/conmon')]
        assert value['runtimes']['crun']==[str(prefix/'opt/crun/bin/crun')]
        assert value['helper_binaries_dir']==[str(prefix/'opt/podman/libexec/podman'),str(prefix/'opt/passt/bin'),str(prefix/'opt/fuse-overlayfs/bin')]
        assert '/Cellar/' not in config.read_text()
        assert stat.S_IMODE(config.stat().st_mode)==0o600
        old=config.read_bytes()
        with pytest.raises(RuntimeError):call()
        assert config.read_bytes()==old
    else:
        with pytest.raises(RuntimeError):call()
        if case=='existing-config':assert config.read_bytes()==b'KEEP_PUBLIC\n'
        else:assert not config.exists()

@pytest.mark.parametrize('case',['valid','distro-runtime','distro-conmon','unknown-version','wrong-machine','wrong-python','missing-runtime-version'])
def test_preflight_measures_paths_versions_and_executes_actual_pinned_base(case,tmp_path,monkeypatch):
    m=load(monkeypatch);prefix,runner=brew_fixture(tmp_path);config=m.configure(runner,brew_prefix=prefix)
    info={'host':{'ociRuntime':{'path':str(prefix/'opt/crun/bin/crun'),'name':'crun','version':'crun public-fixture'},'conmon':{'path':str(prefix/'opt/conmon/bin/conmon'),'version':'conmon public-fixture'}}}
    if case=='distro-runtime':info['host']['ociRuntime']['path']='/usr/bin/crun'
    if case=='distro-conmon':info['host']['conmon']['path']='/usr/bin/conmon'
    if case=='missing-runtime-version':info['host']['ociRuntime']['version']=''
    base,py=re.search(r'FROM (docker.io/library/python:([0-9.]+)-[^ ]+)',(ROOT/'deploy/release/Dockerfile.linux-amd64').read_text()).groups()
    observed={'machine':'aarch64' if case=='wrong-machine' else 'x86_64','python':'foreign' if case=='wrong-python' else py}
    calls=[]
    def run(args,*,read=False):
        calls.append(args)
        if 'info' in args:return json.dumps(info)
        if 'version' in args:return '6.1.3'
        if case=='unknown-version':raise RuntimeError('unknown version specified')
        return json.dumps(observed)
    destination=runner/'preflight.json'
    call=lambda:m.qualify(run, source_root=ROOT, config=config, destination=destination,brew_prefix=prefix)
    if case=='valid':
        value=call();assert json.loads(destination.read_text())==value
        assert value['runtime']['path']==info['host']['ociRuntime']['path']
        assert value['conmon']['version']==info['host']['conmon']['version']
        assert value['podman_version']=='6.1.3' and value['base']==base
        commands=[c for c in calls if 'run' in c];assert len(commands)==1
        command=commands[0];assert base in command and '--remote=false' in command
        assert '--read-only' in command and '--network=none' in command and '--rm' in command
        assert '--cap-drop=ALL' in command
    else:
        with pytest.raises(RuntimeError):call()
        assert not destination.exists()

def test_both_linux_image_workflows_bind_and_qualify_runtime_before_build():
    for name,job in [('cortex-package-rehearsal.yml','rehearse_amd64'),('cortex-linux-candidate.yml','images')]:
        doc=yaml.load((ROOT/'.github/workflows'/name).read_text(),Loader=yaml.BaseLoader)
        steps=doc['jobs'][job]['steps']; configure=[i for i,s in enumerate(steps) if 'linux_ci_runtime.py configure' in s.get('run','')];preflight=[i for i,s in enumerate(steps) if 'linux_ci_runtime.py qualify' in s.get('run','')];build=[i for i,s in enumerate(steps) if 'build-candidate.py images' in s.get('run','')]
        assert len(configure)==len(preflight)==len(build)==1
        assert configure[0]<preflight[0]<build[0]
        assert any('runtime-preflight.json' in s.get('with',{}).get('path','') for s in steps)
