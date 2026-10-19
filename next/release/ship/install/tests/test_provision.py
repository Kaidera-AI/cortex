"""Installer caller tests that survive the day-3 fixed sequence. RETIRED here (disclosed): the callback-API classes RunPhase and Sequence and their doubles tested the removed
`provision_sequence(..., migrate=, seed=)` and `run_phase(..., seed_ack=)`; their predicates (order, one transaction per stage, bound parameters, failure stops everything, ledger flag,
no retry, no default) are re-asserted against the fixed sequence in tests/test_provision_fixed.py. Kept: roles.bind_logins and the module-purity guard."""
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import roles as R  # noqa: E402
from _fixtures import LOGINS, REQUEST_GROUP  # noqa: E402


class BindLogins(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(R, 'bind_logins'), 'roles.bind_logins')

    def test_the_five_nonsecret_logins_bind_with_the_same_name_rules_as_the_three_views(self):
        b = R.bind_logins(dict(LOGINS), '/vault')
        self.assertEqual((b.logins, b.vault_root, b.secrets), (LOGINS, '/vault', {}))
        for bad, reason in ((dict(LOGINS, probe=REQUEST_GROUP), 'role_name_conflict:probe'), (dict(LOGINS, installer_enrollment='mine'), 'role_name_conflict:installer_enrollment'),
                            (dict(LOGINS, probe="a'b"), 'role_name_unsafe:probe'), (dict(LOGINS, probe=LOGINS['api_request']), 'role_config_collision:login')):
            with self.assertRaises(R.RoleRefusal) as e:
                R.bind_logins(bad, '/vault')
            self.assertEqual(e.exception.reason, reason)
        partial = {k: v for k, v in LOGINS.items() if k != 'probe'}
        with self.assertRaises(R.RoleRefusal) as e:
            R.bind_logins(partial, '/vault')
        self.assertEqual(e.exception.reason, 'role_config_incomplete:probe')


class Purity(unittest.TestCase):
    def test_the_module_reads_no_environment_file_process_or_shell(self):
        text = (HERE.parent / 'provision.py').read_text()
        for bad in ('subprocess', 'os.environ', 'getenv', 'read_text', 'os.system', 'tempfile', 'shutil', 'argv'):
            self.assertNotIn(bad, text, bad)
        self.assertIsNone(re.search(r'(?<![\w.])open\(', text), 'no file open')


if __name__ == '__main__':
    unittest.main()
