"""Causal controls for the S2 correction: each mutant re-introduces ONE defect or removes ONE guard in config_writer.py (restored in `finally`); the UNCHANGED test_write_config_faults must then fail.
Author-run counts, not an independent replay. No test here creates a key. usage: python3 mutate_write_config_faults.py [out.json]"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WRITER = HERE.parent / 'config_writer.py'
MUTANTS = [
    ('rollback_never_removes_the_final', "                try:\n                    os.unlink(FILE, dir_fd=cfg_fd)\n                except OSError:\n                    incomplete = True", "                pass"),
    ('rollback_ignores_file_identity', "if ident is not None and (leaf.st_dev, leaf.st_ino) == ident:", "if True:"),
    ('no_directory_fsync_after_the_temp_removal', "        state['tmp'] = False\n        os.fsync(cfg_fd)\n    except OSError:", "        state['tmp'] = False\n    except OSError:"),
    ('no_directory_fsync_after_the_link', "        state['linked'] = True\n        os.fsync(cfg_fd)\n", "        state['linked'] = True\n"),
    ('an_incomplete_rollback_is_reported_as_an_ordinary_failure', "return 'config_publish_incomplete' if incomplete else 'config_write_failed'", "return 'config_write_failed'"),
    ('a_temp_unlink_failure_is_swallowed_again', "        os.unlink(tmp, dir_fd=cfg_fd)\n        state['tmp'] = False", "        try:\n            os.unlink(tmp, dir_fd=cfg_fd)\n        except OSError:\n            pass\n        state['tmp'] = False"),
    ('the_new_directory_descriptor_is_not_closed', "    except OSError:\n        os.close(fd)\n        raise Refusal('config_write_failed') from None\n    except BaseException:", "    except OSError:\n        raise Refusal('config_write_failed') from None\n    except BaseException:"),
    ('a_directory_check_error_escapes_raw', "    try:\n        st = os.fstat(fd)\n    except OSError:\n        raise Refusal('config_write_failed') from None   # a failing check is a typed refusal, never a raw OSError\n", "    st = os.fstat(fd)\n"),
    ('a_directory_fchmod_error_escapes_raw', "    try:   # the newly acquired descriptor is owned here: every failure closes it and is typed\n        os.fchmod(fd, 0o700)\n        _check_dir(fd, owner, strict=True)\n    except OSError:\n        os.close(fd)\n        raise Refusal('config_write_failed') from None\n    except BaseException:\n        os.close(fd)\n        raise\n", "    os.fchmod(fd, 0o700)\n    _check_dir(fd, owner, strict=True)\n"),
    ('a_lost_link_race_overwrites_via_rename', "            os.link(tmp, FILE, src_dir_fd=cfg_fd, dst_dir_fd=cfg_fd, follow_symlinks=False)", "            os.rename(tmp, FILE, src_dir_fd=cfg_fd, dst_dir_fd=cfg_fd)"),
]


def main(out=None):
    rows = []
    original = WRITER.read_bytes()
    text = original.decode()
    for name, old, new in MUTANTS:
        assert text.count(old) == 1, f'{name}: pattern must occur once ({text.count(old)})'
        try:
            WRITER.write_bytes(text.replace(old, new).encode())
            proc = subprocess.run([sys.executable, '-m', 'unittest', 'test_write_config_faults'], cwd=HERE, capture_output=True, text=True, timeout=300,
                                  env={'PATH': os.environ['PATH'], 'PYTHONDONTWRITEBYTECODE': '1'})
        finally:
            WRITER.write_bytes(original)
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
