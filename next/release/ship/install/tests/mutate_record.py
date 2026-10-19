"""Causal controls for S5 record.py and the install.sh delegation: each mutant breaks ONE rule in place (restored in `finally`); the UNCHANGED test_record must then fail.
Author-run counts, not an independent replay. No test here creates a key. usage: python3 mutate_record.py [out.json]"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALL = HERE.parent
REC, SH = INSTALL / 'record.py', INSTALL / 'install.sh'
MUTANTS = [
    ('record_file_followed_through_a_symlink', REC, "os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK", "os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK"),
    ('prefix_followed_through_a_symlink', REC, "_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY", "_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY"),
    ('another_release_overwritten', REC, "        if doc is not None and doc['release_id'] != release_id:\n            raise RecordRefusal('record_foreign_release')   # never overwrite another installation's ownership\n", ""),
    ('prefix_owner_not_checked', REC, "        st = os.fstat(fd)\n        if st.st_uid != owner:\n            raise RecordRefusal('record_owner')\n        if st.st_mode & 0o077:", "        st = os.fstat(fd)\n        if False:\n            raise RecordRefusal('record_owner')\n        if st.st_mode & 0o077:"),
    ('record_owner_not_checked', REC, "        if st.st_uid != owner:\n            raise RecordRefusal('record_owner')\n        if st.st_mode & 0o077 or st.st_nlink != 1:", "        if False:\n            raise RecordRefusal('record_owner')\n        if st.st_mode & 0o077 or st.st_nlink != 1:"),
    ('loose_prefix_mode_accepted', REC, "        if st.st_mode & 0o077:\n            raise RecordRefusal('record_private')\n        return fd", "        if False:\n            raise RecordRefusal('record_private')\n        return fd"),
    ('hard_linked_record_accepted', REC, " or st.st_nlink != 1:", ":"),
    ('duplicate_json_keys_accepted', REC, "object_pairs_hook=_no_duplicates, ", ""),
    ('non_finite_numbers_accepted', REC, ", parse_constant=_refuse_constant", ""),
    ('forget_writes_when_the_name_is_absent', REC, "        if doc is None or all(o['name'] != name for o in doc['objects']):\n            return False", "        if doc is None:\n            return False"),
    ('temp_file_left_behind', REC, "            try:\n                os.unlink(tmp, dir_fd=dirfd)\n            except OSError:\n                pass", "            pass"),
    ('output_format_changed', REC, "json.dumps(doc, sort_keys=True, indent=1)", "json.dumps(doc, sort_keys=True, indent=2)"),
    ('objects_not_sorted', REC, "sorted(doc['objects'], key=lambda o: o['name'])", "list(doc['objects'])"),
    ('extra_record_keys_accepted', REC, "type(doc) is not dict or set(doc) != {'release_id', 'objects'}", "type(doc) is not dict"),
    ('inputs_not_validated', REC, "    for value in (release_id, kind, name, ident):\n        _field(value)\n", ""),
    ('shell_swallows_the_module_refusal', SH, '--id "$3" || exit 2', '--id "$3" || true'),
    ('shell_keeps_a_second_embedded_writer', SH, "  mkdir -p \"$prefix\" && chmod 700 \"$prefix\"\n  python3 -I -B", "  mkdir -p \"$prefix\" && chmod 700 \"$prefix\"\n  : os.replace json.dump\n  python3 -I -B"),
]


def main(out=None):
    rows = []
    for name, path, old, new in MUTANTS:
        original = path.read_bytes()
        text = original.decode()
        assert text.count(old) == 1, f'{name}: pattern must occur once ({text.count(old)})'
        try:
            path.write_bytes(text.replace(old, new).encode())
            proc = subprocess.run([sys.executable, '-m', 'unittest', 'test_record'], cwd=HERE, capture_output=True, text=True, timeout=300,
                                  env={'PATH': os.environ['PATH'], 'PYTHONDONTWRITEBYTECODE': '1'})
        finally:
            path.write_bytes(original)
        failed = sorted({l.split(' ')[1] for l in proc.stderr.splitlines() if l.startswith(('FAIL:', 'ERROR:'))})
        rows.append({'mutant': name, 'killed': proc.returncode != 0, 'n_failing': len(failed), 'failing_tests': failed[:3]})
        print(('KILLED   ' if proc.returncode else 'SURVIVED ') + name, flush=True)
    summary = {'mutants': len(rows), 'killed': sum(r['killed'] for r in rows), 'rows': rows}
    if out:
        Path(out).write_text(json.dumps(summary, indent=1) + '\n')
    print(f"{summary['killed']}/{summary['mutants']} killed")
    return 0 if summary['killed'] == summary['mutants'] else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
