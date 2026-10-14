"""Independent RC2-IDENTITY-001 oracle, frozen before the reader guards.

Synthetic genuine seals reuse RC2's existing test-only authority double. These
tests prove argument matching, not signatures, real R01 binding or isolation.
"""
from pathlib import Path
import sys
import unittest

NEXT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(NEXT/'tests/runtime'))
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble
from cortex_core.runtime import signed_member as port

AGGREGATE='identity-oracle-aggregate'
MEMBER='runtime.lock.json'
RAW=b'public synthetic original member bytes\n'


class ReaderIdentity(unittest.TestCase):
    def member(self):
        double=NonAuthoritativeSignedMemberDouble(RAW,aggregate_id=AGGREGATE,member_name=MEMBER)
        with double.installed(port):
            return port.request_member(AGGREGATE,MEMBER)

    def check_refusal(self,field,value,callbacks,*,member=None,reason='member_identity_mismatch'):
        args={'aggregate_id':AGGREGATE,'member_name':MEMBER,field:value}
        seal=self.member() if member is None else member
        try:
            port.read_member(seal,**args)
            actual='RETURNED_BYTES'
        except port.Refusal as error:
            actual=error.reason
        with self.subTest(field=field,gate='closed-refusal'):
            self.assertEqual(actual,reason)
        with self.subTest(field=field,gate='no-equality-callback'):
            self.assertEqual(callbacks,[])

    def subclass_cases(self,field):
        calls=[]
        class PlainSubclass(str):pass
        class DeceptiveSubclass(str):
            def __eq__(self,other):calls.append('eq');return True
            def __ne__(self,other):calls.append('ne');return False
            def __str__(self):raise AssertionError('must not coerce identity')
        correct=AGGREGATE if field=='aggregate_id' else MEMBER
        for value in (PlainSubclass(correct),DeceptiveSubclass('different')):
            calls.clear()
            with self.subTest(kind=type(value).__name__):
                self.check_refusal(field,value,calls)

    def lookalike(self,field):
        calls=[]
        class Lookalike:
            def __eq__(self,other):calls.append('eq');return True
            def __ne__(self,other):calls.append('ne');return False
            def __str__(self):raise AssertionError('must not stringify identity')
        self.check_refusal(field,Lookalike(),calls)

    def test_aggregate_subclasses_refuse_without_comparison(self):
        self.subclass_cases('aggregate_id')

    def test_member_subclasses_refuse_without_comparison(self):
        self.subclass_cases('member_name')

    def test_aggregate_lookalike_refuses_without_comparison(self):
        self.lookalike('aggregate_id')

    def test_member_lookalike_refuses_without_comparison(self):
        self.lookalike('member_name')

    def test_exact_builtin_strings_require_the_exact_value(self):
        member=self.member()
        self.assertEqual(port.read_member(member,aggregate_id=AGGREGATE,member_name=MEMBER),RAW)
        for field in ('aggregate_id','member_name'):
            with self.subTest(field=field):self.check_refusal(field,'wrong',[],member=member)

    def test_other_builtin_types_are_closed_refusals(self):
        for field in ('aggregate_id','member_name'):
            for value in (None,False,0,b'identity'):
                with self.subTest(field=field,kind=type(value).__name__):
                    self.check_refusal(field,value,[])

    def test_provenance_precedes_identity_argument_comparison(self):
        calls=[]
        class Poison:
            def __eq__(self,other):calls.append('eq');raise AssertionError('comparison')
            def __ne__(self,other):calls.append('ne');raise AssertionError('comparison')
        forged=object.__new__(port.OriginalMember)
        for field in ('aggregate_id','member_name'):
            with self.subTest(field=field):
                self.check_refusal(field,Poison(),calls,member=forged,reason='unsealed_member')

    def test_production_request_gap_still_precedes_argument_inspection(self):
        class Poison:
            def __eq__(self,other):raise AssertionError('comparison')
            def __ne__(self,other):raise AssertionError('comparison')
        with self.assertRaises(port.Refusal) as caught:
            port.request_member(Poison(),Poison())
        self.assertEqual((caught.exception.reason,caught.exception.unit),
                         ('required_binding_gap',port.MISSING_UNIT))
