"""S1 make_secret (day 3): INTENTIONAL RED. Deciding behaviour of the future make_secret against the published interface (make_secret.py, make_secret_interface.md).
Labels (each test says which): COMPONENT / LIFETIME = make_secret called directly with NAMED DOUBLES (OwnerDouble, CoreDouble, ProducerDouble, ReceiverDouble, ClockDouble) and replaced resolvers
(`_owner_type`, `_core`, `_receiver`); it earns NO supported-caller credit. FIXTURE = a labeled spy replaces make_secret: it proves what the FIXTURE hands the callee, never S1. CALLER = the actual
install.sh on an UNSIGNED release folder (no key exists or is created) - expected to PASS today and to keep passing. STATIC / INTERFACE = source and signature guards, expected to pass.
All values are synthetic non-secrets (`SYNTHETIC-NONSECRET-...`); no key, signature, unlock, custody file, database or Podman effect exists here, and test_sign.py is not imported.
Every LIFETIME predicate is its own subTest, so a failing reason is never allowed to mask the producer, import, delivery and invalidation predicates after it.
NOT RUN (named, not coded): the actual Core InstallerCustody / _current_installer / _installer_fail and the actual S4 render.receive_bootstrap (reference only: selected Core 90daf7ea custody.py blob
3c9713101fb24451896eedadaabcf7c49ad85f23, S4 head dd9c2f80 render.py blob 7e94cc47da7ae3c1cd780d96b2fa9e200e14037f; neither is in this tree), the signed caller, a real J06 producer, the unlock
descriptor and the initdb transfer. The doubles model the conditions the contract names; they are not the Core and cannot earn the actual join."""
import contextlib
import inspect
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
INSTALL = HERE.parent
sys.path[:0] = [str(INSTALL)]
import make_secret as M  # noqa: E402

FIVE = ('bootstrap', 'migrator', 'api_request', 'probe', 'installer_enrollment')
SYNTH = {p: f'SYNTHETIC-NONSECRET-{p}-0123456789abcdef'.encode() for p in FIVE}
STUB = '#!/bin/sh\necho "$*" >> "$STUB_LOG"\ncase "$1" in --version) echo "podman version 7.0.3" ;; *) : ;; esac\n'


class RefusalDouble(Exception):
    """NAMED DOUBLE of cortex_core.runtime.signed_member.Refusal (the type Core's private admission raises)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class ClockDouble:
    """The one controllable monotonic clock: real time plus an offset a scenario advances. Installed over time.monotonic for the duration of a call."""

    def __init__(self):
        self.real, self.offset = time.monotonic, 0.0

    def __call__(self):
        return self.real() + self.offset

    def advance(self, seconds):
        self.offset += seconds


class OwnerDouble:
    """NAMED DOUBLE of Core's InstallerCustody (selected Core 90daf7ea, custody.py blob 3c971310). It carries the private slots the contract reads (_config _vault _deadline _stage _imported _closed
    _tainted _busy) and, like the real class, REFUSES assignment to any private slot (S1 must never write one); only `poke` (the scenario or the CoreDouble) writes. `import_credential` records the
    attempt and applies Core's own admission (a closed, tainted, busy or non-IMPORT owner refuses); a failing import taints it, as Core does. `run` exists only to prove S1 never calls it."""

    def __init__(self, fail_on=None, leak=None, deadline=None):
        self.poke(calls=[], runs=[], hooks={}, fail_on=fail_on, leak=leak, _config=object(), _vault=object(), _deadline=deadline, _stage='IMPORT',
                  _imported=frozenset(), _closed=False, _tainted=False, _busy=False)

    def __setattr__(self, name, value):
        if name.startswith('_'):
            raise AttributeError('private installer custody')
        object.__setattr__(self, name, value)

    def poke(self, **slots):
        for name, value in slots.items():
            object.__setattr__(self, name, value)

    def import_credential(self, purpose, credential):
        self.calls.append((purpose, credential))
        if self._closed or self._tainted or self._busy or self._stage != 'IMPORT':
            raise RefusalDouble('installer_context')
        if purpose == self.fail_on:
            self.poke(_tainted=True)
            raise RuntimeError(f'double failure {self.leak!r}' if self.leak else 'double failure')
        self.poke(_imported=self._imported | {purpose})
        if purpose in self.hooks:
            self.hooks[purpose]()

    def run(self, operation_key):
        self.runs.append(operation_key)
        raise RefusalDouble('installer_context')


