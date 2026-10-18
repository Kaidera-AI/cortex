"""S1 make_secret (day 3): INTENTIONAL RED. Deciding behaviour of the future make_secret against the published interface (make_secret.py, make_secret_interface.md).
Labels (each test says which): COMPONENT = make_secret called directly with NAMED DOUBLES (OwnerDouble, ProducerDouble, SinkDouble) and a replaced `_owner_type`; it earns NO supported-caller
credit. CALLER = the actual install.sh on an UNSIGNED release folder (no key exists or is created) - expected to PASS today and to keep passing. STATIC = a source guard, expected to pass.
All values are synthetic non-secrets (`SYNTHETIC-NONSECRET-...`); no key, signature, unlock, custody file, database or Podman effect exists here, and test_sign.py is not imported.
NOT RUN (named, not coded): the signed-caller control (needs a signature), the real Core InstallerCustody (not at this base), a real J06 producer, the unlock descriptor, initdb transfer, and the
config-before-import join (make_secret_interface.md options A/B, awaiting Cox)."""
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


class OwnerDouble:
    """NAMED DOUBLE of Core's InstallerCustody: records import_credential calls; optionally fails on one purpose."""

    def __init__(self, fail_on=None, leak=None):
        self.calls, self.fail_on, self.leak = [], fail_on, leak

    def import_credential(self, purpose, credential):
        self.calls.append((purpose, credential))
        if purpose == self.fail_on:
            raise RuntimeError(f'double failure {self.leak!r}' if self.leak else 'double failure')


class ProducerDouble:
    """NAMED DOUBLE of the J06 producer (synthetic fixed values; the real producer is undefined)."""

    def __init__(self, values=None):
        self.values, self.asked = dict(SYNTH if values is None else values), []

    def bytes_for(self, purpose):
        self.asked.append(purpose)
        return self.values[purpose]


class SinkDouble:
    def __init__(self, fail=False):
        self.got, self.fail = [], fail

    def __call__(self, data):
        self.got.append(data)
        if self.fail:
            raise OSError('sink failure')


_UNSET = object()


def future(seconds=60.0):
    return time.monotonic() + seconds


