"""Product entry ordering and closed plan limits; never launch an inspector."""
import copy
import unittest
from unittest.mock import patch
from fixture import canonical,digest
import native_matrix_fixture as matrix
import test_native_policy_unit2_trust_red as trust
from test_unit1_red import product


class InspectionBoundary(unittest.TestCase):
    def admitted_native_wire(self):
        value,raw,_,_,_,policy=trust.native_case()
        module=product('inputs')
        return module.admit_build_input(module.parse_build_input(canonical(value)),trust.ObservedInputs(value,raw),policy),policy

    def test_original_unaccepted_raw_receipt_never_parses_or_reads(self):
        admitted,policy=self.admitted_native_wire();module=product('receipts');calls=[]
        def reader(path):calls.append(path);raise AssertionError('reader called')
        with patch.object(module,'parse_build_receipt',wraps=module.parse_build_receipt) as parser:
            with self.assertRaisesRegex(ValueError,'^native_receipt_not_independently_accepted$'):
                module.verify_native_receipt_bytes(b'not-json',admitted,reader,reader,policy)
        self.assertEqual(parser.call_count,0);self.assertEqual(calls,[])
        self.assertFalse(admitted.native_qualified)

    def test_exact_raw_digest_does_not_authorize_unaccepted_policy(self):
        admitted,policy=self.admitted_native_wire();raw=b'not-json'
        policy=dict(policy,receipt_sha256=product('codec').sha256(raw),policy_sha256='a'*64)
        module=product('receipts')
        with patch.object(module,'parse_build_receipt',wraps=module.parse_build_receipt) as parser:
            with self.assertRaisesRegex(ValueError,'^receipt_audit_policy$'):
                module.verify_native_receipt_bytes(raw,admitted,lambda p:b'',lambda p:b'',policy)
        self.assertEqual(parser.call_count,0)

    def test_candidate_inspector_dict_is_not_a_sealed_execution_capability(self):
        profile={'schema':'cortex.release-policy.v1','inspector':{'execution':'contained'}}
        module=product('inspector')
        with self.assertRaisesRegex(ValueError,'^inspector_capability_untrusted$'):
            module.prepare_plan({'_seal':'claimed','root':'/candidate'},profile,b'')

    def test_profile_identity_and_unknown_rule_fields_refuse(self):
        case=matrix.family();value,raw,*_=case;module=product('inputs')
        bad=copy.deepcopy(value);bad['admission_policy']['policy_sha256']='a'*64
        acceptance=dict(case[5],input_sha256=digest(bad),policy_sha256='a'*64)
        with self.assertRaisesRegex(ValueError,'^profile_policy_identity$'):
            module.admit_build_input(module.parse_build_input(canonical(bad)),raw.__getitem__,acceptance)


if __name__=='__main__':unittest.main(verbosity=2)