class CoreDouble:
    """NAMED DOUBLE of cortex_core.runtime.custody for exactly the two private functions the contract names. `_current_installer` models the conditions of `_bound_installer` (custody.py lines 341-353 at
    blob 3c971310): the bound owner/config/vault/original deadline, not closed, not tainted, stage IMPORT or RUN, same deadline object/type/value, unexpired. `failed` records every `_installer_fail`.
    The context-variable token refresh of the real function is NOT modeled."""

    def __init__(self, clock):
        self.clock, self.binding, self.failed = clock, None, []

    def bind(self, owner):
        self.binding = (owner, owner._config, owner._vault, owner._deadline)

    def _current_installer(self, config, custody, deadline):
        if self.binding is None:
            raise RefusalDouble('installer_context')
        owner, original_config, vault, original = self.binding
        if (config is not original_config or custody is not vault or owner._config is not config or owner._vault is not vault or owner._closed or owner._tainted
                or owner._stage not in ('IMPORT', 'RUN') or owner._deadline is not original or type(deadline) is not type(original) or deadline != original):
            raise RefusalDouble('installer_context')
        if self.clock() >= original:
            raise RefusalDouble('installer_deadline')
        return owner

    def _installer_fail(self, owner):
        owner.poke(_tainted=True, _stage='ERROR')
        self.failed.append(owner)


class ProducerDouble:
    """NAMED DOUBLE of the J06 producer (synthetic fixed values; the real producer is undefined). `hooks` run after the ask is recorded, `raises` raises instead of returning."""

    def __init__(self, values=None, hooks=None, raises=None):
        self.values, self.asked = dict(SYNTH if values is None else values), []
        self.hooks, self.raises = hooks or {}, raises or {}

    def bytes_for(self, purpose):
        self.asked.append(purpose)
        if purpose in self.raises:
            raise self.raises[purpose]
        if purpose in self.hooks:
            self.hooks[purpose]()
        return self.values[purpose]


class ReceiverDouble:
    """NAMED DOUBLE of the fixed S4 receiver render.receive_bootstrap(owner, bootstrap) (S4 head dd9c2f80, render.py blob 7e94cc47): records the pair; nothing passes a callable. It may fail, raise
    a given exception or run a scenario hook."""

    def __init__(self, fail=False, hook=None, raises=None):
        self.got, self.owners, self.fail, self.hook, self.raises = [], [], fail, hook, raises

    def __call__(self, owner, bootstrap):
        self.owners.append(owner)
        self.got.append(bootstrap)
        if self.hook:
            self.hook()
        if self.raises:
            raise self.raises
        if self.fail:
            raise OSError('receiver failure')


_UNSET = object()


def future(seconds=60.0):
    return time.monotonic() + seconds


class Harness:
    """Builds one live scenario and calls make_secret with the named doubles installed (owner type, Core admission, fixed receiver, clock). OMITTED arguments take the valid double; an explicit
    None stays None. Supported-caller proof is NOT claimed."""

    def raw(self, owner=_UNSET, producer=_UNSET, receiver=_UNSET, deadline=_UNSET, real_type=False, core=_UNSET, prepare=None, **kw):
        self.clock = ClockDouble()
        self.deadline = self.clock() + 60.0 if deadline is _UNSET else deadline
        self.owner = OwnerDouble(deadline=self.deadline, **kw) if owner is _UNSET else owner
        if isinstance(self.owner, OwnerDouble) and self.owner._deadline is None:
            self.owner.poke(_deadline=self.deadline)
        self.core = CoreDouble(self.clock) if core is _UNSET else core
        if isinstance(self.core, CoreDouble) and self.core.binding is None and isinstance(self.owner, OwnerDouble):
            self.core.bind(self.owner)
        self.producer = ProducerDouble() if producer is _UNSET else producer
        self.receiver = ReceiverDouble() if receiver is _UNSET else receiver
        self.inner = []
        if prepare:
            prepare(self)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(M, '_owner_type', return_value=OwnerDouble) if not real_type else mock.patch.object(M, '_owner_type', return_value=None))  # real_type: the resolver in its UNBOUND state (Core absent); the real binding is tested in test_make_secret_core_join.py
            stack.enter_context(mock.patch.object(M, '_core', return_value=self.core))
            stack.enter_context(mock.patch.object(M, '_receiver', return_value=self.receiver))
            stack.enter_context(mock.patch.object(time, 'monotonic', self.clock))
            try:
                return ('ok', M.make_secret(self.owner, producer=self.producer, deadline=self.deadline))
            except M.MakeSecretRefusal as refusal:
                return ('refused', refusal)

    def call(self, *args, **kw):
        kind, value = self.raw(*args, **kw)
        return (kind, value.reason if kind == 'refused' else value)

    def reenter(self):
        """What a callback does when it calls make_secret again on the same owner (inside the installed doubles)."""
        try:
            return ('ok', M.make_secret(self.owner, producer=ProducerDouble(), deadline=self.deadline))
        except M.MakeSecretRefusal as refusal:
            return ('refused', refusal.reason)