class Component(unittest.TestCase):
    """COMPONENT-ONLY: replaces `_owner_type` with the double's type; supported-caller proof is NOT claimed."""

    def call(self, owner=None, producer=None, sink=None, deadline=_UNSET, real_type=False, **kw):
        self.owner = OwnerDouble(**kw) if owner is None else owner
        self.producer = ProducerDouble() if producer is None else producer
        self.sink = SinkDouble() if sink is None else sink
        patch = mock.patch.object(M, '_owner_type', return_value=OwnerDouble) if not real_type else mock.patch.object(M, '_owner_type', M._owner_type)
        with patch:
            try:
                return ('ok', M.make_secret(self.owner, producer=self.producer, bootstrap_sink=self.sink, deadline=future() if deadline is _UNSET else deadline))
            except M.MakeSecretRefusal as refusal:
                return ('refused', refusal.reason)

    def test_imports_exactly_the_five_purposes_once_in_the_frozen_order_and_returns_names_only(self):
        outcome = self.call()
        self.assertEqual(outcome, ('ok', {'imported': list(FIVE), 'bootstrap_delivered': True}))
        self.assertEqual([c[0] for c in self.owner.calls], list(FIVE))
        self.assertEqual([c[1] for c in self.owner.calls], [SYNTH[p] for p in FIVE])
        self.assertEqual(self.producer.asked, list(FIVE))
        self.assertNotIn(b'SYNTHETIC', json.dumps(outcome[1]).encode())

    def test_the_bootstrap_bytes_that_were_imported_reach_the_sink_once_and_only_after_all_five_imports(self):
        order = []
        owner = OwnerDouble()
        original = owner.import_credential
        owner.import_credential = lambda p, c: (order.append(('import', p)), original(p, c))[1]
        sink = mock.Mock(side_effect=lambda data: order.append(('sink', data)))
        self.assertEqual(self.call(owner=owner, sink=sink)[0], 'ok')
        self.assertEqual(order, [('import', p) for p in FIVE] + [('sink', SYNTH['bootstrap'])])

    def test_every_credential_is_validated_before_the_first_import(self):
        short = dict(SYNTH, migrator=b'tooshort')
        self.assertEqual(self.call(producer=ProducerDouble(short)), ('refused', 'secret_invalid:migrator'))
        self.assertEqual((self.owner.calls, self.sink.got), ([], []))
        for bad in (None, 'text', bytearray(SYNTH['probe'])):
            self.assertEqual(self.call(producer=ProducerDouble(dict(SYNTH, probe=bad))), ('refused', 'secret_invalid:probe'), repr(bad))
            self.assertEqual(self.owner.calls, [])

    def test_two_purposes_may_never_share_one_credential(self):
        same = dict(SYNTH, probe=SYNTH['api_request'])
        self.assertEqual(self.call(producer=ProducerDouble(same)), ('refused', 'secret_reused'))
        self.assertEqual((self.owner.calls, self.sink.got), ([], []))

    def test_a_failing_import_stops_the_sequence_and_the_sink_never_runs(self):
        self.assertEqual(self.call(fail_on='api_request'), ('refused', 'import_failed:api_request'))
        self.assertEqual([c[0] for c in self.owner.calls], ['bootstrap', 'migrator', 'api_request'])
        self.assertEqual(self.sink.got, [])

    def test_a_failing_sink_is_a_typed_refusal_after_the_five_imports(self):
        self.assertEqual(self.call(sink=SinkDouble(fail=True)), ('refused', 'bootstrap_sink_failed'))
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
            self.owner, self.sink = OwnerDouble(), SinkDouble()
            with mock.patch.object(M, '_owner_type', return_value=OwnerDouble):
                try:
                    outcome = ('ok', M.make_secret(self.owner, producer=producer, bootstrap_sink=self.sink, deadline=future()))
                except M.MakeSecretRefusal as refusal:
                    outcome = ('refused', refusal.reason)
            self.assertEqual(outcome, ('refused', 'j06_producer_undefined'), name)
            self.assertEqual((self.owner.calls, self.sink.got), ([], []), name)

    def test_the_one_original_deadline_must_be_finite_unexpired_and_not_a_bool(self):
        for name, deadline, want in (('bool', True, 'deadline_invalid'), ('none', None, 'deadline_invalid'), ('text', '9', 'deadline_invalid'), ('nan', math.nan, 'deadline_invalid'),
                                     ('inf', math.inf, 'deadline_invalid'), ('past', time.monotonic() - 1.0, 'deadline_expired')):
            outcome = self.call(deadline=deadline)
            self.assertEqual(outcome, ('refused', want), name)
            self.assertEqual((self.producer.asked, self.owner.calls), ([], []), name)

    def test_no_exception_text_repr_or_traceback_carries_a_credential_value(self):
        secret = SYNTH['migrator']
        owner = OwnerDouble(fail_on='migrator', leak=secret)
        outcome = None
        with mock.patch.object(M, '_owner_type', return_value=OwnerDouble):
            try:
                M.make_secret(owner, producer=ProducerDouble(), bootstrap_sink=SinkDouble(), deadline=future())
            except M.MakeSecretRefusal as refusal:
                outcome = refusal
                text = ''.join(traceback.format_exception(refusal)) + repr(refusal) + str(refusal)
        self.assertIsNotNone(outcome, 'make_secret must refuse a failing import')
        self.assertEqual(outcome.reason, 'import_failed:migrator')
        self.assertNotIn('SYNTHETIC', text)


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
        self.assertEqual((fixture.producer.asked, fixture.sink.got), ([], []))

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
    """STATIC guards: expected to pass now and to constrain the future implementation."""

    def test_the_make_secret_module_has_no_secret_source_sink_or_channel_of_its_own(self):
        source = (INSTALL / 'make_secret.py').read_text()
        code = re.sub(r'"""(.|\n)*?"""', '', source)
        for banned in (r'\bos\.environ\b', r'\bsys\.argv\b', r'\bsubprocess\b', r'\bopen\(', r'\bprint\(', r'\blogging\b', r'\bos\.urandom\b', r'\bsecrets\.', r'\brandom\b',
                       r'\bsocket\b', r'\burllib\b', r'\bhttp\b', r'\bshutil\b', r'\bpathlib\b'):
            self.assertIsNone(re.search(banned, code), banned)

    def test_install_sh_keeps_make_secret_first_and_unwired_so_the_actual_refusal_path_is_unchanged(self):
        text = (INSTALL / 'install.sh').read_text()
        self.assertRegex(text, r"UNWIRED='make_secret write_config provision_roles bootstrap serve'")


if __name__ == '__main__':
    unittest.main()
