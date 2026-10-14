"""Frozen U3-A oracle: BODY failures and missing surfaces are distinct.

Explicit invocation only; this pending future gate is not a default-suite skip.
No crypto key, signature, image, process, native or install authority is made.
The inherited RC2 double tests only supported Python API identity behavior.
"""
import importlib
from pathlib import Path
import sys
import unittest

NEXT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(NEXT / 'src'), str(NEXT / 'tests/runtime')]
from cortex_core.runtime import signed_member as port
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble

AGGREGATE = 'sha256:' + 'a' * 64
MEMBER = 'runtime.lock.json'
RAW = b'NON_AUTHORITATIVE_U3_A_IDENTITY_BYTES\n'
MISSING = 'R01 unit 3: signed Core/API aggregate and member admission'


class EqualityObject:
    comparisons = 0

    def __eq__(self, other):
        self.comparisons += 1
        return True

    def __ne__(self, other):
        self.comparisons += 1
        return False


class EqualityString(str):
    comparisons = 0

    def __eq__(self, other):
        self.comparisons += 1
        return True

    def __ne__(self, other):
        self.comparisons += 1
        return False


class SameString(str):
    pass


FACTORIES = {
    'equality_object': lambda expected: EqualityObject(),
    'wrong_string_subclass': lambda expected: EqualityString('different'),
    'same_string_subclass': lambda expected: SameString(expected),
}


def member():
    double = NonAuthoritativeSignedMemberDouble(RAW, aggregate_id=AGGREGATE, member_name=MEMBER)
    with double.installed(port):
        result = port.request_member(AGGREGATE, MEMBER)
    return result


def refusal(fn):
    try:
        fn()
    except port.Refusal as error:
        return error.reason
    return None


def optional_module(name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name:
            raise  # dependency/setup failure is never a surface-absence result
        return None


class FrozenOracle(unittest.TestCase):
    def test_CONTROL_production_default_is_exact_unbound_before_reader(self):
        with self.assertRaises(port.Refusal) as caught:
            port.request_member(AGGREGATE, MEMBER)
        self.assertEqual(caught.exception.reason, 'required_binding_gap')
        self.assertEqual(caught.exception.unit, MISSING)
        self.assertIs(port._original_authority, port._unbound)

    def test_CONTROL_exact_strings_read_original_snapshot(self):
        sealed = member()
        self.assertEqual(port.read_member(sealed, aggregate_id=AGGREGATE, member_name=MEMBER), RAW)
        self.assertEqual(refusal(lambda: port.read_member(sealed, aggregate_id=AGGREGATE+'b', member_name=MEMBER)),
                         'member_identity_mismatch')

    def test_SURFACE_R01_original_member_backend_missing(self):
        module = optional_module('cortex_core.release.member_admission')
        self.assertIsNotNone(module, 'SURFACE_ABSENCE: R01 original-member backend; no verifier body executed')
        self.assertTrue(callable(getattr(module, 'request_original_member', None)), 'SURFACE_ABSENCE: backend callable')

    def test_SURFACE_R01_unsigned_assembly_missing(self):
        module = importlib.import_module('cortex_core.release.contracts')
        self.assertTrue(callable(getattr(module, 'assemble_aggregate', None)),
                        'SURFACE_ABSENCE: R01 assembly; no assembly body executed')

    def test_SURFACE_isolated_test_factory_missing(self):
        # This is future test support outside installed source, not a root binder.
        module = optional_module('r01u3_support.factory')
        self.assertIsNotNone(module, 'SURFACE_ABSENCE: test-only factory; zero signature/root cases executed')
        self.assertTrue(callable(getattr(module, 'test_authority', None)), 'SURFACE_ABSENCE: test factory callable')


def reader_case(field, label, factory):
    def test(self):
        sealed = member()
        actual = factory(AGGREGATE if field == 'aggregate_id' else MEMBER)
        arguments = dict(aggregate_id=AGGREGATE, member_name=MEMBER)
        arguments[field] = actual
        reason = refusal(lambda: port.read_member(sealed, **arguments))
        self.assertEqual(reason, 'member_identity_mismatch',
                         'BODY: U3-ID-READER-%s-%s must reject exact-type/string mismatch before equality' % (field, label))
        self.assertEqual(getattr(actual, 'comparisons', 0), 0, 'BODY: crafted equality must never execute')
    return test


def request_case(field, label, factory):
    def test(self):
        double = NonAuthoritativeSignedMemberDouble(RAW, aggregate_id=AGGREGATE, member_name=MEMBER)
        arguments = dict(aggregate_id=AGGREGATE, member_name=MEMBER)
        arguments[field] = factory(AGGREGATE if field == 'aggregate_id' else MEMBER)
        with double.installed(port):
            reason = refusal(lambda: port.request_member(**arguments))
        self.assertEqual(reason, 'member_identity_mismatch', 'CONTROL: existing port exact request identity guard')
        self.assertEqual(double.reads, [], 'CONTROL: no raw read after crafted request')
    return test


for field in ('aggregate_id', 'member_name'):
    for label, factory in FACTORIES.items():
        setattr(FrozenOracle, 'test_BODY_reader_%s_%s' % (field, label), reader_case(field, label, factory))
        setattr(FrozenOracle, 'test_CONTROL_request_%s_%s' % (field, label), request_case(field, label, factory))


if __name__ == '__main__':
    sys.path.insert(0, str(NEXT / 'tests/release'))
    unittest.main(verbosity=2)
