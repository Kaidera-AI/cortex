"""S1 make_secret, J1 effect gap: SUPPLEMENTARY component tests (first half) and the ACTUAL-CORE JOIN tests (classes Join and Resolvers, second half; they live here because this file is already registered in .github/next-offline-suites.json) added WITH the implementation (the 55 tests of test_make_secret.py are unchanged and are NOT edited here).
The closed S1 doubles let Core's modelled `_current_installer` refuse closed, tainted, busy, non-IMPORT and expired owners, so S1's OWN reading of the owner slots was never the only thing
standing in the way. LenientCore is a NAMED DOUBLE that returns the bound owner whatever the slots say (a Core that did not check, or a mis-modelled one): every predicate below must
still be refused by make_secret itself, with the owner invalidated through the fail path. COMPONENT-ONLY over doubles for the first half. THE JOIN HALF runs make_secret with its REAL resolvers against Core's ACTUAL InstallerCustody, `_current_installer` and `_installer_fail` (owner opened by Core's own open_installer_custody); NAMED NONSECRET adapter doubles (Core's own SourceAdapters) stand in only BELOW the owner, the fixed S4 receiver is a NAMED DOUBLE (render.py has no receive_bootstrap in this tree), the producer is the J06 double. NOT RUN: real S4, real AESGCM/unlock descriptor/key file, PostgreSQL, the signed caller, native isolation."""
import contextlib
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
INSTALL = HERE.parent
SRC = INSTALL.parents[2] / 'src'
sys.path[:0] = [str(HERE), str(INSTALL), str(SRC)]
import make_secret as M  # noqa: E402
import test_make_secret as T  # noqa: E402
from test_make_secret import CoreDouble, Harness, OwnerDouble, ProducerDouble, ReceiverDouble  # noqa: E402
from cortex_core.runtime import custody  # noqa: E402
from cortex_core.runtime.bootstrap_config import decode_config  # noqa: E402
from cortex_core.runtime.tests.test_bootstrap_config import sample  # noqa: E402
from cortex_core.runtime.tests.test_installer_custody import SourceAdapters  # noqa: E402


class LenientCore(CoreDouble):
    """Returns the bound owner without testing any slot, the deadline or the clock."""

    def _current_installer(self, config, custody, deadline):
        return self.binding[0]


class Own(Harness, unittest.TestCase):
    def lenient(self, **kw):
        return self.call(core=LenientCore(None), **kw)

    def refused_and_invalidated(self, outcome, reason):
        with self.subTest('reason'):
            self.assertEqual(outcome, ('refused', reason))
        with self.subTest('invalidated through the fail path'):
            self.assertTrue(self.core.failed and all(o is self.owner for o in self.core.failed))

    def test_make_secret_itself_refuses_each_owner_slot_that_core_did_not_check(self):
        for name, slots in (('closed', {'_closed': True}), ('tainted', {'_tainted': True}), ('busy', {'_busy': True}), ('run stage', {'_stage': 'RUN'}),
                            ('error stage', {'_stage': 'ERROR'}), ('already imported', {'_imported': frozenset({'bootstrap'})})):
            with self.subTest(name):
                outcome = self.lenient(prepare=lambda s, slots=slots: s.owner.poke(**slots))
                self.refused_and_invalidated(outcome, 'custody_context')
                self.assertEqual((self.producer.asked, self.owner.calls, self.receiver.got), ([], [], []))

    def test_make_secret_itself_refuses_each_slot_a_callback_changes_when_core_does_not_notice(self):
        hooks = {'closed by the producer': ('producer', {'_closed': True}), 'tainted by the producer': ('producer', {'_tainted': True}),
                 'stage by the producer': ('producer', {'_stage': 'RUN'}), 'busy by the producer': ('producer', {'_busy': True})}
        for name, (_, slots) in hooks.items():
            with self.subTest(name):
                producer = ProducerDouble(hooks={'probe': lambda s=None, slots=slots: self.owner.poke(**slots)})
                outcome = self.lenient(producer=producer)
                self.refused_and_invalidated(outcome, 'custody_context')
                self.assertEqual(self.owner.calls, [])
        with self.subTest('closed by an import'):
            outcome = self.lenient(prepare=lambda s: s.owner.hooks.update(migrator=lambda: s.owner.poke(_closed=True)))
            self.refused_and_invalidated(outcome, 'custody_context')
            self.assertEqual([c[0] for c in self.owner.calls], ['bootstrap', 'migrator'])

    def test_make_secret_itself_refuses_a_deadline_that_passed_when_core_does_not_notice(self):
        outcome = self.lenient(producer=ProducerDouble(hooks={'probe': lambda: self.clock.advance(120.0)}))
        self.refused_and_invalidated(outcome, 'deadline_expired')
        self.assertEqual(self.owner.calls, [])
        outcome = self.lenient(receiver=ReceiverDouble(hook=lambda: self.clock.advance(120.0)))
        self.refused_and_invalidated(outcome, 'deadline_expired')
        self.assertEqual(len(self.owner.calls), 5)

    def test_an_owner_whose_deadline_object_changes_during_the_call_is_refused_as_not_original(self):
        def swap():
            self.owner.poke(_deadline=self.owner._deadline + 0.0)
        outcome = self.lenient(producer=ProducerDouble(hooks={'migrator': swap}))
        self.refused_and_invalidated(outcome, 'deadline_not_original')
        self.assertEqual(self.owner.calls, [])

    def test_an_expired_argument_is_refused_before_the_owner_is_read_so_it_is_not_invalidated(self):
        past = T.time.monotonic() - 1.0
        for core in ('modelled', 'lenient'):
            with self.subTest(core):
                outcome = self.call(deadline=past, **({} if core == 'modelled' else {'core': LenientCore(None)}))
                self.assertEqual(outcome, ('refused', 'deadline_expired'))
                self.assertEqual((self.core.failed, self.producer.asked, self.owner.calls), ([], [], []))