class Component(Harness, unittest.TestCase):
    """COMPONENT-ONLY: replaces the resolvers with the named doubles; supported-caller proof is NOT claimed."""

    def test_imports_exactly_the_five_purposes_once_in_the_frozen_order_and_returns_names_only(self):
        outcome = self.call()
        self.assertEqual(outcome, ('ok', {'imported': list(FIVE), 'bootstrap_delivered': True}))
        self.assertEqual([c[0] for c in self.owner.calls], list(FIVE))
        self.assertEqual([c[1] for c in self.owner.calls], [SYNTH[p] for p in FIVE])
        self.assertEqual(self.producer.asked, list(FIVE))
        self.assertNotIn(b'SYNTHETIC', json.dumps(outcome[1]).encode())

    def test_the_bootstrap_bytes_that_were_imported_reach_the_fixed_receiver_once_and_only_after_all_five_imports(self):
        order = []
        owner = OwnerDouble()
        original = owner.import_credential
        owner.import_credential = lambda p, c: (order.append(('import', p)), original(p, c))[1]
        receiver = ReceiverDouble(hook=lambda: order.append(('receiver', receiver.got[-1])))
        self.assertEqual(self.call(owner=owner, receiver=receiver)[0], 'ok')
        self.assertEqual(order, [('import', p) for p in FIVE] + [('receiver', SYNTH['bootstrap'])])
        self.assertEqual(receiver.owners, [owner])

    def test_every_credential_is_validated_before_the_first_import(self):
        short = dict(SYNTH, migrator=b'tooshort')
        self.assertEqual(self.call(producer=ProducerDouble(short)), ('refused', 'secret_invalid:migrator'))
        self.assertEqual((self.owner.calls, self.receiver.got), ([], []))
        for bad in (None, 'text', bytearray(SYNTH['probe'])):
            self.assertEqual(self.call(producer=ProducerDouble(dict(SYNTH, probe=bad))), ('refused', 'secret_invalid:probe'), repr(bad))
            self.assertEqual(self.owner.calls, [])

    def test_two_purposes_may_never_share_one_credential(self):
        same = dict(SYNTH, probe=SYNTH['api_request'])
        self.assertEqual(self.call(producer=ProducerDouble(same)), ('refused', 'secret_reused'))
        self.assertEqual((self.owner.calls, self.receiver.got), ([], []))

    def test_a_failing_import_stops_the_sequence_and_the_receiver_never_runs(self):
        self.assertEqual(self.call(fail_on='api_request'), ('refused', 'import_failed:api_request'))
        self.assertEqual([c[0] for c in self.owner.calls], ['bootstrap', 'migrator', 'api_request'])
        self.assertEqual(self.receiver.got, [])

    def test_a_failing_receiver_is_a_typed_refusal_after_the_five_imports(self):
        self.assertEqual(self.call(receiver=ReceiverDouble(fail=True)), ('refused', 'bootstrap_receiver_failed'))
        self.assertEqual([c[0] for c in self.owner.calls], list(FIVE))

    def test_only_the_exact_owner_type_is_authority_and_the_producer_is_not_asked_first(self):
        for name, owner in (('dict', {'import_credential': None}), ('none', None), ('subclass', type('Sub', (OwnerDouble,), {})()), ('plain object', object())):
            outcome = self.call(owner=owner)
            self.assertEqual(outcome, ('refused', 'custody_owner_type'), name)
            self.assertEqual(self.producer.asked, [], name)

    def test_with_the_real_core_type_unbound_at_this_base_the_call_refuses_before_any_producer_use(self):
        outcome = self.call(owner=OwnerDouble(), real_type=True)
        self.assertEqual(outcome, ('refused', 'custody_owner_unbound'))
        self.assertEqual((self.producer.asked, self.owner.calls), ([], []))

    def test_an_undefined_j06_producer_is_a_typed_refusal_with_no_default(self):
        for name, producer in (('none', None), ('no method', object())):
            outcome = self.call(producer=producer)
            self.assertEqual(outcome, ('refused', 'j06_producer_undefined'), name)
            self.assertEqual((self.owner.calls, self.receiver.got), ([], []), name)

    def test_the_one_original_deadline_must_be_finite_unexpired_and_not_a_bool(self):
        for name, deadline, want in (('bool', True, 'deadline_invalid'), ('none', None, 'deadline_invalid'), ('text', '9', 'deadline_invalid'), ('nan', math.nan, 'deadline_invalid'),
                                     ('inf', math.inf, 'deadline_invalid'), ('past', time.monotonic() - 1.0, 'deadline_expired')):
            outcome = self.call(deadline=deadline)
            self.assertEqual(outcome, ('refused', want), name)
            self.assertEqual((self.producer.asked, self.owner.calls), ([], []), name)

    def test_no_exception_text_repr_or_traceback_carries_a_credential_value(self):
        secret = SYNTH['migrator']
        kind, refusal = self.raw(fail_on='migrator', leak=secret)
        text = ''.join(traceback.format_exception(refusal)) + repr(refusal) + str(refusal) if kind == 'refused' else ''
        self.assertEqual(kind, 'refused', 'make_secret must refuse a failing import')
        self.assertEqual(refusal.reason, 'import_failed:migrator')
        self.assertNotIn('SYNTHETIC', text)


