import pathlib,subprocess

SCRIPT=pathlib.Path(__file__).resolve().parents[1]/'new-owner-setup.sh'

def test_existing_parent_is_not_reowned(tmp_path):
    trace=tmp_path/'trace'
    shell=r'''created=0
id() { case "$*" in '-un') echo rocky;; 'kos') [ "$created" = 1 ];; '-u kos') echo 1001;; *) return 1;; esac; }
stat() { echo rocky; }
sudo() { printf '%s\n' "$*" >> "$TRACE"; if [ "$1" = useradd ]; then created=1; fi; }
awk() { :; }
[() {
 case "$*" in
 '-e /home/linuxbrew/.linuxbrew ]'|'-L /home/linuxbrew/.linuxbrew ]'|'-L /home ]'|'-L /home/linuxbrew ]') return 1;;
 '-d /home/linuxbrew ]'|'-e /home/linuxbrew ]'|'-d /home ]'|'-e /home ]') return 0;;
 *) builtin [ "$@";;
 esac
}
source "$SCRIPT"
'''
    import os
    p=subprocess.run(['/bin/bash','-c',shell],env=dict(os.environ,TRACE=str(trace),SCRIPT=str(SCRIPT)),capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    calls=trace.read_text().splitlines()
    assert any('install -d' in c and c.endswith('/home/linuxbrew/.linuxbrew') for c in calls)
    assert not any('install -d' in c and ' /home/linuxbrew /home/linuxbrew/.linuxbrew' in c for c in calls),calls


def test_symlink_parent_has_an_explicit_refusal_before_account_creation():
    s=SCRIPT.read_text()
    assert 'homebrew_parent_unsafe' in s
    assert s.index('homebrew_parent_unsafe') < s.index('sudo useradd')
