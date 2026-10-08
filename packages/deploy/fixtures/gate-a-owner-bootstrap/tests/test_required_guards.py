import os
import pathlib
import subprocess
import pytest

BASE = pathlib.Path(__file__).resolve().parents[1]

@pytest.mark.parametrize("script,owner,home", [
    ("host-tools-setup.sh", "kos", "/wrong-home"),
    ("host-tools-setup.sh", "rocky", "/home/kos"),
    ("new-owner-setup.sh", "kos", "/home/kos"),
])
def test_required_guard_refuses_before_side_effect(script, owner, home, tmp_path):
    trace = tmp_path / "trace"
    shell = r'''id() { case "$*" in '-un') printf '%s\n' "$MOCK_OWNER";; '-u') echo 1001;; kos) return 1;; *) echo 1001;; esac; }
stat() { echo 1001; }
[() {
 case "$*" in
 '-L /home/linuxbrew/.linuxbrew ]'|'-e /home/linuxbrew/.linuxbrew/bin/brew ]'|'-e /home/linuxbrew/.linuxbrew ]'|'-L /home ]'|'-L /home/linuxbrew ]') return 1;;
 '-d /home/linuxbrew/.linuxbrew ]'|'-w /home/linuxbrew/.linuxbrew ]'|'-d /home/linuxbrew ]'|'-d /home ]'|'-e /home ]'|'-e /home/linuxbrew ]') return 0;;
 *) builtin [ "$@";;
 esac
}
bash() { printf 'INSTALLER_REACHED\n' >> "$TRACE"; exit 0; }
sudo() { printf 'ADMIN_SIDE_EFFECT_REACHED\n' >> "$TRACE"; exit 0; }
source "$SCRIPT"
'''
    result = subprocess.run(["/bin/bash", "-c", shell], env=dict(os.environ,
        HOME=home, USER="kos", LOGNAME="kos", MOCK_OWNER=owner,
        SCRIPT=str(BASE/script), TRACE=str(trace)), capture_output=True, text=True)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not trace.exists(), trace.read_text() if trace.exists() else ""