class Lifetime(Harness, unittest.TestCase):
    """LIFETIME (HALE-S1-LIFETIME-002), COMPONENT-ONLY over named doubles: the existing private Core same-context admission (`_current_installer`) and failure path (`_installer_fail`) around the
    producer, the five imports and the fixed receiver, with the owner's ORIGINAL deadline. Each case is independently reachable and has its own live, closed, expired, reentrant or lying double.
    Expected today: every behavioural predicate FAILS against the refusing skeleton; none is a setup or import error. Actual Core and actual S4 are NOT RUN."""

    def expect(self, outcome, reason, *, asked=None, imported=None, delivered=None, failed=None):
        with self.subTest('reason'):
            self.assertEqual(outcome, ('refused', reason))
        if asked is not None:
            with self.subTest('producer asks'):
                self.assertEqual(self.producer.asked, asked)
        if imported is not None:
            with self.subTest('import attempts'):
                self.assertEqual([c[0] for c in self.owner.calls], imported)
        if delivered is not None:
            with self.subTest('receiver deliveries'):
                self.assertEqual(self.receiver.got, delivered)
        if failed is not None:
            with self.subTest('owner invalidated through the Core fail path'):
                self.assertEqual(bool(self.core.failed) and all(o is self.owner for o in self.core.failed), failed)

    # ---- before the producer: admission and original deadline ----
    def test_a_closed_owner_is_refused_before_the_producer_is_asked(self):
        outcome = self.call(prepare=lambda s: s.owner.poke(_closed=True))
        self.expect(outcome, 'custody_context', asked=[], imported=[], delivered=[], failed=True)

    def test_a_tainted_owner_is_refused_before_the_producer_is_asked(self):
        outcome = self.call(prepare=lambda s: s.owner.poke(_tainted=True))
        self.expect(outcome, 'custody_context', asked=[], imported=[], delivered=[], failed=True)

    def test_an_owner_that_is_not_at_the_import_stage_or_is_busy_is_refused_before_the_producer_is_asked(self):
        for name, slots in (('run', {'_stage': 'RUN'}), ('complete', {'_stage': 'COMPLETE'}), ('error', {'_stage': 'ERROR'}), ('busy', {'_busy': True})):
            with self.subTest(name):
                outcome = self.call(prepare=lambda s, slots=slots: s.owner.poke(**slots))
                self.expect(outcome, 'custody_context', asked=[], imported=[], delivered=[], failed=True)

    def test_an_owner_that_already_holds_an_import_is_refused_so_nothing_can_replay(self):
        outcome = self.call(prepare=lambda s: s.owner.poke(_imported=frozenset({'bootstrap'})))
        self.expect(outcome, 'custody_context', asked=[], imported=[], delivered=[], failed=True)

    def test_an_owner_of_the_exact_type_that_is_not_the_context_bound_current_owner_is_refused(self):
        def foreign(scenario):
            other = OwnerDouble(deadline=scenario.deadline)
            scenario.core.bind(other)
        outcome = self.call(prepare=foreign)
        self.expect(outcome, 'custody_context', asked=[], imported=[], delivered=[], failed=True)

    def test_an_exact_type_lookalike_that_copies_the_bound_slots_is_not_the_bound_owner_and_is_refused(self):
        def lookalike(scenario):
            bound = OwnerDouble(deadline=scenario.deadline)
            scenario.owner.poke(_config=bound._config, _vault=bound._vault)
            scenario.core.bind(bound)
        outcome = self.call(prepare=lookalike)
        self.expect(outcome, 'custody_context', asked=[], imported=[], delivered=[], failed=True)

    def test_a_caller_deadline_that_is_not_the_owners_original_deadline_is_refused_before_the_producer(self):
        original = future(60.0)
        for name, offered in (('later', original + 30.0), ('earlier', original - 30.0)):
            with self.subTest(name):
                outcome = self.call(owner=OwnerDouble(deadline=original), deadline=offered)
                self.expect(outcome, 'deadline_not_original', asked=[], imported=[], delivered=[], failed=True)

    def test_an_unbound_fixed_receiver_is_refused_before_any_producer_use_and_leaves_the_owner_untouched(self):
        outcome = self.call(receiver=None)
        self.expect(outcome, 'bootstrap_receiver_unbound', asked=[], imported=[], failed=False)

    def test_an_unbound_core_admission_is_refused_before_any_producer_use(self):
        outcome = self.call(core=None)
        self.expect(outcome, 'custody_owner_unbound', asked=[], imported=[], delivered=[])

    # ---- after every producer callback ----
    def test_a_producer_that_closes_the_context_stops_the_sequence_at_once_with_no_import(self):
        outcome = self.call(producer=ProducerDouble(hooks={'probe': lambda: self.owner.poke(_closed=True)}))
        self.expect(outcome, 'custody_context', asked=['bootstrap', 'migrator', 'api_request', 'probe'], imported=[], delivered=[], failed=True)

    def test_a_producer_that_runs_past_the_original_deadline_stops_the_sequence_with_no_import(self):
        outcome = self.call(producer=ProducerDouble(hooks={'probe': lambda: self.clock.advance(120.0)}))
        self.expect(outcome, 'deadline_expired', asked=['bootstrap', 'migrator', 'api_request', 'probe'], imported=[], delivered=[], failed=True)

    def test_a_producer_that_advances_the_owner_to_another_stage_stops_the_sequence_with_no_import(self):
        outcome = self.call(producer=ProducerDouble(hooks={'migrator': lambda: self.owner.poke(_stage='RUN')}))
        self.expect(outcome, 'custody_context', asked=['bootstrap', 'migrator'], imported=[], delivered=[], failed=True)

    def test_a_producer_that_imports_through_the_owner_itself_is_refused_before_s1_imports_anything(self):
        def sneak():
            self.owner.import_credential('bootstrap', SYNTH['bootstrap'])
        outcome = self.call(producer=ProducerDouble(hooks={'migrator': sneak}))
        self.expect(outcome, 'custody_context', asked=['bootstrap', 'migrator'], imported=['bootstrap'], delivered=[], failed=True)

    def test_a_producer_that_reenters_make_secret_is_refused_and_the_swallowed_refusal_still_ends_the_call(self):
        outcome = self.call(producer=ProducerDouble(hooks={'migrator': lambda: self.inner.append(self.reenter())}))
        with self.subTest('the reentered call'):
            self.assertEqual(self.inner, [('refused', 'make_secret_reentered')])
        self.expect(outcome, 'custody_context', asked=['bootstrap', 'migrator'], imported=[], delivered=[], failed=True)

    def test_a_producer_that_lets_the_reentry_refusal_escape_is_a_typed_producer_failure(self):
        def escape():
            raise M.MakeSecretRefusal('make_secret_reentered')
        outcome = self.call(producer=ProducerDouble(hooks={'migrator': escape}))
        self.expect(outcome, 'producer_failed:migrator', asked=['bootstrap', 'migrator'], imported=[], delivered=[], failed=True)

    def test_a_failing_producer_is_a_typed_refusal_with_no_value_in_it_and_the_owner_is_invalidated(self):
        kind, refusal = self.raw(producer=ProducerDouble(raises={'probe': RuntimeError(SYNTH['probe'].decode())}))
        text = ''.join(traceback.format_exception(refusal)) + repr(refusal) + str(refusal) if kind == 'refused' else ''
        self.expect((kind, refusal.reason if kind == 'refused' else refusal), 'producer_failed:probe', asked=['bootstrap', 'migrator', 'api_request', 'probe'], imported=[], delivered=[], failed=True)
        with self.subTest('no value in the text'):
            self.assertNotIn('SYNTHETIC', text)

    def test_an_interrupt_from_the_producer_propagates_after_the_owner_is_invalidated(self):
        raised = None
        try:
            self.call(producer=ProducerDouble(raises={'migrator': KeyboardInterrupt()}))
        except KeyboardInterrupt as error:
            raised = error
        with self.subTest('the interrupt propagates'):
            self.assertIsNotNone(raised)
        with self.subTest('owner invalidated'):
            self.assertTrue(self.core.failed and all(o is self.owner for o in self.core.failed))
        with self.subTest('nothing imported or delivered'):
            self.assertEqual((self.owner.calls, self.receiver.got), ([], []))

    def test_a_validation_refusal_after_production_invalidates_the_owner_so_the_producer_cannot_be_asked_again(self):
        outcome = self.call(producer=ProducerDouble(dict(SYNTH, migrator=b'tooshort')))
        self.expect(outcome, 'secret_invalid:migrator', imported=[], delivered=[], failed=True)

    def test_a_reused_credential_refusal_invalidates_the_owner(self):
        outcome = self.call(producer=ProducerDouble(dict(SYNTH, probe=SYNTH['api_request'])))
        self.expect(outcome, 'secret_reused', imported=[], delivered=[], failed=True)

    # ---- between and after the five imports ----
    def test_an_import_that_closes_the_context_stops_before_the_next_import(self):
        outcome = self.call(prepare=lambda s: s.owner.hooks.update(migrator=lambda: s.owner.poke(_closed=True)))
        self.expect(outcome, 'custody_context', imported=['bootstrap', 'migrator'], delivered=[], failed=True)

    def test_an_import_that_runs_past_the_original_deadline_stops_before_the_next_import(self):
        outcome = self.call(prepare=lambda s: s.owner.hooks.update(migrator=lambda: s.clock.advance(120.0)))
        self.expect(outcome, 'deadline_expired', imported=['bootstrap', 'migrator'], delivered=[], failed=True)

    def test_an_import_that_reenters_make_secret_is_refused_and_the_call_ends_without_delivery(self):
        outcome = self.call(prepare=lambda s: s.owner.hooks.update(migrator=lambda: s.inner.append(s.reenter())))
        with self.subTest('the reentered call'):
            self.assertEqual(self.inner, [('refused', 'make_secret_reentered')])
        self.expect(outcome, 'custody_context', imported=['bootstrap', 'migrator'], delivered=[], failed=True)

    def test_after_the_final_import_a_closed_context_is_refused_before_the_receiver_is_called(self):
        outcome = self.call(prepare=lambda s: s.owner.hooks.update(installer_enrollment=lambda: s.owner.poke(_closed=True)))
        self.expect(outcome, 'custody_context', imported=list(FIVE), delivered=[], failed=True)

    def test_after_the_final_import_an_expired_deadline_is_refused_before_the_receiver_is_called(self):
        outcome = self.call(prepare=lambda s: s.owner.hooks.update(installer_enrollment=lambda: s.clock.advance(120.0)))
        self.expect(outcome, 'deadline_expired', imported=list(FIVE), delivered=[], failed=True)

    def test_success_is_never_inferred_from_the_import_count_the_owner_must_hold_all_five(self):
        outcome = self.call(prepare=lambda s: s.owner.hooks.update(installer_enrollment=lambda: s.owner.poke(_imported=s.owner._imported - {'probe'})))
        self.expect(outcome, 'custody_context', imported=list(FIVE), delivered=[], failed=True)

    def test_a_failing_import_invalidates_the_owner_through_the_core_fail_path(self):
        outcome = self.call(fail_on='api_request')
        self.expect(outcome, 'import_failed:api_request', imported=['bootstrap', 'migrator', 'api_request'], delivered=[], failed=True)

    # ---- the fixed receiver ----
    def test_the_fixed_receiver_gets_the_owner_and_the_same_imported_bootstrap_bytes_once_after_the_admitted_final_import(self):
        outcome = self.call()
        with self.subTest('result'):
            self.assertEqual(outcome, ('ok', {'imported': list(FIVE), 'bootstrap_delivered': True}))
        with self.subTest('receiver call'):
            self.assertEqual((self.receiver.owners, self.receiver.got), ([self.owner], [SYNTH['bootstrap']]))
        with self.subTest('same bytes as the bootstrap import'):
            self.assertEqual(self.receiver.got, [dict(self.owner.calls).get('bootstrap')])

    def test_a_receiver_that_returns_after_closing_the_context_is_a_late_success_and_is_refused(self):
        receiver = ReceiverDouble(hook=lambda: self.owner.poke(_closed=True))
        outcome = self.call(receiver=receiver)
        self.expect(outcome, 'custody_context', imported=list(FIVE), delivered=[SYNTH['bootstrap']], failed=True)

    def test_a_receiver_that_returns_after_the_original_deadline_is_a_late_success_and_is_refused(self):
        receiver = ReceiverDouble(hook=lambda: self.clock.advance(120.0))
        outcome = self.call(receiver=receiver)
        self.expect(outcome, 'deadline_expired', imported=list(FIVE), delivered=[SYNTH['bootstrap']], failed=True)

    def test_a_refusing_receiver_is_a_typed_refusal_and_the_owner_is_invalidated(self):
        outcome = self.call(receiver=ReceiverDouble(fail=True))
        self.expect(outcome, 'bootstrap_receiver_failed', imported=list(FIVE), delivered=[SYNTH['bootstrap']], failed=True)

    def test_an_interrupt_from_the_receiver_propagates_after_the_owner_is_invalidated(self):
        raised = None
        try:
            self.call(receiver=ReceiverDouble(raises=KeyboardInterrupt()))
        except KeyboardInterrupt as error:
            raised = error
        with self.subTest('the interrupt propagates'):
            self.assertIsNotNone(raised)
        with self.subTest('owner invalidated'):
            self.assertTrue(self.core.failed and all(o is self.owner for o in self.core.failed))

    def test_a_receiver_that_reenters_make_secret_is_refused_and_the_bootstrap_is_not_delivered_twice(self):
        receiver = ReceiverDouble(hook=lambda: self.inner.append(self.reenter()))
        outcome = self.call(receiver=receiver)
        with self.subTest('the reentered call'):
            self.assertEqual(self.inner, [('refused', 'make_secret_reentered')])
        self.expect(outcome, 'custody_context', imported=list(FIVE), delivered=[SYNTH['bootstrap']], failed=True)

    def test_a_refusal_caught_inside_the_open_owner_cannot_be_followed_by_another_attempt(self):
        first = self.call(receiver=ReceiverDouble(fail=True))
        owner, core, asked = self.owner, self.core, list(self.producer.asked)
        second = self.call(owner=owner, core=core, producer=ProducerDouble(), deadline=owner._deadline)
        with self.subTest('first attempt'):
            self.assertEqual(first, ('refused', 'bootstrap_receiver_failed'))
        with self.subTest('second attempt'):
            self.assertEqual(second, ('refused', 'custody_context'))
        with self.subTest('the producer was not asked again and nothing more was imported'):
            self.assertEqual((self.producer.asked, len(owner.calls), asked), ([], len(FIVE), list(FIVE)))

    def test_a_successful_call_leaves_the_owner_admitted_unmodified_and_unrun(self):
        outcome = self.call()
        with self.subTest('success'):
            self.assertEqual(outcome[0], 'ok')
        with self.subTest('no invalidation'):
            self.assertEqual(self.core.failed, [])
        with self.subTest('slots as Core left them'):
            self.assertEqual((self.owner._stage, self.owner._tainted, self.owner._closed, self.owner._busy, self.owner._imported), ('IMPORT', False, False, False, frozenset(FIVE)))
        with self.subTest('the finite sequence is the caller s, S1 never runs it'):
            self.assertEqual(self.owner.runs, [])