FIVE = T.FIVE
DEADLINE = 200.0
_DEFAULT = object()


class Join(unittest.TestCase):
    def setUp(self):
        self.adapters = SourceAdapters()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(self.adapters.installed())
        self.stack.enter_context(mock.patch.object(time, 'monotonic', lambda: self.adapters.clock))
        self.config = decode_config(json.dumps(sample('/source-only/NONSECRET-vault')).encode(), mode='install')
        self.receiver = T.ReceiverDouble()
        self.stack.enter_context(mock.patch.object(M, '_receiver', return_value=self.receiver))
        self.owner = self.stack.enter_context(custody.open_installer_custody(self.config, unlock_fd=99, key_id='NONSECRET-id', deadline=DEADLINE))

    def call(self, producer=None, owner=_DEFAULT, deadline=DEADLINE):
        try:
            return ('ok', M.make_secret(self.owner if owner is _DEFAULT else owner, producer=T.ProducerDouble() if producer is None else producer, deadline=deadline))
        except M.MakeSecretRefusal as refusal:
            return ('refused', refusal.reason)

    def sealed(self):
        return len(self.adapters.records)

    def test_the_real_resolvers_bind_the_actual_core_owner_type_and_private_functions(self):
        self.assertIs(M._owner_type(), custody.InstallerCustody)
        self.assertIs(M._core(), custody)
        self.assertTrue(callable(custody._current_installer) and callable(custody._installer_fail))

    def test_success_seals_five_distinct_roles_through_the_real_owner_and_leaves_it_admitted_unrun(self):
        outcome = self.call()
        with self.subTest('result'):
            self.assertEqual(outcome, ('ok', {'imported': list(FIVE), 'bootstrap_delivered': True}))
        with self.subTest('five records sealed by the real vault'):
            self.assertEqual((self.sealed(), self.adapters.events.count('write-double')), (5, 5))
        with self.subTest('the owner holds exactly the five, still at IMPORT, untainted, unclosed, not busy'):
            self.assertEqual((self.owner._imported, self.owner._stage, self.owner._tainted, self.owner._closed, self.owner._busy),
                             (custody.ROLE_NAMES, 'IMPORT', False, False, False))
        with self.subTest('the fixed receiver got the owner and the bootstrap bytes once'):
            self.assertEqual((self.receiver.owners, self.receiver.got), ([self.owner], [T.SYNTH['bootstrap']]))
        with self.subTest('S1 never ran the sequence and the actual admission still holds'):
            self.assertIs(custody._current_installer(self.config, self.owner._vault, self.owner._deadline), self.owner)
            self.assertIsNone(self.owner._operation_key)

    def test_a_failing_receiver_taints_the_real_owner_through_the_real_fail_path_and_blocks_a_second_attempt(self):
        self.receiver.fail = True
        first = self.call()
        with self.subTest('first attempt'):
            self.assertEqual(first, ('refused', 'bootstrap_receiver_failed'))
        with self.subTest('actual owner invalidated by Core'):
            self.assertEqual((self.owner._tainted, self.owner._stage), (True, 'ERROR'))
        asked = T.ProducerDouble()
        second = self.call(producer=asked)
        with self.subTest('second attempt refused, producer never asked, nothing more sealed'):
            self.assertEqual((second, asked.asked, self.sealed()), (('refused', 'custody_context'), [], 5))
        with self.subTest('the real run entry also refuses'):
            with self.assertRaises(Exception) as caught:
                self.owner.run('op-1')
            self.assertEqual(getattr(caught.exception, 'reason', None), 'installer_context')

    def test_a_producer_that_taints_the_real_owner_stops_the_sequence_with_nothing_sealed(self):
        hook = lambda: custody._installer_fail(self.owner)  # noqa: E731 - the real Core fail path, called by the producer
        outcome = self.call(producer=T.ProducerDouble(hooks={'probe': hook}))
        self.assertEqual((outcome, self.sealed(), self.owner._stage), (('refused', 'custody_context'), 0, 'ERROR'))

    def test_a_deadline_passed_during_production_is_refused_with_nothing_sealed(self):
        outcome = self.call(producer=T.ProducerDouble(hooks={'probe': lambda: setattr(self.adapters, 'clock', DEADLINE + 5.0)}))
        self.assertEqual((outcome, self.sealed(), self.owner._tainted), (('refused', 'deadline_expired'), 0, True))

    def test_a_deadline_passed_after_the_third_real_import_stops_before_the_next(self):
        original = custody.InstallerCustody.import_credential
        count = []

        def slow(owner, purpose, credential):
            original(owner, purpose, credential)
            count.append(purpose)
            if len(count) == 3:
                self.adapters.clock = DEADLINE + 5.0
        with mock.patch.object(custody.InstallerCustody, 'import_credential', slow):
            outcome = self.call()
        self.assertEqual((outcome, count, self.sealed(), self.receiver.got), (('refused', 'deadline_expired'), list(FIVE[:3]), 3, []))

    def test_a_reentrant_call_from_the_producer_is_refused_and_invalidates_the_real_owner(self):
        inner = []
        hook = lambda: inner.append(self.call(owner=self.owner))  # noqa: E731
        outcome = self.call(producer=T.ProducerDouble(hooks={'migrator': hook}))
        self.assertEqual((inner, outcome, self.sealed(), self.owner._stage), ([('refused', 'make_secret_reentered')], ('refused', 'custody_context'), 0, 'ERROR'))

    def test_a_caller_deadline_that_is_not_the_real_owners_original_is_refused_and_invalidates_it(self):
        asked = T.ProducerDouble()
        outcome = self.call(producer=asked, deadline=DEADLINE + 30.0)
        self.assertEqual((outcome, asked.asked, self.sealed(), self.owner._stage), (('refused', 'deadline_not_original'), [], 0, 'ERROR'))

    def test_anything_but_the_actual_owner_type_is_refused_before_the_producer(self):
        asked = T.ProducerDouble()
        for name, other in (('plain object', object()), ('none', None), ('dict', {})):
            with self.subTest(name):
                self.assertEqual(self.call(producer=asked, owner=other), ('refused', 'custody_owner_type'))
        self.assertEqual((asked.asked, self.owner._tainted, self.sealed()), ([], False, 0))

    def test_a_credential_refused_by_the_real_vault_is_a_typed_import_failure_with_no_value_in_the_text(self):
        values = dict(T.SYNTH, migrator=b'x' * 5000)  # over Core's 4096-byte limit: Core refuses purpose_not_admitted
        outcome = self.call(producer=T.ProducerDouble(values))
        self.assertEqual(outcome, ('refused', 'import_failed:migrator'))
        self.assertEqual((self.owner._tainted, self.sealed()), (True, 1))


