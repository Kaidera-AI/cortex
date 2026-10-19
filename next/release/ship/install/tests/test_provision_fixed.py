"""S3 fixed provision (day 3): provision.provision_sequence(config, *, custody, deadline, operation_key) is the ONE fixed Nix sequence that Core's owner.run imports (Core2 private join,
INTERFACE.md sha256 34797c35…c703). Each stage = `with custody._installer_phase(config, custody=, deadline=, phase=PHASE):` around the complete actual body AND its resource cleanup, in the exact order
PRE, MIGRATE, POST, LEDGER, SEED, ACK, CLOSE, once. Nix owns the SQL and the order; Core owns admission, cleanup and the private ACK.
FAKE BOUNDARIES (all named, all source-only; none earns full-composition credit): GuardDouble = a double of Core2's `_installer_phase` (it enforces the contract's order/once/context rules so a
Nix mistake is observable; it is NOT Core2); ResourcesDouble = doubles of Core's three resource openers, their one bootstrap transaction and `.migrate()`; PasswordDouble = a double of Core2's
`_installer_password`; SeedDouble = a double of the UNCHANGED `seed_runtime_bindings` (only its call shape is checked); ReloadDouble = a double of `operator._reload_seed_receipt`. Core2's real source is
not present at this base (Core1 f79ead56), so the test installs these names on the real Core modules and the production code under test is the real provision.py. NOT RUN: the real Core2 guard/openers,
real custody/AESGCM/unlock, any PostgreSQL or Podman, and the actual composed Nix+Core2 path."""
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE), str(HERE.parents[3] / 'src')]
import provision as P  # noqa: E402
import roles as R  # noqa: E402
from _fixtures import FIVE, LOGINS  # noqa: E402
from cortex_core.runtime import custody as CUSTODY_MOD, operator as OPERATOR_MOD, resources as RESOURCES_MOD  # noqa: E402
from cortex_core.schema import seed as SEED_MOD  # noqa: E402

ORDER = ('PRE', 'MIGRATE', 'POST', 'LEDGER', 'SEED', 'ACK', 'CLOSE')
FOUR = ('migrator', 'installer_enrollment', 'api_request', 'probe')
PW = {p: f'SYNTHETIC-NONSECRET-pw-{p}-0123456789'.encode() for p in FIVE}
RESULT = {'operation_key': 'op-1', 'synthetic': 'seed-return'}
RECEIPT = {'operation_key': 'op-1', 'synthetic': 'validated-receipt'}
OPKEY = 'op-1'
OPENERS = {'PRE': 'bootstrap', 'POST': 'bootstrap', 'LEDGER': 'bootstrap', 'CLOSE': 'bootstrap', 'MIGRATE': 'migration', 'SEED': 'seed'}


