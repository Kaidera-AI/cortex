"""Frozen R02 consumer boundaries through the single R01 policy carrier."""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from cortex_core.release.build import inputs, receipts
from cortex_core.release import inputs as owner_inputs, receipts as owner_receipts
from cortex_core.release.codec import canonical_bytes, sha256
from cortex_core.release.contracts import thaw
sys.path.insert(0, str(Path(__file__).parents[1] / 'release'))
import native_matrix_fixture as matrix
import test_native_policy_unit2_trust_red as trust


class ProfileConsumer(unittest.TestCase):
    def native(self):
        value, data, _, _, _, acceptance = trust.native_case()
        admitted = owner_inputs.admit_build_input(owner_inputs.parse_build_input(canonical_bytes(value)), trust.ObservedInputs(value, data), acceptance)
        return value, data, acceptance, admitted

    def test_profile_v1_positive_consumes_owner_metadata(self):
        value, data, receipt, outputs, evidence, acceptance = matrix.family()
        admitted = inputs.admit(canonical_bytes(value), data.__getitem__, acceptance)
        self.assertEqual(thaw(admitted.metadata)['release_policy']['schema'], 'cortex.release-policy.v1')
        checked = receipts.verify_shared_receipt(canonical_bytes(receipt), admitted, outputs.__getitem__, evidence.__getitem__, acceptance)
        self.assertFalse(checked.native_qualified)

    def test_native_legacy_profile_refuses_before_preparation(self):
        value, data, acceptance, _ = self.native()
        with self.assertRaisesRegex(ValueError, '^r02_profile_v1_required$'):
            inputs.admit(canonical_bytes(value), trust.ObservedInputs(value, data), acceptance)

    def test_absent_profile_refuses_owner_admission(self):
        value, data, *_, acceptance = matrix.family()
        path = value['descriptor']['path']
        descriptor = json.loads(data[path]); descriptor.pop('release_policy')
        data[path] = canonical_bytes(descriptor)
        value['descriptor'].update(bytes=len(data[path]), sha256=sha256(data[path]))
        acceptance = dict(acceptance, input_sha256=sha256(canonical_bytes(value)))
        with self.assertRaises(ValueError):
            inputs.admit(canonical_bytes(value), data.__getitem__, acceptance)

    def test_wrong_profile_target_refuses(self):
        value, data, *_, acceptance = matrix.family()
        path = value['descriptor']['path']; descriptor = json.loads(data[path])
        descriptor['release_policy']['target']['module_id'] = 'foreign'
        data[path] = canonical_bytes(descriptor)
        value['descriptor'].update(bytes=len(data[path]), sha256=sha256(data[path]))
        acceptance = dict(acceptance, input_sha256=sha256(canonical_bytes(value)))
        with self.assertRaisesRegex(ValueError, '^profile_target_identity$'):
            inputs.admit(canonical_bytes(value), data.__getitem__, acceptance)

    def test_policy_digest_mismatch_refuses(self):
        value, data, *_, acceptance = matrix.family()
        value['admission_policy']['policy_sha256'] = 'a' * 64
        acceptance = dict(acceptance, input_sha256=sha256(canonical_bytes(value)), policy_sha256='a' * 64)
        with self.assertRaisesRegex(ValueError, '^profile_policy_identity$'):
            inputs.admit(canonical_bytes(value), data.__getitem__, acceptance)

    def test_unaccepted_native_original_never_parses_or_reads(self):
        _, _, acceptance, admitted = self.native(); calls = []
        def reader(path): calls.append(path); raise AssertionError('reader called')
        with patch.object(owner_receipts, 'parse_build_receipt', wraps=owner_receipts.parse_build_receipt) as parser:
            with self.assertRaisesRegex(ValueError, '^native_receipt_not_independently_accepted$'):
                receipts.verify_shared_receipt(b'not-json', admitted, reader, reader, acceptance)
        self.assertEqual(parser.call_count, 0); self.assertEqual(calls, [])

    def test_digest_accepted_but_closure_unadmitted_never_parses(self):
        _, _, acceptance, admitted = self.native(); raw = b'not-json'
        acceptance = dict(acceptance, receipt_sha256=sha256(raw))
        with patch.object(owner_receipts, 'parse_build_receipt', wraps=owner_receipts.parse_build_receipt) as parser:
            with self.assertRaisesRegex(ValueError, '^inspector_profile_unqualified$'):
                receipts.verify_shared_receipt(raw, admitted, lambda _: b'', lambda _: b'', acceptance)
        self.assertEqual(parser.call_count, 0)
