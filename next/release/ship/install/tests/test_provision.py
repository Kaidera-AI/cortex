"""Installer caller (J06): provision.run_phase runs roles.provision_steps in ONE actual Core bootstrap transaction (Core's open_bootstrap_resources(config, custody=, deadline=) ->
.transaction('bootstrap')), resolving each secret from the encrypted custody at execution and passing it as a BOUND parameter; provision.provision_sequence orders pre -> Core migrate ->
post -> (ledger, explicitly PENDING Kai's decision) -> Finn's durable seed acknowledgement -> close. The Core resource opener and the custody are DOUBLES here (source-only credit): the real
connector needs psycopg + verify-full TLS. NOT proved here: a run through Core's real opener against a real PostgreSQL (test_ledger_acl_pg.py proves provision_steps' SQL and
bound-parameter semantics on real PG with its own driver; the Core opener + TLS + encrypted custody path stays an open installed-gate item)."""
import re
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import roles as R  # noqa: E402
from _fixtures import FIVE, INSTALLER, LOGINS, REQUEST_GROUP  # noqa: E402


class Refusal(Exception):
    """stands in for cortex_core.runtime.signed_member.Refusal: raised by the custody double"""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class FakeConnection:
    def __init__(self, log, fail_at=None):
        self.log, self.fail_at, self.n = log, fail_at, 0

    def execute(self, sql, params=None):
        self.n += 1
        if self.fail_at == self.n:
            raise RuntimeError('boom')
        self.log.append(('execute', sql, params))


class FakeCustody:
    def __init__(self, log, values=None, refuse=()):
        self.log, self.refuse = log, set(refuse)
        self.values = values or {p: f'pw-{p}-' + 'x' * 24 for p in FIVE}

    def resolve(self, purpose):
        self.log.append(('resolve', purpose))
        if purpose in self.refuse:
            raise Refusal('custody_purpose')
        v = self.values[purpose]
        return v if isinstance(v, bytes) else v.encode()


def seed_config(mode='seed'):
    return SimpleNamespace(mode=mode, role_logins=dict(LOGINS), vault_root='/vault', database={'name': 'cortex'}, installation_id='11111111-1111-4111-8111-111111111111')


class Base(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(R, 'bind_logins'), 'roles.bind_logins')
        try:
            import provision as P
        except ImportError:
            self.fail('provision.py (the installer caller) does not exist')
        self.P = P
        self.log = []
        self.opened = []
        self.fail_at = None
        log, opened = self.log, self.opened

        @contextmanager
        def fake_open(config, *, custody, deadline):
            opened.append((config, custody, deadline))
            log.append(('open', deadline))
            res = SimpleNamespace()

            @contextmanager
            def transaction(purpose):
                log.append(('begin', purpose))
                try:
                    yield FakeConnection(log, self.fail_at)
                    log.append(('commit', purpose))
                except BaseException:
                    log.append(('rollback', purpose))
                    raise
            res.transaction = transaction
            try:
                yield res
            finally:
                log.append(('close',))
        self._orig = P._open
        P._open = fake_open
        self.addCleanup(setattr, P, '_open', self._orig)
        self.custody = FakeCustody(log)

    def run_phase(self, phase, **kw):
        return self.P.run_phase(seed_config(), custody=self.custody, deadline=123.5, phase=phase, **kw)

    def executes(self):
        return [e for e in self.log if e[0] == 'execute']


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


