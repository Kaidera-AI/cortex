"""Encrypted provisioning without a shell `cat` (J06: Core's open_bootstrap_resources(...).transaction('bootstrap') runs Nix's FIXED provisioning SQL; the secret is a BOUND PARAMETER taken from
the actual custody, never text, never a file, never argv). roles.provision_steps(binding, phase, seed_ack=False) -> [(sql, secret_purpose | None)]: the executor runs them in order on the one
Core-owned transaction, passing the named purpose's plaintext (from custody.resolve) as the single %s parameter. The psql scripts (render_*) stay byte-identical (the nine PG bodies run them)."""
import hashlib
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import roles as R  # noqa: E402
from _fixtures import LOGINS, config  # noqa: E402

GOLDEN = {'pre': '1dd3c5ad0856eeb2cd613673b740fec9cac3189b47400472d841c68e208287a9', 'post': '9a7e2436e3c54e4b1142dedffd0e0adcf979aceb6129e37572528b9543b03b33',
          'close': 'fb3132f8d9a34dc8fdd58777c9004f96c3055dea56ec43c8c8edadca128b1293', 'ledger': '8a731e7a2cf71e5eedf007105e125317006a9109f55e69bc4260274f7f471d57'}
SET = "SELECT set_config('cortex.provision_pw', %s, true)"
CLEAR = "SELECT set_config('cortex.provision_pw', '', true)"


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.b = R.bind(config(Path('/vault-golden')))
        self.assertTrue(hasattr(R, 'provision_steps'), 'roles.provision_steps is the fixed provisioning SQL for the encrypted-custody executor')

    def body(self, script):
        """the statements of a psql script: no meta line, no BEGIN/COMMIT"""
        lines = script.rstrip('\n').splitlines()
        return '\n'.join(l for l in lines if l not in ('\\set ON_ERROR_STOP on', 'BEGIN;', 'COMMIT;'))


class Golden(Base):
    def test_the_psql_scripts_are_byte_identical_to_the_ones_the_nine_pg_bodies_run(self):
        self.assertEqual({'pre': digest(R.render_pre(self.b)), 'post': digest(R.render_post(self.b)), 'close': digest(R.render_close(self.b, True)),
                          'ledger': digest(R.render_ledger_grant(self.b))}, GOLDEN)


class Steps(Base):
    def test_pre_binds_each_secret_once_in_the_finite_first_order_and_clears_it(self):
        steps = R.provision_steps(self.b, 'pre')
        bound = [s for s in steps if s[1] is not None]
        self.assertEqual([s[1] for s in bound], ['migrator', 'installer_enrollment', 'api_request', 'probe'])
        self.assertTrue(all(s[0] == SET for s in bound))
        for i, (sql, purpose) in enumerate(steps):
            if purpose is not None:
                self.assertIn('CREATE ROLE', steps[i + 1][0])
                self.assertEqual(steps[i + 2], (CLEAR, None))
        self.assertEqual(sum(1 for s in steps if s[0] == CLEAR), 4)

    def test_no_step_carries_a_secret_value_a_file_read_a_shell_or_a_psql_meta_command(self):
        for phase, ack in (('pre', False), ('post', False), ('ledger', False), ('close', True)):
            for sql, purpose in R.provision_steps(self.b, phase, seed_ack=ack):
                for bad in ('cat ', '\\set', ":'pw'", '`', 'BEGIN;', 'COMMIT;', '/secrets/'):
                    self.assertNotIn(bad, sql, (phase, bad))
                self.assertEqual('%s' in sql, purpose is not None, (phase, sql[:60]))

    def test_every_pre_do_block_checks_the_secret_length_and_alters_the_exact_login_with_the_policy_flags(self):
        steps = R.provision_steps(self.b, 'pre')
        blocks = [s[0] for s in steps if s[0].startswith('DO $$')]
        self.assertEqual(len(blocks), 4)
        for purpose, block in zip(('migrator', 'installer_enrollment', 'api_request', 'probe'), blocks):
            login = self.b.logins[purpose]
            self.assertIn(f'secret_missing_or_short:{login}', block)
            self.assertIn(f"length(pw) >= {R.MIN_SECRET}", block)
            self.assertIn(R._flags(self.b.policy[purpose]), block)
            self.assertIn(f"'{login}'", block)
            self.assertIn("current_setting('cortex.provision_pw')", block)

    def test_pre_never_touches_the_bootstrap_login_or_the_two_groups(self):
        text = '\n'.join(s[0] for s in R.provision_steps(self.b, 'pre'))
        for name in (self.b.logins['bootstrap'], R.REQUEST_GROUP, R.VERIFIER_GROUP):
            self.assertNotIn(name, text)

    def test_post_ledger_and_close_steps_are_exactly_the_script_bodies(self):
        join = lambda phase, ack=False: '\n'.join(s[0] for s in R.provision_steps(self.b, phase, seed_ack=ack))
        self.assertEqual(join('post'), self.body(R.render_post(self.b)))
        self.assertEqual(join('ledger'), self.body(R.render_ledger_grant(self.b)))
        self.assertEqual(join('close', True), self.body(R.render_close(self.b, True)))
        self.assertTrue(all(s[1] is None for p, a in (('post', False), ('ledger', False), ('close', True)) for s in R.provision_steps(self.b, p, seed_ack=a)))

    def test_close_needs_the_durable_seed_acknowledgement_and_unknown_phases_are_refused(self):
        with self.assertRaises(R.RoleRefusal) as e:
            R.provision_steps(self.b, 'close')
        self.assertEqual(e.exception.reason, 'seed_not_acknowledged')
        with self.assertRaises(R.RoleRefusal) as e:
            R.provision_steps(self.b, 'serve')
        self.assertEqual(e.exception.reason, 'provision_phase')

    def test_the_steps_are_plain_detached_data(self):
        steps = R.provision_steps(self.b, 'pre')
        self.assertIsInstance(steps, list)
        self.assertTrue(all(type(s) is tuple and type(s[0]) is str and (s[1] is None or type(s[1]) is str) for s in steps))
        steps.clear()
        self.assertTrue(R.provision_steps(self.b, 'pre'))


if __name__ == '__main__':
    unittest.main()
