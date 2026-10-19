"""S1 make_secret, J1 effect gap: SUPPLEMENTARY component tests added WITH the implementation (the 55 tests of test_make_secret.py are unchanged and are NOT edited here).
The closed S1 doubles let Core's modelled `_current_installer` refuse closed, tainted, busy, non-IMPORT and expired owners, so S1's OWN reading of the owner slots was never the only thing
standing in the way. LenientCore is a NAMED DOUBLE that returns the bound owner whatever the slots say (a Core that did not check, or a mis-modelled one): every predicate below must
still be refused by make_secret itself, with the owner invalidated through the fail path. COMPONENT-ONLY over doubles; the actual Core and S4 join is NOT RUN."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]
import test_make_secret as T  # noqa: E402
from test_make_secret import CoreDouble, Harness, OwnerDouble, ProducerDouble, ReceiverDouble  # noqa: E402


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


if __name__ == '__main__':
    unittest.main()