class RunPhase(Base):
    def test_one_actual_resource_context_one_bootstrap_transaction_the_steps_in_order(self):
        self.run_phase('pre')
        b = R.bind_logins(dict(LOGINS), '/vault')
        want = [s[0] for s in R.provision_steps(b, 'pre')]
        self.assertEqual([e[1] for e in self.executes()], want)
        self.assertEqual([e[0] for e in self.log if e[0] != 'execute' and e[0] != 'resolve'], ['open', 'begin', 'commit', 'close'])
        self.assertEqual(self.opened[0][2], 123.5)
        self.assertEqual(self.log[1], ('begin', 'bootstrap'))

    def test_each_secret_is_resolved_at_execution_and_bound_as_the_single_parameter(self):
        self.run_phase('pre')
        resolves = [e[1] for e in self.log if e[0] == 'resolve']
        self.assertEqual(resolves, ['migrator', 'installer_enrollment', 'api_request', 'probe'])
        bound = [e for e in self.executes() if e[2] is not None]
        self.assertEqual([e[2] for e in bound], [(self.custody.values[p],) for p in resolves])
        for e in self.executes():
            for v in self.custody.values.values():
                self.assertNotIn(v, e[1])
        # lazily: the resolve of a purpose comes after the previous step's execution
        order = [e[0] if e[0] != 'execute' else 'x' for e in self.log]
        self.assertLess(order.index('resolve'), len(order))
        first_two = [i for i, e in enumerate(self.log) if e[0] == 'resolve']
        self.assertGreater(first_two[0], 2)  # after the two SET LOCAL steps

    def test_post_and_close_need_no_secret_and_close_needs_the_seed_acknowledgement(self):
        self.run_phase('post')
        self.assertEqual([e for e in self.log if e[0] == 'resolve'], [])
        self.log.clear()
        with self.assertRaises(R.RoleRefusal) as e:
            self.run_phase('close')
        self.assertEqual(e.exception.reason, 'seed_not_acknowledged')
        self.assertEqual(self.log, [], 'refused before opening any resource')
        self.run_phase('close', seed_ack=True)
        self.assertTrue(self.executes())

    def test_unknown_phase_and_a_non_seed_config_are_refused_before_any_resource(self):
        with self.assertRaises(R.RoleRefusal) as e:
            self.run_phase('serve')
        self.assertEqual(e.exception.reason, 'provision_phase')
        with self.assertRaises(R.RoleRefusal) as e:
            self.P.run_phase(seed_config('serve'), custody=self.custody, deadline=1.0, phase='post')
        self.assertEqual(e.exception.reason, 'provision_config')
        self.assertEqual(self.log, [])

    def test_a_failing_step_stops_everything_rolls_back_and_is_never_retried(self):
        for at in (1, 3, 4, 6):  # a plain step, a secret-bound step, the DO block, a later bound step
            self.log.clear()
            self.fail_at = at
            with self.assertRaises(RuntimeError):
                self.run_phase('pre')
            self.assertEqual(len(self.executes()), at - 1)
            self.assertIn(('rollback', 'bootstrap'), self.log)
            self.assertNotIn(('commit', 'bootstrap'), self.log)
            self.assertEqual(self.log[-1], ('close',))

    def test_a_custody_refusal_propagates_unchanged_and_no_default_is_tried(self):
        self.custody = FakeCustody(self.log, refuse={'migrator'})
        with self.assertRaises(Refusal) as e:
            self.run_phase('pre')
        self.assertEqual(e.exception.reason, 'custody_purpose')
        self.assertEqual([x[1] for x in self.log if x[0] == 'resolve'], ['migrator'])
        self.assertIn(('rollback', 'bootstrap'), self.log)

    def test_an_empty_or_nul_secret_is_refused_before_it_is_bound(self):
        for bad in (b'', b'a\x00b', b'\xff\xfe'):
            self.log.clear()
            self.custody = FakeCustody(self.log, values={p: bad for p in FIVE})
            with self.assertRaises(R.RoleRefusal) as e:
                self.run_phase('pre')
            self.assertEqual(e.exception.reason, 'provision_secret')
            self.assertTrue(all(x[2] is None for x in self.executes()))

    def test_the_ledger_phase_is_explicitly_pending_until_it_is_accepted(self):
        self.assertIs(self.P.LEDGER_ACCEPTED, False)
        with self.assertRaises(R.RoleRefusal) as e:
            self.run_phase('ledger')
        self.assertEqual(e.exception.reason, 'ledger_not_accepted')
        self.assertEqual(self.log, [])
        self.P.LEDGER_ACCEPTED = True
        self.addCleanup(setattr, self.P, 'LEDGER_ACCEPTED', False)
        self.run_phase('ledger')
        self.assertTrue(any('schema_migrations' in e[1] for e in self.executes()))

    def test_the_module_reads_no_environment_file_process_or_shell(self):
        text = (HERE.parent / 'provision.py').read_text()
        for bad in ('subprocess', 'os.environ', 'getenv', 'read_text', 'os.system', 'tempfile', 'shutil', 'argv'):
            self.assertNotIn(bad, text, bad)
        self.assertIsNone(re.search(r'(?<![\w.])open\(', text), 'no file open')


class Sequence(Base):
    def seq(self, migrate=None, seed=None, **kw):
        order = []
        m = migrate or (lambda: order.append('migrate'))
        s = seed or (lambda: order.append('seed') or True)
        self.P.provision_sequence(seed_config(), custody=self.custody, deadline=9.0, migrate=m, seed=s, **kw)
        return order

    def test_pre_then_migrate_then_post_then_seed_then_close_with_the_ledger_skipped_while_pending(self):
        calls = []
        orig = self.P.run_phase

        def spy(config, **kw):
            calls.append(kw['phase'])
            return orig(config, **kw)
        self.P.run_phase = spy
        self.addCleanup(setattr, self.P, 'run_phase', orig)
        self.seq(migrate=lambda: calls.append('migrate'), seed=lambda: calls.append('seed') or True)
        self.assertEqual(calls, ['pre', 'migrate', 'post', 'seed', 'close'])

    def test_a_seed_that_does_not_return_true_never_reaches_close(self):
        for bad in (False, None, 1, 'yes'):
            self.log.clear()
            with self.assertRaises(R.RoleRefusal) as e:
                self.seq(seed=lambda b=bad: b)
            self.assertEqual(e.exception.reason, 'seed_not_acknowledged')
            self.assertEqual(sum(1 for x in self.log if x[0] == 'begin'), 2)  # pre and post only

    def test_a_failing_migration_stops_before_post_seed_and_close(self):
        def boom():
            raise RuntimeError('migrate failed')
        with self.assertRaises(RuntimeError):
            self.seq(migrate=boom)
        self.assertEqual(sum(1 for x in self.log if x[0] == 'begin'), 1)

    def test_every_phase_opens_its_own_resource_context_with_the_same_config_custody_and_deadline(self):
        self.seq()
        self.assertEqual(len(self.opened), 3)
        self.assertTrue(all(o[1] is self.custody and o[2] == 9.0 for o in self.opened))

    def test_the_pending_ledger_runs_only_when_explicitly_accepted(self):
        self.P.LEDGER_ACCEPTED = True
        self.addCleanup(setattr, self.P, 'LEDGER_ACCEPTED', False)
        self.seq()
        self.assertEqual(len(self.opened), 4)
        self.assertTrue(any('schema_migrations' in e[1] for e in self.executes()))


if __name__ == '__main__':
    unittest.main()
