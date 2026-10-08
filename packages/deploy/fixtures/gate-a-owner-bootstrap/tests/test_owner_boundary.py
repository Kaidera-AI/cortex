import pathlib,runpy,subprocess,sys
import pytest

FIXTURE=pathlib.Path(__file__).resolve().parents[1]

def test_real_prepare_host_routes_installer_and_brew_to_runtime_owner(monkeypatch,tmp_path):
    calls=[]
    def capture(cmd,**kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd,0)
    monkeypatch.setattr(subprocess,'run',capture)
    key=tmp_path/'key';key.touch();known=tmp_path/'known';known.touch()
    monkeypatch.setattr(sys,'argv',['prepare-host.py','--host','synthetic-host','--key',str(key),'--known-hosts',str(known)])
    runpy.run_path(str(FIXTURE/'prepare-host.py'),run_name='__main__')
    install=[c for c in calls if c[0]=='ssh' and c[-1].endswith('host-tools-setup.sh')]
    assert len(install)==1
    assert 'kos@synthetic-host' in install[0],calls
    owner=[c for c in calls if c[0]=='ssh' and c[-1].endswith('new-owner-setup.sh')]
    assert len(owner)==1 and 'rocky@synthetic-host' in owner[0]
    assert any(c[0]=='scp' and c[-1].startswith('kos@synthetic-host:') and 'homebrew-install.sh' in c[-2] for c in calls)

def test_host_tools_requires_the_owner_and_never_root_or_sudo():
    s=(FIXTURE/'host-tools-setup.sh').read_text()
    assert '[ "$(id -un)" = kos ]' in s
    assert 'HOMEBREW_NO_SUDO=1' in s
    assert 'sudo ' not in s and '/etc/sudoers' not in s
    assert '$(stat -c %u /home/linuxbrew/.linuxbrew)' in s
    assert '/home/kos/gate-a-bootstrap/homebrew-install.sh' in s

def test_admin_prepares_fresh_owner_prefix_without_reowning_existing_tree():
    s=(FIXTURE/'new-owner-setup.sh').read_text()
    assert 'homebrew_prefix_foreign_owner' in s
    assert 'install -d -o kos -g kos -m 0755 /home/linuxbrew /home/linuxbrew/.linuxbrew' in s
    assert 'chown -R' not in s and 'chgrp' not in s and '/etc/sudoers' not in s
    assert 'brew install' not in s

def test_owner_helper_preflight_runs_before_the_unchanged_runtime_setup():
    s=(FIXTURE/'host-tools-setup.sh').read_text()
    assert 'homebrew_owner_helper_unavailable' in s
    assert 'rootlessport' in s and 'netavark' in s and 'aardvark-dns' in s
    assert 'readlink -f' in s and 'Cellar/' in s
    c=(FIXTURE/'COMMANDS.md').read_text()
    assert 'owner-helper preflight' in c and 'Homebrew installer and brew install run as kos' in c
