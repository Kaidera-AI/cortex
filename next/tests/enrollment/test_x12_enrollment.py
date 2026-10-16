"""X12 installer enrollment oracle (Focus gB1, kai rulings Q1/Q2 16 Oct).

Q1 transport: the enrollment request is an install-owner ORIGINAL bound by the
consented effect's expected_after_contract_ref; it runs only at P2a's consumed
dispatch (UNBOUND in source); its receipt is the P1 readiness.checks_ref record.
Q2: one human roundtrip at enrollment; every health call re-reads 006 and
re-resolves the roundtrip scope. Ports are fixtures; no real credential/value.
Each negative has a matched positive so a refusal is causal.
"""
import ast
import asyncio
import copy
import hashlib
import importlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import UUID

NEXT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(NEXT/'src'))
from cortex_core.runtime.signed_member import Refusal

INST = '11111111-1111-4111-8111-111111111111'
TEN = '22222222-2222-4222-8222-222222222222'
PROJ = '33333333-3333-4333-8333-333333333333'
PRIN = '44444444-4444-4444-8444-444444444444'
CRED = '55555555-5555-4555-8555-555555555555'
HANDLE = '66666666-6666-4666-8666-666666666666'
DIGEST = 'a' * 64


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()


def ref(path, raw):
    return {'path': path, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


CONSENT = ref('enrollment/consent.json', b'{"purpose":"enrollment-only"}\n')


def request_doc(**change):
    doc = {'schema': 'cortex.enrollment-request.v1', 'installation_id': INST, 'tenant_id': TEN,
           'project_id': PROJ, 'principal_id': PRIN, 'credential_id': CRED, 'credential_digest': DIGEST,
           'idempotency_key': 'enroll-1', 'consent_ref': CONSENT, 'roundtrip_handle': HANDLE}
    doc.update(change)
    return doc


def effect_context(raw, **change):
    effect = {'effect_id': 'cortex.enrollment.first-identity', 'product': 'cortex', 'resource_kind': 'interface',
              'expected_before_ref': ref('a', b'a'), 'expected_after_contract_ref': ref('enrollment/request.json', raw),
              'idempotency_ref': ref('b', b'b'), 'reversal_contract_ref': ref('c', b'c'), 'data_policy_ref': ref('d', b'd')}
    effect.update(change)
    return {'context': {'identity': {'product': 'cortex', 'installation_id': INST}, 'effect': effect},
            'journal_ref': ref('journal/00000000000000000003.json', b'consumed'), 'preimages_ref': ref('p', b'p')}


class FakeCore:
    """005/006 semantics in RAM: installer-only, one-use, same-input replay."""
    def __init__(self):
        self.receipt = None
        self.identities = 0
        self.lost_reply_once = False

    def first_identity(self, request):
        key = tuple(request[k] for k in ('installation_id', 'tenant_id', 'project_id', 'principal_id',
                                         'credential_id', 'credential_digest', 'idempotency_key'))
        if self.receipt is not None:
            if self.receipt != key:
                raise Refusal('enrollment_forbidden')
            return {'principal_id': PRIN, 'credential_id': CRED, 'project_id': PROJ, 'replayed': True}
        self.receipt = key
        self.identities += 1
        if self.lost_reply_once:
            self.lost_reply_once = False
            raise ConnectionError('commit reply lost')
        return {'principal_id': request['principal_id'], 'credential_id': request['credential_id'],
                'project_id': request['project_id'], 'replayed': False}


def good_roundtrip(request):
    return {'route': 'C11-D1', 'status': 200, 'principal_id': request['principal_id'],
            'project_id': request['project_id'], 'response_sha256': 'b' * 64}


P1 = json.loads((NEXT / 'tests/install/fixtures/p1-subjects-pgc20-v1.json').read_bytes())['receipt']


def accepted_original(accepted, delivered, request_raw=None):
    """What the fixed install owner delivers: its protected P1 receipt (whose
    readiness.checks_ref names the ACCEPTED enrollment receipt), the stored
    enrollment receipt bytes and the consumed request reference."""
    if accepted is None:
        return None  # owner-attested: no enrollment recorded yet
    request_raw = canonical(request_doc()) if request_raw is None else request_raw
    p1 = copy.deepcopy(P1)
    p1['identity']['installation_id'] = INST
    p1['disposition'] = 'created_by_this_transaction'
    p1['product_state'] = {'source_revision': 'a' * 40, **{k: ref(k, k.encode()) for k in (
        'runtime_inventory_ref', 'module_inventory_ref', 'schema_manifest_ref', 'api_descriptor_ref', 'product_interface_ref')}}
    p1['readiness'] = {'state': 'ready', 'descriptor_ref': ref('descriptor', b'd'), 'connection_custody_ref': ref('custody', b'c'),
                       'checks_ref': ref('enrollment/receipt.json', accepted)}
    return {'p1_receipt': canonical(p1), 'enrollment_receipt': delivered,
            'request_ref': ref('enrollment/request.json', request_raw)}


def owner_reader(m, readiness, scope, delivered, accepted='same'):
    """Build the D3 reader the way production does: the receipt comes ONLY from the
    owner delivery. (Pre-fix fallback: the old candidate-bytes API, RED evidence.)"""
    accepted = delivered if accepted == 'same' else accepted
    if not hasattr(m, '_accepted_enrollment'):
        return m.enrollment_state_reader(readiness, scope, delivered)
    with patch.object(m, '_accepted_enrollment', return_value=accepted_original(accepted, delivered)):
        return m.enrollment_state_reader(readiness, scope)


def module():
    try:
        return importlib.import_module('cortex_core.runtime.enrollment')
    except ModuleNotFoundError as error:
        if error.name != 'cortex_core.runtime.enrollment':
            raise
        raise AssertionError('SURFACE_ABSENCE: cortex_core.runtime.enrollment') from None


def consumed(m):
    # Test-only stand-in for P2a's consumed dispatch, like trust's _production_context.
    return patch.object(m, '_consumed_dispatch', return_value=None)


class X12(unittest.TestCase):
    def refused(self, reason, fn):
        result = None
        try:
            fn()
        except Exception as error:
            result = error
        self.assertIsInstance(result, Refusal, 'BODY: accepted or leaked a non-refusal')
        self.assertEqual(result.reason, reason, 'BODY: exact refusal')

    def enroll(self, m, core, raw=None, roundtrip=good_roundtrip, context=None):
        raw = canonical(request_doc()) if raw is None else raw
        with consumed(m):
            return m.enroll(effect_context(raw) if context is None else context, raw,
                            first_identity=core.first_identity, roundtrip=roundtrip)

    # ---- X12-a: frozen transport -------------------------------------------------
    def test_A1_default_dispatch_is_unbound_before_any_candidate(self):
        m = module()
        self.assertIs(m._consumed_dispatch, m._unbound)
        core = FakeCore()
        raw = canonical(request_doc())
        self.refused('required_binding_gap', lambda: m.enroll(effect_context(raw), raw,
                     first_identity=core.first_identity, roundtrip=good_roundtrip))
        self.assertEqual(core.identities, 0)

    def test_A2_request_is_closed_canonical_and_value_free(self):
        m = module()
        self.assertEqual(m.parse_request(canonical(request_doc()))['principal_id'], PRIN)
        bad = [canonical(request_doc(extra=1)), json.dumps(request_doc()).encode(),
               canonical(request_doc(credential_digest='A' * 64)), canonical(request_doc(idempotency_key='')),
               canonical(request_doc(principal_id='not-a-uuid')), canonical(request_doc(schema='v2')),
               canonical(request_doc(credential_secret='x')), canonical(request_doc(principal_id='ABCDEF12-3456-4789-8ABC-DEF012345678')),
               canonical({k: v for k, v in request_doc().items() if k != 'consent_ref'})]
        for raw in bad:
            self.refused('enrollment_request_invalid', lambda raw=raw: m.parse_request(raw))

    def test_A3_request_must_be_the_consumed_effects_original(self):
        m = module()
        raw = canonical(request_doc())
        other = canonical(request_doc(idempotency_key='enroll-2'))
        self.refused('enrollment_scope_denied', lambda: self.enroll(m, FakeCore(), raw=other, context=effect_context(raw)))
        self.refused('enrollment_scope_denied', lambda: self.enroll(
            m, FakeCore(), context=effect_context(raw, effect_id='cortex.other')))
        self.refused('enrollment_scope_denied', lambda: self.enroll(
            m, FakeCore(), context=effect_context(raw, product='kos')))
        ctx = effect_context(raw)
        ctx['context']['identity']['installation_id'] = TEN
        self.refused('enrollment_scope_denied', lambda: self.enroll(m, FakeCore(), context=ctx))
        self.assertTrue(self.enroll(m, FakeCore()))  # matched positive

    def test_A4_authority_is_checked_before_the_candidate_is_parsed(self):
        m = module()
        with patch.object(m, 'parse_request', side_effect=AssertionError('parsed before authority')):
            self.refused('required_binding_gap', lambda: m.enroll(effect_context(b'x'), b'x',
                         first_identity=FakeCore().first_identity, roundtrip=good_roundtrip))

    # ---- E01-E12 ------------------------------------------------------------------
    def reader(self, m, state, receipt, rows=None):
        async def readiness(installation):
            if isinstance(state, BaseException):
                raise state
            return state
        async def scope(digest, installation, project):
            return [] if rows is None else rows
        return owner_reader(m, readiness, scope, receipt)

    def state(self, reader, installation=INST):
        return asyncio.run(reader(UUID(installation)))

    def receipt(self, m):
        return self.enroll(m, FakeCore())

    def row(self, **change):
        r = {'installation_id': INST, 'tenant_id': TEN, 'project_id': PROJ, 'principal_id': PRIN}
        r.update(change)
        return [r]

    def test_E01_fresh_install_is_enrollment_required(self):
        m = module()
        self.assertEqual(self.state(self.reader(m, 'enrollment_required', None)), 'enrollment_required')

    def test_E02_identities_without_receipt_are_unavailable(self):
        m = module()
        self.assertEqual(self.state(self.reader(m, 'unavailable', None)), 'unavailable')

    def test_E03_reader_cannot_enroll_and_enroll_needs_the_installer_port(self):
        m = module()
        tree = ast.parse(Path(m.__file__).read_text())
        reader = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'enrollment_state_reader')
        names = {n.id for n in ast.walk(reader) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(reader) if isinstance(n, ast.Attribute)}
        self.assertFalse({'first_identity', 'enroll', 'FIRST_IDENTITY_SQL'} & names, 'BODY: reader reaches enrollment')

        class Denied(Exception):
            sqlstate = '42501'

        class Cursor:
            def __init__(self, error=None):
                self.calls, self.error = [], error
            def execute(self, sql, params):
                self.calls.append((sql, params))
                if self.error:
                    raise self.error
            def fetchone(self):
                return ({'principal_id': PRIN, 'credential_id': CRED, 'project_id': PROJ, 'replayed': False},)
        request = m.parse_request(canonical(request_doc()))
        denied = Cursor(Denied('first_identity_forbidden'))
        self.refused('enrollment_forbidden', lambda: m.first_identity_sql(denied, request))
        ok = Cursor()
        self.assertEqual(m.first_identity_sql(ok, request)['principal_id'], PRIN)  # matched positive
        self.assertEqual(ok.calls, [(m.FIRST_IDENTITY_SQL, (INST, TEN, PROJ, PRIN, CRED, DIGEST, 'enroll-1'))])
        self.refused('enrollment_unavailable', lambda: m.first_identity_sql(Cursor(ConnectionError('gone')), request))

    def enroll_with(self, m, first_identity, roundtrip=good_roundtrip):
        raw = canonical(request_doc())
        with consumed(m):
            return m.enroll(effect_context(raw), raw, first_identity=first_identity, roundtrip=roundtrip)

    def test_E04_same_input_replays_same_receipt_changed_input_refuses(self):
        m = module()
        core = FakeCore()
        first = self.enroll(m, core)
        self.assertEqual(self.enroll(m, core), first)
        self.assertEqual(core.identities, 1)
        changed = canonical(request_doc(idempotency_key='enroll-2'))
        self.refused('enrollment_forbidden', lambda: self.enroll(m, core, raw=changed))
        self.assertEqual(core.identities, 1)
        # 005's answer must name exactly the requested identity.
        for key, value in (('principal_id', TEN), ('credential_id', TEN), ('project_id', TEN), ('replayed', 'no')):
            odd = lambda r, key=key, value=value: dict(FakeCore().first_identity(r), **{key: value})
            self.refused('enrollment_unavailable', lambda odd=odd: self.enroll_with(m, odd))
        self.refused('enrollment_unavailable', lambda: self.enroll_with(m, lambda r: dict(FakeCore().first_identity(r), x=1)))

    def test_E05_receipt_without_roundtrip_is_not_ready(self):
        m = module()
        def failing(_):
            raise ConnectionError('api down')
        self.refused('enrollment_roundtrip_failed', lambda: self.enroll(m, FakeCore(), roundtrip=failing))
        self.assertEqual(self.state(self.reader(m, 'ready', None, self.row())), 'unavailable')
        self.assertEqual(self.state(self.reader(m, 'ready', self.receipt(m), self.row())), 'ready')  # positive

    def test_E06_other_principal_project_or_installation_is_not_ready(self):
        m = module()
        for key, value in (('principal_id', TEN), ('project_id', TEN)):
            bad = lambda r, key=key, value=value: dict(good_roundtrip(r), **{key: value})
            self.refused('enrollment_roundtrip_mismatch', lambda bad=bad: self.enroll(m, FakeCore(), roundtrip=bad))
        for bad in (lambda r: dict(good_roundtrip(r), route='C01-R102'), lambda r: dict(good_roundtrip(r), status=401)):
            self.refused('enrollment_roundtrip_mismatch', lambda bad=bad: self.enroll(m, FakeCore(), roundtrip=bad))
        receipt = self.receipt(m)
        self.assertEqual(self.state(self.reader(m, 'ready', receipt, self.row()), installation=TEN), 'unavailable')
        for change in ({'principal_id': TEN}, {'project_id': TEN}, {'tenant_id': PROJ}, {'installation_id': TEN}):
            self.assertEqual(self.state(self.reader(m, 'ready', receipt, self.row(**change))), 'unavailable')
        self.assertEqual(self.state(self.reader(m, 'ready', receipt, self.row() + self.row())), 'unavailable')
        self.assertEqual(self.state(self.reader(m, 'ready', receipt, self.row())), 'ready')  # positive

    def test_E07_ready_needs_both_truths(self):
        m = module()
        receipt = self.receipt(m)
        self.assertEqual(self.state(self.reader(m, 'ready', receipt, [])), 'unavailable')
        self.assertEqual(self.state(self.reader(m, 'enrollment_required', receipt, self.row())), 'unavailable')
        self.assertEqual(self.state(self.reader(m, 'READY', receipt, self.row())), 'unavailable')
        self.assertEqual(self.state(self.reader(m, 'ready', b'{}\n', self.row())), 'unavailable')
        self.assertEqual(self.state(self.reader(m, 'enrollment_required', b'{}\n', self.row())), 'unavailable')
        self.assertEqual(self.state(self.reader(m, 'ready', receipt, self.row())), 'ready')

    def test_E08_source_audit_no_route_default_principal_or_raw_writes(self):
        m = module()
        text = Path(m.__file__).read_text()
        for banned in ('INSERT', 'UPDATE ', 'DELETE', '@app.', 'add_route', 'Route(', 'default_principal'):
            self.assertNotIn(banned, text, 'BODY: forbidden path ' + banned)
        sql = [n.value for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Constant) and isinstance(n.value, str) and 'SELECT' in n.value]
        self.assertEqual(sorted(sql), sorted([m.FIRST_IDENTITY_SQL, m.READINESS_SQL, m.SCOPE_SQL]))
        self.assertIn('auth.first_identity(', m.FIRST_IDENTITY_SQL)
        self.assertIn('auth.enrollment_readiness(', m.READINESS_SQL)
        self.assertIn('auth.resolve_scope(', m.SCOPE_SQL)

    def test_E09_lost_db_or_listener_is_unavailable(self):
        m = module()
        receipt = self.receipt(m)
        self.assertEqual(self.state(self.reader(m, ConnectionError('db gone'), receipt, self.row())), 'unavailable')
        def down(_):
            raise ConnectionError('db gone')
        self.refused('enrollment_unavailable', lambda: self.enroll_with(m, down))

    def test_E10_lost_commit_reply_reconciles_the_same_attempt(self):
        m = module()
        core = FakeCore()
        core.lost_reply_once = True
        self.refused('enrollment_unavailable', lambda: self.enroll(m, core))
        receipt = self.enroll(m, core)
        self.assertEqual(core.identities, 1, 'BODY: a second identity was created')
        self.assertEqual(receipt, self.enroll(m, FakeCore()))

    def test_E11_inspection_credential_cannot_enroll_or_write(self):
        m = module()
        ops = json.loads((NEXT/'contracts/runtime/runtime-operations-v1.json').read_text())
        self.assertNotIn('installer_enrollment', ops['secret_handles_by_mode']['inspect'])
        self.assertEqual(ops['roles']['installer_enrollment']['credential_consumers'], ['enrollment-owner-client'])
        self.assertFalse(hasattr(owner_reader(m, None, None, None), 'enroll'))

    def test_E12_reader_is_bounded_by_the_existing_health_budget(self):
        m = module()
        from cortex_core.api.c11a import EnrollmentHealth
        async def slow(_):
            await asyncio.sleep(5)
            return 'ready'
        async def core():
            return {'core_available': True, 'state': 'healthy'}
        async def scope(*_):
            return self.row()
        health = EnrollmentHealth(core, owner_reader(m, slow, scope, self.receipt(m)), UUID(INST), timeout_seconds=.05)
        self.assertEqual(asyncio.run(health())['readiness'], 'unavailable')
        async def fast(_):
            return 'ready'
        health = EnrollmentHealth(core, owner_reader(m, fast, scope, self.receipt(m)), UUID(INST), timeout_seconds=.05)
        self.assertEqual(asyncio.run(health()), {'component': 'cortex', 'status': 'ok', 'readiness': 'ready'})

    # ---- X12-001 (Vera P2): the receipt must be the ACCEPTED original -----------
    def corrupt(self, m, field):
        original = self.receipt(m); doc = json.loads(original)
        if field == 'request_sha256': doc[field] = 'c' * 64
        elif field == 'response_sha256': doc['roundtrip'][field] = 'c' * 64
        elif field == 'credential_id': doc[field] = HANDLE
        elif field == 'consent_ref': doc[field] = dict(doc[field], path='enrollment/replaced-consent.json')
        mutated = canonical(doc); self.assertNotEqual(mutated, original)
        m.parse_receipt(mutated)  # schema-valid: only provenance can refuse it
        return original, mutated

    def probe(self, m, delivered, accepted, scope=None):
        async def readiness(_): return 'ready'
        async def rows(*_): return self.row()
        return asyncio.run(owner_reader(m, readiness, scope or rows, delivered, accepted)(UUID(INST)))

    def test_V1_changed_request_digest_refuses(self):
        m = module(); original, mutated = self.corrupt(m, 'request_sha256')
        self.assertEqual(self.probe(m, mutated, original), 'unavailable', 'BODY_RED: changed admitted request digest became ready')
        self.assertEqual(self.probe(m, original, original), 'ready')  # matched positive

    def test_V2_changed_roundtrip_digest_refuses(self):
        m = module(); original, mutated = self.corrupt(m, 'response_sha256')
        self.assertEqual(self.probe(m, mutated, original), 'unavailable', 'BODY_RED: changed roundtrip response digest became ready')

    def test_V3_changed_credential_identity_refuses(self):
        m = module(); original, mutated = self.corrupt(m, 'credential_id')
        self.assertEqual(self.probe(m, mutated, original), 'unavailable', 'BODY_RED: unrelated credential UUID became ready')

    def test_V4_changed_consent_reference_refuses(self):
        m = module(); original, mutated = self.corrupt(m, 'consent_ref')
        self.assertEqual(self.probe(m, mutated, original), 'unavailable', 'BODY_RED: substituted consent reference became ready')

    def test_V5_replaced_principal_cannot_hide_first_credential_revocation(self):
        m = module(); original = self.receipt(m); doc = json.loads(original)
        other_p, other_c, other_d = '77777777-7777-4777-8777-777777777777', '88888888-8888-4888-8888-888888888888', 'd' * 64
        doc.update(principal_id=other_p, credential_id=other_c, credential_digest=other_d)
        doc['roundtrip']['principal_id'] = other_p
        forged = canonical(doc); m.parse_receipt(forged)
        async def scope(digest, installation, project):
            if digest == other_d:
                return [{'installation_id': INST, 'tenant_id': TEN, 'project_id': PROJ, 'principal_id': other_p}]
            return []  # first credential A revoked
        self.assertEqual(self.probe(m, original, original, scope), 'unavailable')
        self.assertEqual(self.probe(m, forged, original, scope), 'unavailable',
                         'BODY_RED: replacing the first identity receipt hides the revoked first credential')

    def test_V6_no_candidate_receipt_parameter_and_unbound_default_is_unavailable(self):
        m = module()
        import inspect
        self.assertTrue(hasattr(m, '_accepted_enrollment'), 'BODY_RED: no owner-delivered receipt admission')
        self.assertEqual(list(inspect.signature(m.enrollment_state_reader).parameters), ['readiness', 'resolve_scope'],
                         'BODY_RED: the reader still accepts candidate receipt bytes')
        async def ready(_): return 'ready'
        async def rows(*_): return self.row()
        self.assertEqual(asyncio.run(m.enrollment_state_reader(ready, rows)(UUID(INST))), 'unavailable')  # UNBOUND

    def test_V7_receipt_bound_to_recorded_request_and_installation(self):
        m = module(); receipt = self.receipt(m)
        async def ready(_): return 'ready'
        async def rows(*_): return self.row()
        if hasattr(m, '_accepted_enrollment'):
            other_request = canonical(request_doc(idempotency_key='enroll-other'))
            record = accepted_original(receipt, receipt, other_request)
            with patch.object(m, '_accepted_enrollment', return_value=record):
                self.assertEqual(asyncio.run(m.enrollment_state_reader(ready, rows)(UUID(INST))), 'unavailable')
            record = accepted_original(receipt, receipt)
            p1 = json.loads(record['p1_receipt']); p1['identity']['installation_id'] = TEN
            record['p1_receipt'] = canonical(p1)
            with patch.object(m, '_accepted_enrollment', return_value=record):
                self.assertEqual(asyncio.run(m.enrollment_state_reader(ready, rows)(UUID(INST))), 'unavailable')
            p1['identity']['installation_id'] = INST; p1['readiness']['state'] = 'not_checked'
            for k in ('descriptor_ref', 'connection_custody_ref', 'checks_ref'): p1['readiness'][k] = None
            record['p1_receipt'] = canonical(p1)
            with patch.object(m, '_accepted_enrollment', return_value=record):
                self.assertEqual(asyncio.run(m.enrollment_state_reader(ready, rows)(UUID(INST))), 'unavailable')
        else:
            self.fail('BODY_RED: receipt not bound to the recorded request/installation')

    # ---- receipt -----------------------------------------------------------------
    def test_R1_receipt_is_canonical_value_free_and_binds_request_and_roundtrip(self):
        m = module()
        raw = canonical(request_doc())
        receipt = self.enroll(m, FakeCore(), raw=raw)
        doc = m.parse_receipt(receipt)
        self.assertEqual(receipt, canonical(json.loads(receipt)))
        self.assertEqual(doc['request_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual((doc['installation_id'], doc['tenant_id'], doc['project_id'], doc['principal_id']), (INST, TEN, PROJ, PRIN))
        self.assertEqual(doc['roundtrip']['route'], 'C11-D1')
        self.assertNotIn('replayed', receipt.decode())
        self.assertNotIn(HANDLE, receipt.decode(), 'BODY: resolver handle copied into the receipt')
        self.refused('enrollment_receipt_invalid', lambda: m.parse_receipt(canonical(dict(json.loads(receipt), extra=1))))


if __name__ == '__main__':
    unittest.main(verbosity=2)