class Resolvers(unittest.TestCase):
    """The three resolvers in two real interpreters: one whose import path has NO Core package (they must be unbound) and one with this tree's Core on the path."""

    def run_child(self, with_core):
        import subprocess
        code = ("import sys\nsys.path[:0]=[sys.argv[1]]+([sys.argv[2]] if sys.argv[3]=='yes' else [])\nimport make_secret as M\n"
                "o,c,r=M._owner_type(),M._core(),M._receiver()\n"
                "print(repr((None if o is None else o.__name__, None if c is None else c.__name__, None if r is None else r.__name__)))\n")
        done = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(INSTALL), str(SRC), 'yes' if with_core else 'no'], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-300:])
        return done.stdout.strip()

    def test_without_a_core_package_every_resolver_is_unbound_so_no_authority_exists_by_import(self):
        self.assertEqual(self.run_child(False), "(None, None, None)")

    def test_with_the_core_package_the_owner_type_and_private_functions_bind_and_the_receiver_follows_s4(self):
        owner, core, receiver = eval(self.run_child(True))  # noqa: S307 - a repr of three names written by the child above
        self.assertEqual((owner, core), ('InstallerCustody', 'cortex_core.runtime.custody'))
        self.assertIn(receiver, (None, 'receive_bootstrap'))

    def test_a_receiver_package_that_defines_receive_bootstrap_binds_and_one_that_does_not_stays_unbound(self):
        """A synthetic cortex_ship.run.render in a private temp directory (the S4 module is not in this tree): the resolver returns exactly its receive_bootstrap, and None when the function is absent."""
        import subprocess
        import tempfile
        for defined in (True, False):
            with self.subTest(defined=defined):
                root = Path(tempfile.mkdtemp(prefix='s4-fixture-'))
                self.addCleanup(__import__('shutil').rmtree, root, True)
                for package in ('cortex_ship', 'cortex_ship/run'):
                    (root / package).mkdir(parents=True, exist_ok=True)
                    (root / package / '__init__.py').write_text('')
                (root / 'cortex_ship/run/render.py').write_text('def receive_bootstrap(owner, bootstrap):\n    return None\n' if defined else 'def other(owner, bootstrap):\n    return None\n')
                code = ("import sys\nsys.path[:0]=[sys.argv[1], sys.argv[2]]\nimport make_secret as M\nr=M._receiver()\nprint(repr(None if r is None else (r.__name__, r.__module__)))\n")
                done = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(INSTALL), str(root)], capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr[-300:])
                self.assertEqual(done.stdout.strip(), repr(('receive_bootstrap', 'cortex_ship.run.render') if defined else None))

    def test_a_core_custody_module_lacking_either_private_admission_function_stays_unbound(self):
        """A synthetic cortex_core.runtime.custody (importable, with an InstallerCustody class) that defines only ONE of `_current_installer` / `_installer_fail`: `_core` must stay None."""
        import subprocess
        import tempfile
        for present in ('_current_installer', '_installer_fail'):
            with self.subTest(only=present):
                root = Path(tempfile.mkdtemp(prefix='core-fixture-'))
                self.addCleanup(__import__('shutil').rmtree, root, True)
                for package in ('cortex_core', 'cortex_core/runtime'):
                    (root / package).mkdir(parents=True, exist_ok=True)
                    (root / package / '__init__.py').write_text('')
                (root / 'cortex_core/runtime/custody.py').write_text('class InstallerCustody:\n    pass\n\ndef %s(*a, **k):\n    return None\n' % present)
                code = "import sys\nsys.path[:0]=[sys.argv[1], sys.argv[2]]\nimport make_secret as M\nprint(repr((M._owner_type() is not None, M._core())))\n"
                done = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(INSTALL), str(root)], capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr[-300:])
                self.assertEqual(done.stdout.strip(), repr((True, None)))