class Refusal(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def install_config(mode='install'):
    return SimpleNamespace(mode=mode, role_logins=dict(LOGINS), vault_root='/vault', database={'name': 'cortex'}, installation_id='11111111-1111-4111-8111-111111111111')


def outcome_of(call):
    """A behavioural outcome, never a setup error: ('ok', value) or (exception class name, reason or message)."""
    try:
        return ('ok', call())
    except TypeError as error:
        return ('TypeError', str(error)[:70])
    except Exception as error:
        return (type(error).__name__, getattr(error, 'reason', getattr(error, 'args', ('',))[0]))


class World:
    """One shared event log plus the Core2-contract state the doubles enforce."""

    def __init__(self, active=True, fail_in=None, fail_at=1):
        self.events, self.done, self.stage, self.open, self.tx = [], [], None, 0, 0
        self.active, self.fail_in, self.fail_at, self.sql_seen = active, fail_in, fail_at, 0
        self.config, self.deadline = install_config(), 12345.0
        self.custody = SimpleNamespace(resolve=lambda purpose: self.events.append(('PUBLIC_RESOLVE', purpose)) or b'x' * 24)

    def guard(self, config, *, custody, deadline, phase):
        return self._guard(config, custody, deadline, phase)

    @contextmanager
    def _guard(self, config, custody, deadline, phase):
        expected = ORDER[len(self.done)] if len(self.done) < len(ORDER) else None
        if not self.active:
            raise Refusal('installer_context')
        if phase != expected or self.stage is not None:
            raise Refusal('installer_phase')
        if config is not self.config or custody is not self.custody or deadline != self.deadline:
            raise Refusal('installer_context')
        self.stage = phase
        self.events.append(('enter', phase))
        try:
            yield
            if self.open:
                raise Refusal('installer_resources')   # existing resources must all close BEFORE guard exit
            self.events.append(('exit', phase))
            self.done.append(phase)
        finally:
            self.stage = None

    def opener(self, kind):
        @contextmanager
        def open_resources(config, *, custody, deadline):
            if OPENERS.get(self.stage) != kind or config is not self.config or custody is not self.custody or deadline != self.deadline:
                raise Refusal('resources_stage')
            self.open += 1
            self.events.append(('open', kind, self.stage))
            resources = SimpleNamespace(transaction=self.transaction, migrate=self.migrate)
            try:
                yield resources
            finally:
                self.open -= 1
                self.events.append(('close', kind, self.stage))
        return open_resources

    @contextmanager
    def transaction(self, purpose):
        self.tx += 1
        self.events.append(('tx_begin', purpose))
        conn = SimpleNamespace(execute=self.execute)
        try:
            yield conn
            self.events.append(('tx_commit',))
        except BaseException:
            self.events.append(('tx_rollback',))
            raise
        finally:
            self.tx -= 1

    def execute(self, sql, params=None):
        self.sql_seen += 1
        if self.fail_in == self.stage and self.fail_at == self.sql_seen_in_stage():
            raise RuntimeError('injected step failure')
        self.events.append(('sql', self.stage, sql, params))

    def sql_seen_in_stage(self):
        return sum(1 for e in self.events if e[0] == 'sql' and e[1] == self.stage) + 1

    def migrate(self):
        if self.tx:
            raise Refusal('migrate_in_transaction')
        if self.fail_in == 'MIGRATE':
            raise RuntimeError('injected migrate failure')
        self.events.append(('migrate', self.stage))
        return []

    def password(self, config, *, custody, deadline, purpose):
        if self.stage != 'PRE' or purpose not in FOUR or config is not self.config or custody is not self.custody:
            raise Refusal('purpose_not_admitted')
        self.events.append(('password', purpose))
        return PW[purpose]

    def seed(self, config, operation_key, deadline, /):
        if self.stage != 'SEED' or not self.open or config is not self.config:
            raise Refusal('seed_context')
        self.events.append(('seed_call', operation_key, deadline))
        return RESULT

    def reload(self, config, *, custody, operation_key, deadline, result):
        if self.stage != 'ACK' or self.open or result is not RESULT:
            raise Refusal('ack_context')
        self.events.append(('ack_reload', operation_key))
        return RECEIPT


class Base(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(P, 'provision_sequence'))

    def world(self, **kw):
        self.w = World(**kw)
        patches = [mock.patch.object(CUSTODY_MOD, '_installer_phase', self.w.guard, create=True),
                   mock.patch.object(CUSTODY_MOD, '_installer_password', self.w.password, create=True),
                   mock.patch.object(RESOURCES_MOD, 'open_bootstrap_resources', self.w.opener('bootstrap')),
                   mock.patch.object(RESOURCES_MOD, 'open_migration_resources', self.w.opener('migration')),
                   mock.patch.object(RESOURCES_MOD, 'open_seed_resources', self.w.opener('seed')),
                   mock.patch.object(SEED_MOD, 'seed_runtime_bindings', self.w.seed),
                   mock.patch.object(OPERATOR_MOD, '_reload_seed_receipt', self.w.reload, create=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return self.w

    def run_sequence(self, w=None, old_style=False, **over):
        w = w or self.w
        kwargs = dict(custody=w.custody, deadline=w.deadline)
        if not old_style:
            kwargs['operation_key'] = OPKEY
        kwargs.update(over)
        return outcome_of(lambda: P.provision_sequence(w.config, **kwargs))

    def phases(self):
        return [e[1] for e in self.w.events if e[0] == 'enter']


class FixedSequence(Base):
    def test_the_seven_stages_run_once_in_order_each_body_and_its_cleanup_inside_its_guard(self):
        w = self.world()
        self.assertEqual(self.run_sequence(), ('ok', RECEIPT))
        self.assertEqual(self.phases(), list(ORDER))
        stage = None
        for event in w.events:
            if event[0] == 'enter':
                stage = event[1]
            elif event[0] == 'exit':
                stage = None
            elif event[0] in ('open', 'close', 'sql', 'migrate', 'seed_call', 'ack_reload', 'password'):
                self.assertIsNotNone(stage, f'{event[0]} happened outside a stage guard')
        kinds = [(e[2], e[1]) for e in w.events if e[0] == 'open']
        self.assertEqual(kinds, [('PRE', 'bootstrap'), ('MIGRATE', 'migration'), ('POST', 'bootstrap'), ('LEDGER', 'bootstrap'), ('SEED', 'seed'), ('CLOSE', 'bootstrap')])
        for index, event in enumerate(w.events):
            if event[0] == 'open':
                closes = [i for i, e in enumerate(w.events) if e[0] == 'close' and i > index][0]
                exits = [i for i, e in enumerate(w.events) if e[0] == 'exit' and i > index][0]
                self.assertLess(closes, exits, 'resources must close before the guard exits')
        self.assertNotIn('PUBLIC_RESOLVE', [e[0] for e in w.events])

    def test_pre_runs_one_bootstrap_transaction_with_the_four_bound_passwords_from_the_private_reader_only(self):
        w = self.world()
        self.run_sequence()
        binding = R.bind_logins(dict(LOGINS), '/vault')
        pre = [e for e in w.events if e[0] == 'sql' and e[1] == 'PRE']
        self.assertEqual([e[2] for e in pre], [sql for sql, _ in R.provision_steps(binding, 'pre')])
        self.assertEqual([e[3] for e in pre if e[3] is not None], [(PW[p].decode(),) for p in FOUR])   # bound as text, like the real-PG proven set_config(%s, true) step
        self.assertEqual([e[1] for e in w.events if e[0] == 'password'], list(FOUR))
        self.assertEqual(sum(1 for e in w.events if e == ('tx_begin', 'bootstrap')), 4, 'PRE, POST, LEDGER and CLOSE each own exactly one bootstrap transaction')
        for secret in PW.values():
            self.assertFalse(any(secret in (e[2].encode() if isinstance(e[2], str) else b'') for e in pre), 'a password must be a bound parameter, never statement text')

    def test_migrate_is_the_actual_engine_call_without_an_outer_transaction(self):
        w = self.world()
        self.run_sequence()
        self.assertEqual([e for e in w.events if e[0] == 'migrate'], [('migrate', 'MIGRATE')])
        index = w.events.index(('migrate', 'MIGRATE'))
        self.assertNotIn(('tx_begin', 'bootstrap'), w.events[w.events.index(('enter', 'MIGRATE')):index])

    def test_post_and_ledger_are_separate_bootstrap_transactions_with_the_existing_sql_and_the_api_only_grant(self):
        w = self.world()
        self.run_sequence()
        binding = R.bind_logins(dict(LOGINS), '/vault')
        for stage, phase in (('POST', 'post'), ('LEDGER', 'ledger')):
            got = [e[2] for e in w.events if e[0] == 'sql' and e[1] == stage]
            self.assertEqual(got, [sql for sql, _ in R.provision_steps(binding, phase)], stage)
        ledger = ' '.join(e[2] for e in w.events if e[0] == 'sql' and e[1] == 'LEDGER')
        self.assertIn(f'"{LOGINS["api_request"]}"', ledger)
        for other in ('probe', 'installer_enrollment', 'migrator', 'bootstrap'):
            self.assertNotIn(f'TO "{LOGINS[other]}"', ledger, other)
        self.assertNotIn('PUBLIC', ledger)

    def test_seed_calls_the_unchanged_seed_function_with_its_positional_signature_inside_open_seed_resources(self):
        w = self.world()
        self.run_sequence()
        calls = [e for e in w.events if e[0] == 'seed_call']
        self.assertEqual([(c[1], c[2]) for c in calls], [(OPKEY, w.deadline)])
        enter, exit_ = w.events.index(('enter', 'SEED')), w.events.index(('exit', 'SEED'))
        self.assertLess(w.events.index(('open', 'seed', 'SEED')), w.events.index(calls[0]))
        self.assertLess(w.events.index(calls[0]), w.events.index(('close', 'seed', 'SEED')))
        self.assertTrue(enter < w.events.index(calls[0]) < exit_)

    def test_ack_reloads_through_the_shared_helper_only_after_the_seed_resources_closed_and_its_receipt_is_returned(self):
        w = self.world()
        outcome = self.run_sequence()
        self.assertEqual(outcome, ('ok', RECEIPT))
        self.assertEqual([e for e in w.events if e[0] == 'ack_reload'], [('ack_reload', OPKEY)])
        self.assertLess(w.events.index(('close', 'seed', 'SEED')), w.events.index(('ack_reload', OPKEY)))
        self.assertLess(w.events.index(('ack_reload', OPKEY)), w.events.index(('enter', 'CLOSE')))

    def test_close_renders_the_acknowledged_sql_only_inside_the_admitted_close_stage(self):
        w = self.world()
        self.run_sequence()
        binding = R.bind_logins(dict(LOGINS), '/vault')
        got = [e[2] for e in w.events if e[0] == 'sql' and e[1] == 'CLOSE']
        self.assertEqual(got, [sql for sql, _ in R.provision_steps(binding, 'close', seed_ack=True)])
        self.assertGreater(w.events.index(('enter', 'CLOSE')), w.events.index(('ack_reload', OPKEY)))


class Denials(Base):
    def test_the_old_callback_signature_has_no_authority_and_does_no_io(self):
        w = self.world()
        outcome = self.run_sequence(old_style=True, migrate=lambda: None, seed=lambda: True)   # exactly the retired signature
        self.assertEqual(outcome[0], 'TypeError')
        self.assertEqual(w.events, [])

    def test_there_is_no_caller_supplied_ack_flag_on_any_public_entry(self):
        w = self.world()
        outcome = outcome_of(lambda: P.run_phase(w.config, custody=w.custody, deadline=w.deadline, phase='CLOSE', seed_ack=True))
        self.assertEqual(outcome[0], 'TypeError')
        self.assertEqual(w.events, [])

    def test_a_non_install_config_is_refused_before_the_first_guard_or_any_io(self):
        for mode in ('seed', 'serve', 'migrate', None):
            w = self.world()
            w.config = install_config(mode)
            outcome = self.run_sequence()
            self.assertEqual(outcome, ('RoleRefusal', 'provision_config'), mode)
            self.assertEqual(w.events, [], mode)

    def test_direct_run_phase_outside_an_owner_run_wrong_stage_and_reentry_deny_before_any_connector_or_password(self):
        w = self.world(active=False)
        for phase in ('PRE', 'CLOSE', 'pre'):
            with self.assertRaises(Exception) as cm:
                P.run_phase(w.config, custody=w.custody, deadline=w.deadline, phase=phase, operation_key=OPKEY)
            self.assertIsInstance(cm.exception, (Refusal, R.RoleRefusal), phase)
        self.assertEqual([e for e in w.events if e[0] in ('open', 'password', 'sql', 'migrate')], [])
        w = self.world()   # active owner: a stage out of order, then re-entry after completion, both deny before the connector
        with self.assertRaises(Exception):
            P.run_phase(w.config, custody=w.custody, deadline=w.deadline, phase='CLOSE', operation_key=OPKEY)
        self.assertEqual([e for e in w.events if e[0] in ('open', 'password', 'sql')], [])
        self.assertEqual(self.run_sequence()[0], 'ok')
        before = len(w.events)
        with self.assertRaises(Exception):
            P.run_phase(w.config, custody=w.custody, deadline=w.deadline, phase='PRE', operation_key=OPKEY)
        self.assertEqual(len(w.events), before)

    def test_a_foreign_config_custody_or_expired_deadline_is_denied_by_the_guard_before_any_io(self):
        for name, over in (('foreign custody', dict(custody=SimpleNamespace(resolve=None))), ('other deadline', dict(deadline=1.0))):
            w = self.world()
            outcome = self.run_sequence(**over)
            self.assertEqual(outcome[0], 'Refusal', name)
            self.assertEqual([e for e in w.events if e[0] in ('open', 'password', 'sql', 'migrate', 'seed_call')], [], name)

    def test_a_failure_in_any_stage_taints_the_run_rolls_back_and_nothing_later_starts(self):
        for stage, at in (('PRE', 1), ('PRE', 3), ('POST', 1), ('LEDGER', 1), ('MIGRATE', 1), ('CLOSE', 1)):
            w = self.world(fail_in=stage, fail_at=at)
            outcome = self.run_sequence()
            self.assertEqual(outcome[0], 'RuntimeError', (stage, at))
            self.assertEqual(self.phases()[-1], stage, (stage, at))
            self.assertNotIn(('exit', stage), w.events, (stage, at))
            later = ORDER[ORDER.index(stage) + 1:]
            self.assertTrue(set(later).isdisjoint(self.phases()), (stage, at))
            if stage != 'MIGRATE':
                self.assertIn(('tx_rollback',), w.events, (stage, at))
            if stage == 'CLOSE':
                self.assertIn(('ack_reload', OPKEY), w.events)

    def test_a_refused_ack_never_reaches_close(self):
        w = self.world()
        with mock.patch.object(OPERATOR_MOD, '_reload_seed_receipt', side_effect=Refusal('seed_receipt'), create=True):
            outcome = self.run_sequence()
        self.assertEqual(outcome, ('Refusal', 'seed_receipt'))
        self.assertNotIn('CLOSE', self.phases())
        self.assertFalse([e for e in w.events if e[0] == 'sql' and e[1] == 'CLOSE'])

    def test_a_ledger_flag_turned_off_is_a_loud_refusal_before_seed_ack_and_close(self):
        w = self.world()
        with mock.patch.object(P, 'LEDGER_ACCEPTED', False):
            outcome = self.run_sequence()
        self.assertEqual(outcome, ('RoleRefusal', 'ledger_not_accepted'))
        self.assertTrue({'SEED', 'ACK', 'CLOSE'}.isdisjoint(self.phases()))


CHILD = """
import importlib.util, json, sys
from types import SimpleNamespace
install_dir, tests_dir, src_dir, with_core, spec = sys.argv[1:6]
sys.path[:0] = [install_dir, tests_dir] + ([src_dir] if with_core == 'yes' else [])
absent = importlib.util.find_spec('cortex_core') is None
import provision as P
cfg = json.loads(spec)
config = SimpleNamespace(mode=cfg['mode'], role_logins=cfg['role_logins'], vault_root=cfg['vault_root'], database=cfg['database'], installation_id=cfg['installation_id'])
events = []
custody = SimpleNamespace(resolve=lambda purpose: events.append(purpose))
try:
    outcome = ['ok', repr(P.provision_sequence(config, custody=custody, deadline=9.0e9, operation_key=cfg['operation_key']))]
except Exception as error:
    outcome = [type(error).__name__, getattr(error, 'reason', str(error)[:50])]
print(json.dumps({'core_absent': absent, 'outcome': outcome, 'events': events}))
"""


class CoreMissing(unittest.TestCase):
    """REAL provision module, no doubles, in a CHILD interpreter whose import path genuinely has no Core package (nothing is deleted or faked in this process, where the composed Core is present): the
    call must be a typed non-success with no effect, never a stub or a fallback. A second control runs the SAME child with the Core package present and proves the first is not vacuous."""

    def run_child(self, with_core):
        import json
        import subprocess
        c = install_config()
        spec = json.dumps({'mode': c.mode, 'role_logins': c.role_logins, 'vault_root': c.vault_root, 'database': c.database, 'installation_id': c.installation_id, 'operation_key': OPKEY})
        done = subprocess.run([sys.executable, '-I', '-B', '-c', CHILD, str(HERE.parent), str(HERE), str(HERE.parents[3] / 'src'), 'yes' if with_core else 'no', spec],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-300:])
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_without_the_core2_private_join_the_call_refuses_typed_and_touches_nothing(self):
        result = self.run_child(with_core=False)
        self.assertTrue(result['core_absent'], 'this control is only meaningful while the Core package is genuinely absent from the interpreter under test')
        outcome = tuple(result['outcome'])
        self.assertEqual(outcome, ('RoleRefusal', 'provision_core_missing'))
        self.assertEqual(result['events'], [])

    def test_the_same_child_with_the_core_package_present_does_not_report_a_missing_core(self):
        result = self.run_child(with_core=True)
        self.assertFalse(result['core_absent'])
        self.assertNotEqual(tuple(result['outcome']), ('RoleRefusal', 'provision_core_missing'))
        self.assertEqual(result['events'], [])


if __name__ == '__main__':
    unittest.main()
