"""Causal controls for the day-3 fixed provision + catalog compatibility. Each mutant breaks ONE rule in place (restored in `finally`); the UNCHANGED test_provision_fixed and
test_catalog_install must then fail. Author-run counts, not an independent replay. (The retired callback-API mutants of the old test_provision.py are replaced by these.)
usage: python3 mutate_provision.py [out.json]"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALL = HERE.parent
SRC = HERE.parents[3] / 'src'
PROV, ROLES, CATALOG = INSTALL / 'provision.py', INSTALL / 'roles.py', SRC / 'cortex_core/runtime/catalog.py'
MUTANTS = [
    ('password_through_the_public_resolve', PROV, "core._installer_password(config, custody=custody, deadline=deadline, purpose=purpose)", "custody.resolve(purpose)"),
    ('migrate_before_post_order_swapped', PROV, "for phase in ('PRE', 'MIGRATE', 'POST', 'LEDGER'):", "for phase in ('PRE', 'POST', 'MIGRATE', 'LEDGER'):"),
    ('ledger_flag_ignored', PROV, "if phase == 'LEDGER' and not LEDGER_ACCEPTED:", "if False:"),
    ('config_mode_not_checked', PROV, "if getattr(config, 'mode', None) != 'install':", "if False:"),
    ('returns_the_raw_seed_result_not_the_validated_receipt', PROV, "    return receipt\n", "    return result\n"),
    ('ack_skipped', PROV, "receipt = core._reload_seed_receipt(config, custody=custody, operation_key=operation_key, deadline=deadline, result=result)", "receipt = result"),
    ('seed_gets_another_operation_key', PROV, "core.seed_runtime_bindings(config, operation_key, deadline)", "core.seed_runtime_bindings(config, 'other-key', deadline)"),
    ('close_rendered_without_the_acknowledgement', PROV, "seed_ack=True if phase == 'CLOSE' else False", "seed_ack=False"),
    ('close_runs_outside_the_stage_guard', PROV, "    with core._installer_phase(config, custody=custody, deadline=deadline, phase=phase):\n        steps =", "    if True:\n        steps ="),
    ('phase_guard_wrong_phase_name', PROV, "phase='SEED'", "phase='ACK'"),
    ('catalog_accepts_live_resources_of_another_config', ROLES, "or live._config is not config", "or False"),
    ('catalog_admits_an_install_config_for_any_phase', ROLES, "        elif config.mode != phase:\n", "        elif False:\n"),
    ('catalog_install_without_the_core_current_check', ROLES, "\n                or resources.current_seed_resources(config, live._deadline) is not live):", "):"),
    ('validated_seed_does_not_pass_the_live_resources', CATALOG, "**({'live':resources} if resources._config.mode=='install' else {})", ""),
]


def main(out=None):
    rows = []
    for name, path, old, new in MUTANTS:
        original = path.read_bytes()
        text = original.decode()
        assert text.count(old) == 1, f'{name}: pattern must occur once ({text.count(old)})'
        try:
            path.write_bytes(text.replace(old, new).encode())
            env = {'PATH': os.environ['PATH'], 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(SRC)}
            proc = subprocess.run([sys.executable, '-m', 'unittest', 'test_provision_fixed', 'test_catalog_install'], cwd=HERE, capture_output=True, text=True, timeout=300, env=env)
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
