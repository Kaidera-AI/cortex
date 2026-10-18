"""Causal controls for S2 write_config: each mutant breaks ONE rule of config_writer.py or install.sh in place (restored in `finally`), then the UNCHANGED test_write_config
module must fail. Author-run counts, not an independent replay. usage: python3 mutate_write_config.py [out.json]"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALL = HERE.parent
WRITER, SCRIPT = INSTALL / 'config_writer.py', INSTALL / 'install.sh'
MUTANTS = [
    ('writer_follows_symlinks', WRITER, "os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY", "os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY"),
    ('writer_ignores_foreign_owner', WRITER, "    if st.st_uid != owner:\n        raise Refusal('config_owner')", "    if False:\n        raise Refusal('config_owner')"),
    ('writer_accepts_any_project_name', WRITER, "    _input(NAME.fullmatch(project) is not None, 'config_identifier')\n", ""),
    ('writer_accepts_loose_directory_modes', WRITER, "(0o077 if strict else 0o022)", "(0o022 if strict else 0o022)"),
    ('writer_writes_a_weaker_tls_mode', WRITER, "tls={'mode': 'verify-full', 'root_cert_ref': CA_REF}", "tls={'mode': 'require', 'root_cert_ref': CA_REF}"),
    ('writer_overwrites_a_different_config_instead_of_refusing', WRITER, "                _input(_admit(again)[1] == digest, 'config_exists_different')\n", "                pass\n"),
    ('writer_makes_a_world_readable_file', WRITER, "os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=cfg_fd)", "os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=cfg_fd)"),
    ('writer_leaves_the_temporary_file_behind', WRITER, "            try:\n                os.unlink(tmp, dir_fd=cfg_fd)\n            except OSError:\n                pass", "            pass"),
    ('writer_picks_a_wrong_installer_login', WRITER, "'installer_enrollment': R.INSTALLER_LOGIN}", "'installer_enrollment': 'cortex_installer'}"),
    ('script_skips_the_signed_member_digest_of_the_helper', SCRIPT, '''  [ -f "$helper" ] && [ "$(sha_of "$helper")" = "${line%%  *}" ] || refuse artifact_digest_mismatch:config_writer.py\n''', ''),
    ('script_runs_a_helper_that_is_not_listed_in_the_signed_sums', SCRIPT, '''  line=$(grep "  config_writer\\.py\\$" "$release_dir/SHA256SUMS") || refuse artifact_missing:config_writer.py''', '''  line=$(grep "  config_writer\\.py\\$" "$release_dir/SHA256SUMS") || line=''' ),
    ('script_writes_the_config_in_the_full_run_before_make_secret_refuses', SCRIPT, 'for phase in $UNWIRED; do', 'write_config; for phase in $UNWIRED; do'),
    ('script_skips_the_core_source_check', SCRIPT, '  [ -d "$core_src/cortex_core" ] || refuse write_config_core_missing\n', ''),
    ('script_accepts_a_missing_port', SCRIPT, "  case $port in ''|*[!0-9]*) refuse missing_argument:port ;; esac\n", ''),
]


def main(out=None):
    rows = []
    for name, path, old, new in MUTANTS:
        original = path.read_bytes()
        text = original.decode()
        assert text.count(old) == 1, f'{name}: pattern must occur once ({text.count(old)})'
        try:
            path.write_bytes(text.replace(old, new).encode())
            proc = subprocess.run([sys.executable, '-m', 'unittest', 'test_write_config'], cwd=HERE, capture_output=True, text=True, timeout=300,
                                  env={'PATH': __import__('os').environ['PATH'], 'PYTHONDONTWRITEBYTECODE': '1'})
        finally:
            path.write_bytes(original)
        failed = [l.split(' ')[1] for l in proc.stderr.splitlines() if l.startswith(('FAIL:', 'ERROR:'))]
        rows.append({'mutant': name, 'killed': proc.returncode != 0, 'failing_tests': sorted(set(failed))[:4], 'n_failing': len(set(failed))})
        print(('KILLED   ' if proc.returncode else 'SURVIVED ') + name, flush=True)
    summary = {'mutants': len(rows), 'killed': sum(r['killed'] for r in rows), 'rows': rows}
    if out:
        Path(out).write_text(json.dumps(summary, indent=1) + '\n')
    print(f"{summary['killed']}/{summary['mutants']} killed")
    return 0 if summary['killed'] == summary['mutants'] else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