class FixtureReachability(unittest.TestCase):
    """FIXTURE-ONLY (HALE-S1-NONE-001). A labeled recording spy replaces `M.make_secret`, so these tests prove only what the FIXTURE hands the callee. They are not S1 acceptance and earn no
    implementation credit. The first test reproduces the observation of the S1 review: the purported None owner reaches the callee as an OwnerDouble."""

    def run_unchanged(self, method_name):
        seen = []

        def spy(owner, **kwargs):
            seen.append((owner, kwargs))
            raise M.MakeSecretRefusal('custody_owner_type' if method_name.startswith('test_only_the_exact_owner_type') else 'j06_producer_undefined')
        result = unittest.TestResult()
        with mock.patch.object(M, 'make_secret', spy):
            Component(method_name).run(result)
        return seen, result

    def test_the_owner_denial_cases_deliver_a_dict_none_a_subclass_and_a_plain_object_to_the_callee(self):
        seen, result = self.run_unchanged('test_only_the_exact_owner_type_is_authority_and_the_producer_is_not_asked_first')
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assertEqual([type(owner).__name__ for owner, _ in seen], ['dict', 'NoneType', 'Sub', 'object'])
        self.assertEqual([owner is None for owner, _ in seen], [False, True, False, False])

    def test_an_explicit_none_owner_reaches_the_callee_as_none_with_the_producer_and_sink_untouched(self):
        seen = []

        def spy(owner, **kwargs):
            seen.append((owner, kwargs))
            raise M.MakeSecretRefusal('custody_owner_type')
        fixture = Component('call')
        with mock.patch.object(M, 'make_secret', spy):
            self.assertEqual(fixture.call(owner=None), ('refused', 'custody_owner_type'))
        self.assertEqual(len(seen), 1)
        self.assertIsNone(seen[0][0])
        self.assertEqual((fixture.producer.asked, fixture.receiver.got), ([], []))

    def test_an_omitted_owner_is_the_valid_owner_double_and_the_undefined_producer_cases_stay_direct(self):
        seen = []

        def spy(owner, **kwargs):
            seen.append((owner, kwargs))
            raise M.MakeSecretRefusal('j06_producer_undefined')
        fixture = Component('call')
        with mock.patch.object(M, 'make_secret', spy):
            fixture.call()
        self.assertIsInstance(seen[0][0], OwnerDouble)
        seen, result = self.run_unchanged('test_an_undefined_j06_producer_is_a_typed_refusal_with_no_default')
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assertEqual([type(kwargs['producer']).__name__ for _, kwargs in seen], ['NoneType', 'object'])


