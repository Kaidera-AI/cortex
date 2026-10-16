"""X1-b final cutoff regressions; fixture originals and test-owned files only.

Vera X1AB-001 and Hale X1AB-ACK-001: a valid observation which returns late
cannot authorize dispatch or acknowledge durable result/child evidence.
"""
from contextlib import ExitStack
import tempfile
import time
import unittest
from unittest.mock import patch

from cortex_core.install import lifecycle, owner
from cortex_core.release.contracts import thaw
from test_hold1_interface import Subject
import test_owner_deadline_retention as deadline_tests
from test_recovery_uninstall import AppliedSubject


class FinalAckDeadline(unittest.TestCase):
    def primitive(self, subject):
        return deadline_tests.X1NonSQLDeadline().primitive(subject)

    def execute(self, subject):
        return lifecycle.execute_effect(subject.data['request_raw'].encode())

    def expire(self, history, mode, clock):
        # Exhaust the retained stop; never manufacture a replacement deadline.
        stop = history._publication.current[2]
        if mode == 'simulated':
            clock[0] = stop
        else:
            before = time.monotonic_ns()
            time.sleep(max(0, (stop-before)/1_000_000_000) + .025)
        self.assertGreaterEqual(lifecycle.time.monotonic_ns(), stop)

    def test_delayed_dispatch_observation_refuses_before_effect(self):
        with tempfile.TemporaryDirectory() as root:
            s = Subject(root)
            current = lifecycle._current_owner
            clock, fired = [0], []

            def delayed(*args, **kwargs):
                result = current(*args, **kwargs)
                if len(s.history.events()) == 2 and not s.barrier and not fired:
                    fired.append(True)
                    self.expire(s.history, 'simulated', clock)
                return result

            with s.installed(self.primitive(s)), \
                    patch.object(lifecycle.time, 'monotonic_ns', lambda: clock[0]), \
                    patch.object(lifecycle, '_current_owner', delayed):
                with self.assertRaises(owner.OwnerRefusal) as caught:
                    self.execute(s)
            self.assertEqual(caught.exception.detail, 'reservation_expired')
            self.assertEqual(fired, [True])
            self.assertEqual(s.effects, 0, 'dispatch after the original cutoff')
            self.assertEqual(len(s.history.events()), 2)
            self.assertIsNone(s.history.head()[1]['result_ref'])

    def test_final_owner_wait_refuses_ack_and_keeps_one_durable_result(self):
        for mode in ('simulated', 'real'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                s = Subject(root)
                current = lifecycle._current_owner
                clock, fired, seen = [0], [], []

                def delayed(*args, **kwargs):
                    result = current(*args, **kwargs)
                    if len(s.history.events()) == 3 and not fired:
                        fired.append(True)
                        self.expire(s.history, mode, clock)
                    return result

                with ExitStack() as stack:
                    stack.enter_context(s.installed(self.primitive(s)))
                    stack.enter_context(patch.object(lifecycle, '_current_owner', delayed))
                    if mode == 'simulated':
                        stack.enter_context(patch.object(lifecycle.time, 'monotonic_ns', lambda: clock[0]))
                    try:
                        seen.append(('returned', self.execute(s)))
                    except owner.OwnerRefusal as error:
                        seen.append(('refused', error.detail))
                self.assertEqual(fired, [True])
                self.assertEqual(s.effects, 1)
                self.assertEqual(len(s.history.events()), 3)
                head = s.history.head()[0]
                self.assertIsNotNone(s.history.head()[1]['result_ref'])
                self.assertEqual(lifecycle._History(s.root).head()[0], head)
                self.assertEqual(seen, [('refused', 'reservation_expired')],
                                 'ACK after the original cutoff')
                with s.installed(self.primitive(s)):
                    with self.assertRaises(owner.OwnerRefusal):
                        self.execute(s)
                self.assertEqual(s.effects, 1, 'late ACK refusal replayed the effect')
                self.assertEqual(s.history.head()[0], head)
                self.assertEqual(len(s.history.events()), 3)

    def test_child_history_wait_refuses_ack_retains_exact_child(self):
        for mode in ('simulated', 'real'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                s = AppliedSubject(root, route='kos_prerequisite', retain_receipt=False)
                retain, revision = s.history.retain, s.history.current_revision
                clock, persisted, fired, seen = [0], [], [], []
                head = s.history.head()[0]

                def retained(raw):
                    reference = retain(raw)
                    if raw == s.child_raw:
                        persisted.append(reference)
                    return reference

                def delayed_read():
                    result = revision()
                    if persisted and not fired:
                        fired.append(True)
                        self.expire(s.history, mode, clock)
                    return result

                with ExitStack() as stack:
                    stack.enter_context(patch.object(s.history, 'retain', retained))
                    stack.enter_context(patch.object(s.history, 'current_revision', delayed_read))
                    if mode == 'simulated':
                        stack.enter_context(patch.object(lifecycle.time, 'monotonic_ns', lambda: clock[0]))
                    try:
                        seen.append(('returned', s.parent()))
                    except owner.OwnerRefusal as error:
                        seen.append(('refused', error.detail))
                self.assertEqual(fired, [True])
                self.assertEqual(len(persisted), 1)
                self.assertEqual(s.history.read(persisted[0]), s.child_raw)
                self.assertEqual(s.history.head()[0], head)
                self.assertEqual(s.dispatched, 0)
                self.assertEqual(seen, [('refused', 'reservation_expired')],
                                 'child ACK after the original cutoff')

    def test_short_final_owner_wait_can_ack_original_result(self):
        with tempfile.TemporaryDirectory() as root:
            s = Subject(root)
            current, fired = lifecycle._current_owner, []

            def delayed(*args, **kwargs):
                result = current(*args, **kwargs)
                if len(s.history.events()) == 3 and not fired:
                    fired.append(True)
                    time.sleep(.001)
                return result

            with s.installed(self.primitive(s)), patch.object(lifecycle, '_current_owner', delayed):
                reference = self.execute(s)
            self.assertEqual(fired, [True])
            self.assertEqual(s.effects, 1)
            self.assertEqual(thaw(reference), s.history.head()[0])
            self.assertEqual(len(s.history.events()), 3)

    def test_short_child_history_wait_can_ack_exact_child(self):
        with tempfile.TemporaryDirectory() as root:
            s = AppliedSubject(root, route='kos_prerequisite', retain_receipt=False)
            retain, revision = s.history.retain, s.history.current_revision
            persisted, fired = [], []

            def retained(raw):
                reference = retain(raw)
                if raw == s.child_raw:
                    persisted.append(reference)
                return reference

            def delayed_read():
                result = revision()
                if persisted and not fired:
                    fired.append(True)
                    time.sleep(.001)
                return result

            with patch.object(s.history, 'retain', retained), \
                    patch.object(s.history, 'current_revision', delayed_read):
                reference = s.parent()
            self.assertEqual(fired, [True])
            self.assertEqual(thaw(reference), persisted[0])
            self.assertEqual(s.history.read(reference), s.child_raw)
            self.assertEqual(s.dispatched, 0)

    def test_failed_operation_preserves_refusal_and_restores_scope(self):
        with tempfile.TemporaryDirectory() as root:
            s = Subject(root)
            clock = [0]
            with s.installed(), patch.object(lifecycle.time, 'monotonic_ns', lambda: clock[0]):
                with self.assertRaises(owner.OwnerRefusal) as caught:
                    with s.history.operation(deadline=s.deadline, expected_context=thaw(s.proposal.context)):
                        clock[0] = s.history._publication.current[2]
                        raise owner.OwnerRefusal('consent_revoked')
            self.assertEqual(caught.exception.detail, 'consent_revoked')
            self.assertIsNone(s.history._publication.current)
            self.assertIsNone(s.history.head()[0])