class S4Join(unittest.TestCase):
    """J1 ACTUAL CORE + ACTUAL S4 JOIN (refusal side): make_secret with its REAL resolvers inside Bob's REAL finite_bootstrap (render.py, composed head), over Core's real owner and vault. The fixed
    receiver is resolved by make_secret._receiver() through the packaged name cortex_ship.run.render, aliased to the SAME loaded render module so its context variable is shared (the one test-layout
    step, as in Finn's committed positive join). NOT substituted: _owner_type, _core, _receiver, the owner, S4's receive_bootstrap and finite_bootstrap. NAMED NONSECRET doubles below the production
    boundary only (S4's own _bootstrap_stage: adapters, SQL rows, engine, clock) and the J06 producer. Needs the CI test dependencies (pytest) because it reuses Bob's harness modules.
    NOT RUN: real AESGCM/unlock descriptor, PostgreSQL, Podman, native isolation."""

    @classmethod
    def setUpClass(cls):
        run_tests = str(INSTALL.parent / 'run' / 'tests')
        if run_tests not in sys.path:
            sys.path.insert(0, run_tests)
        import _bootstrap_stage as stage
        import test_lifecycle_deciding as lifecycle
        import test_lifecycle_record_deciding as recording
        import test_render as render_tests
        cls.S, cls.lifecycle, cls.recording, cls.TR = stage, lifecycle, recording, render_tests

    def inputs(self):
        import os
        import tempfile
        TR = self.TR
        root = Path(os.path.realpath(tempfile.mkdtemp(prefix='s4join-')))
        self.addCleanup(__import__('shutil').rmtree, root, True)
        conf = root / 'core-conf'
        (conf / 'sql').mkdir(parents=True)
        for name in ('postgresql.conf', 'pg_hba.conf'):
            (conf / name).write_text('# fixture\n')
        for step in ('pre', 'post', 'close'):
            (conf / 'sql' / f'roles-{step}.sql').write_text('-- fixture\n')
        vault = root / 'vault'
        (vault / 'config').mkdir(parents=True)
        paths = {'config': vault / 'config' / 'cortex.json', 'release': root / 'release.json', 'operations': root / 'ops.json', 'core_conf': conf,
                 'out': root / 'out', 'vault': vault, 'root': root}
        TR.write_config(paths, TR.base_config(str(vault)))
        paths['release'].write_text(json.dumps(TR.base_release()))
        paths['operations'].write_bytes(TR.OPERATIONS.read_bytes())
        return paths

    def world(self, run_exit=0):
        S = self.S
        self.render, self.rec = self.lifecycle.load_render(), self.recording.load_record()
        paths = self.inputs()
        self.TR.write_config(paths, S.install_config(str(paths['vault'])))
        _serve, self.plan, _inventory = self.TR.rendered(paths)
        self.paths = paths
        self.config = decode_config(paths['config'].read_bytes(), mode='install')
        prefix = paths['root'] / 'prefix'
        prefix.mkdir(mode=0o700)
        self.prefix = prefix
        self.record = self.render.InstallRecord(prefix, self.recording.RELEASE)
        self.events = []
        self.engine = S.BootstrapEngine(self.events, run_exit=run_exit)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.doubles = stack.enter_context(S.composed(self.render, self.config))
        stack.enter_context(mock.patch.dict(sys.modules, {'cortex_ship.run.render': self.render}))
        stack.enter_context(mock.patch.object(time, 'monotonic', lambda: self.doubles.adapters.clock))
        self.asked = []

    def producer(self, hooks=None):
        S, asked, hooks = self.S, self.asked, hooks or {}

        class Producer:
            def bytes_for(self, purpose):
                asked.append(purpose)
                if purpose in hooks:
                    hooks[purpose]()
                return S.password(purpose)
        return Producer()

    def block(self):
        return self.render.finite_bootstrap(self.paths['config'], plan=self.plan, unlock_fd=self.S.UNLOCK, key_id=self.S.KEY_ID, operation_key=self.S.OPERATION,
                                           record=self.record, engine=self.engine, deadline=self.S.DEADLINE)

    def effects(self):
        return [e for e in self.events if e[0] in ('run', 'create', 'start', 'remove')]

    def completion_recorded(self):
        doc = self.rec.read_record(str(self.prefix))
        return bool(doc and any(o.get('kind') == 'bootstrap' for o in doc.get('objects', [])))

    def attempt(self, producer, *, swallow=True):
        """Run make_secret inside the real block. Returns (make_secret outcome, the Refusal S4 raised at block exit or None)."""
        outcome, exit_refusal = [], None
        try:
            with self.block() as owner:
                self.owner = owner
                self.assertIs(M._receiver(), self.render.receive_bootstrap, 'the resolver must bind the actual S4 receiver')
                self.assertIs(M._owner_type(), custody.InstallerCustody)
                try:
                    outcome.append(('ok', M.make_secret(owner, producer=producer, deadline=self.S.DEADLINE)))
                except M.MakeSecretRefusal as refusal:
                    outcome.append(('refused', refusal.reason))
                    if not swallow:
                        raise
        except custody.Refusal as refusal:
            exit_refusal = refusal
        return outcome[0] if outcome else None, exit_refusal

    def assert_unwound(self, *, delivered):
        with self.subTest('owner tainted through the real fail path and never run'):
            self.assertTrue(self.owner._tainted)
            self.assertFalse([e for e in self.events if e[:3] == ('call', 'run', 'custody.py')])
        with self.subTest('no completion object, nothing left in the engine'):
            self.assertFalse(self.completion_recorded())
            self.assertEqual(self.engine.objects, {})
        if not delivered:
            with self.subTest('pre-effect: the engine was never touched and core-init got no bytes'):
                self.assertEqual((self.effects(), self.engine.inputs), ([], []))

    def test_a_failing_prelude_inside_the_real_receiver_is_a_typed_failure_and_the_real_block_unwinds(self):
        self.world(run_exit=1)
        outcome, exit_refusal = self.attempt(self.producer())
        with self.subTest('make_secret outcome'):
            self.assertEqual(outcome, ('refused', 'bootstrap_receiver_failed'))
        with self.subTest('S4 block refuses at exit because the owner is tainted'):
            self.assertIsNotNone(exit_refusal)
        with self.subTest('the five imports happened, the receiver ran once'):
            self.assertEqual(self.asked, list(self.S.PURPOSES))
            self.assertEqual(len(self.engine.inputs), 1)
        self.assert_unwound(delivered=True)

    def test_a_deadline_passed_during_production_refuses_before_any_import_or_engine_effect(self):
        self.world()
        outcome, exit_refusal = self.attempt(self.producer({'probe': lambda: setattr(self.doubles.adapters, 'clock', self.S.DEADLINE + 5.0)}))
        with self.subTest('make_secret outcome'):
            self.assertEqual(outcome, ('refused', 'deadline_expired'))
        with self.subTest('S4 block refuses at exit'):
            self.assertIsNotNone(exit_refusal)
        self.assert_unwound(delivered=False)

    def test_a_producer_that_taints_the_real_owner_stops_before_any_import_or_engine_effect(self):
        self.world()
        outcome, exit_refusal = self.attempt(self.producer({'migrator': lambda: custody._installer_fail(self.owner)}))
        with self.subTest('make_secret outcome'):
            self.assertEqual(outcome, ('refused', 'custody_context'))
        with self.subTest('S4 block refuses at exit'):
            self.assertIsNotNone(exit_refusal)
        self.assert_unwound(delivered=False)

    def test_a_reentrant_call_from_the_producer_is_refused_and_nothing_reaches_the_engine(self):
        self.world()
        inner = []

        def reenter():
            try:
                inner.append(M.make_secret(self.owner, producer=self.producer(), deadline=self.S.DEADLINE))
            except M.MakeSecretRefusal as refusal:
                inner.append(('refused', refusal.reason))
        outcome, exit_refusal = self.attempt(self.producer({'migrator': reenter}))
        with self.subTest('the reentered call'):
            self.assertEqual(inner, [('refused', 'make_secret_reentered')])
        with self.subTest('make_secret outcome'):
            self.assertEqual(outcome, ('refused', 'custody_context'))
        self.assert_unwound(delivered=False)

    def test_a_second_call_after_the_bootstrap_was_delivered_is_refused_without_a_second_delivery_and_taints_the_block(self):
        self.world()
        second = []

        def again():
            try:
                M.make_secret(self.owner, producer=self.producer(), deadline=self.S.DEADLINE)
            except M.MakeSecretRefusal as refusal:
                second.append(refusal.reason)
        first_asked, first = self.asked, []
        with self.assertRaises(custody.Refusal):
            with self.block() as owner:
                self.owner = owner
                try:
                    first.append(('ok', M.make_secret(owner, producer=self.producer(), deadline=self.S.DEADLINE)['bootstrap_delivered']))
                except M.MakeSecretRefusal as refusal:
                    first.append(('refused', refusal.reason))
                again()
        with self.subTest('first call delivered'):
            self.assertEqual(first, [('ok', True)])
        with self.subTest('second attempt refused by make_secret itself'):
            self.assertEqual(second, ['custody_context'])
        with self.subTest('exactly one delivery to core-init'):
            self.assertEqual(len(self.engine.inputs), 1)
        with self.subTest('no completion object after the tainted block'):
            self.assertFalse(self.completion_recorded())
        self.assertEqual(self.asked, first_asked)


if __name__ == '__main__':
    unittest.main()