class CallerUnsigned(unittest.TestCase):
    """CALLER (actual install.sh, no signing, no key): expected to PASS today and to stay passing. The signed-caller control is NOT RUN."""

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix='nix-s1-')))
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', str(self.tmp)]))
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        (self.bin / 'podman').write_text(STUB)
        (self.bin / 'podman').chmod(0o755)
        self.log = self.tmp / 'stub.log'
        self.release = self.tmp / 'release'
        self.release.mkdir()
        self.vault, self.prefix = self.tmp / 'vault', self.tmp / 'prefix'

    def run_install(self, *extra):
        env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}', STUB_LOG=str(self.log))
        return subprocess.run(['sh', str(INSTALL / 'install.sh'), '--release-dir', str(self.release), '--vault', str(self.vault), '--project', 'demo', '--prefix', str(self.prefix),
                               '--platform', 'linux-arm64', *extra], capture_output=True, text=True, env=env)

    def reason(self, res):
        try:
            return json.loads(res.stderr.strip().splitlines()[-1])['reason']
        except (ValueError, IndexError, KeyError):
            return f'<no typed refusal: {res.stderr[-160:]!r}>'

    def effects(self):
        return [c for c in (self.log.read_text().splitlines() if self.log.exists() else []) if not c.startswith('--version')]

    def test_the_full_run_without_a_signature_refuses_at_the_verifier_with_no_authority_or_effect(self):
        res = self.run_install()
        self.assertEqual((res.returncode, self.reason(res)), (2, 'release_signature_missing'))
        self.assertEqual((self.effects(), self.vault.exists(), self.prefix.exists()), ([], False, False))

    def test_a_make_secret_entry_without_a_signature_never_reaches_custody_or_a_secret(self):
        res = self.run_install('--only', 'make_secret')
        self.assertEqual(res.returncode, 2)
        self.assertEqual(self.reason(res), 'release_signature_missing')
        self.assertEqual((self.effects(), self.vault.exists(), self.prefix.exists()), ([], False, False))
        self.assertNotIn('SYNTHETIC', res.stdout + res.stderr)


