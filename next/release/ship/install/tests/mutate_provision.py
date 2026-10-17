"""Causal controls for provision.py: each mutant must make tests/test_provision.py FAIL (assertion failure). Run: python3 mutate_provision.py"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / 'provision.py'
LED = "    run_phase(config, custody=custody, deadline=deadline, phase='ledger')  # never skipped: a guard turned off refuses here, before seed and close\n"
MUTANTS = [
    ('secret_into_statement_text', "connection.execute(sql, (_secret(custody, purpose),))", "connection.execute(sql % _secret(custody, purpose))"),
    ('secret_resolved_eagerly_outside_txn', "with resources.transaction('bootstrap') as connection:", "[_secret(custody, p) for _, p in steps if p]\n        with resources.transaction('bootstrap') as connection:"),
    ('wrong_transaction_purpose', "resources.transaction('bootstrap')", "resources.transaction('migrator')"),
    ('ledger_flag_default_off', "LEDGER_ACCEPTED = True  #", "LEDGER_ACCEPTED = False  #"),
    ('ledger_skipped_in_sequence', LED, ''),
    ('ledger_skipped_when_flag_off', LED, '    if LEDGER_ACCEPTED:\n    ' + LED.lstrip()),
    ('ledger_moved_after_seed', LED + "    if seed() is not True:\n        raise R.RoleRefusal('seed_not_acknowledged')\n", "    if seed() is not True:\n        raise R.RoleRefusal('seed_not_acknowledged')\n" + LED),
    ('ledger_for_wrong_phase_name', "phase='ledger')  # never", "phase='post')  # never"),
    ('close_without_seed_true', "if seed() is not True:", "if seed() is None:"),
    ('seed_checked_after_close', "    if seed() is not True:\n        raise R.RoleRefusal('seed_not_acknowledged')\n    run_phase(config, custody=custody, deadline=deadline, phase='close', seed_ack=True)",
     "    run_phase(config, custody=custody, deadline=deadline, phase='close', seed_ack=True)\n    if seed() is not True:\n        raise R.RoleRefusal('seed_not_acknowledged')"),
    ('migrate_skipped', "    migrate()\n", ""),
    ('non_seed_config_accepted', "if getattr(config, 'mode', None) != 'seed':", "if False:"),
    ('empty_secret_accepted', "if type(raw) is not bytes or not raw:", "if type(raw) is not bytes:"),
    ('nul_secret_accepted', "    if '\\x00' in text:\n        raise R.RoleRefusal('provision_secret')\n", ""),
    ('swallow_step_error', "                    connection.execute(sql, (_secret(custody, purpose),))", "                    try:\n                        connection.execute(sql, (_secret(custody, purpose),))\n                    except RuntimeError:\n                        pass"),
    ('env_read_added', "def _secret(custody, purpose):", "import os\n\n\ndef _secret(custody, purpose):\n    os.environ.get('X')"),
]
original = SRC.read_text()
results = []
try:
    for name, old, new in MUTANTS:
        assert original.count(old) == 1, name
        SRC.write_text(original.replace(old, new))
        r = subprocess.run([sys.executable, '-m', 'unittest', 'test_provision'], cwd=HERE, capture_output=True, text=True)
        results.append({'mutant': name, 'killed': r.returncode != 0})
finally:
    SRC.write_text(original)
print(json.dumps(results, indent=1))
sys.exit(0 if all(x['killed'] for x in results) else 1)