class Static(unittest.TestCase):
    """STATIC / INTERFACE guards: expected to pass now and to constrain the future implementation."""

    def code(self):
        return re.sub(r'"""(.|\n)*?"""', '', (INSTALL / 'make_secret.py').read_text())

    def test_the_make_secret_module_has_no_secret_source_sink_or_channel_of_its_own(self):
        code = self.code()
        for banned in (r'\bos\.environ\b', r'\bsys\.argv\b', r'\bsubprocess\b', r'\bopen\(', r'\bprint\(', r'\blogging\b', r'\bos\.urandom\b', r'\bsecrets\.', r'\brandom\b',
                       r'\bsocket\b', r'\burllib\b', r'\bhttp\b', r'\bshutil\b', r'\bpathlib\b'):
            self.assertIsNone(re.search(banned, code), banned)

    def test_install_sh_keeps_make_secret_first_and_unwired_so_the_actual_refusal_path_is_unchanged(self):
        text = (INSTALL / 'install.sh').read_text()
        self.assertRegex(text, r"UNWIRED='make_secret write_config provision_roles bootstrap serve'")

    def test_the_published_signature_has_one_producer_and_the_one_original_deadline_and_no_caller_sink(self):
        parameters = list(inspect.signature(M.make_secret).parameters.values())
        self.assertEqual([(p.name, p.kind) for p in parameters], [('owner', inspect.Parameter.POSITIONAL_OR_KEYWORD), ('producer', inspect.Parameter.KEYWORD_ONLY),
                                                                  ('deadline', inspect.Parameter.KEYWORD_ONLY)])
        with self.assertRaises(TypeError):
            M.make_secret(object(), producer=object(), bootstrap_sink=lambda data: None, deadline=1.0)

    def test_the_three_resolvers_are_unbound_at_this_base_so_no_authority_exists_by_import(self):
        # Same assertion, evaluated where Core genuinely cannot be imported (a child interpreter whose path holds only this directory); in a tree whose Core IS importable the resolvers bind
        # (see test_make_secret_core_join.py).
        done = subprocess.run([sys.executable, '-I', '-B', '-c', 'import sys\nsys.path[:0]=[sys.argv[1]]\nimport make_secret as M\nprint(repr((M._owner_type(), M._core(), M._receiver())))', str(INSTALL)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-300:])
        self.assertEqual(done.stdout.strip(), repr((None, None, None)))

    def test_the_module_writes_no_owner_slot_runs_no_sequence_and_keeps_no_flag_or_store(self):
        code = self.code()
        for banned in (r'\bobject\.__setattr__\b', r'\bsetattr\(', r'\bglobal\b', r'\bnonlocal\b', r'\.run\(', r'bootstrap_sink', r'\bsink\b', r'^(from|import) +(cortex_core|cortex_ship)'):
            self.assertIsNone(re.search(banned, code, re.M), banned)


if __name__ == '__main__':
    unittest.main()
