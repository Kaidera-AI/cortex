import copy
import unittest
from fixture import canonical, digest
from receipt_fixture import receipt_fixture
from test_unit1_red import product


class BuildReceipt(unittest.TestCase):
    def setup_contract(self):
        module = product('receipts')
        self.assertIsNotNone(module, 'product receipt verifier absent')
        value, inputs, receipt, outputs, evidence, policy = receipt_fixture()
        admission = product('inputs')
        admitted = admission.admit_build_input(admission.parse_build_input(canonical(value)), inputs.__getitem__, policy)
        return module, admitted, receipt, outputs, evidence, policy

    def verify(self, module, admitted, receipt, outputs, evidence, policy):
        return module.verify_build_receipt(module.parse_build_receipt(canonical(receipt)), admitted, outputs.__getitem__, evidence.__getitem__, policy)

    def test_actual_oci_and_evidence_positive_is_immutable_fixture(self):
        m, admitted, receipt, outputs, evidence, policy = self.setup_contract()
        verified = self.verify(m, admitted, receipt, outputs, evidence, policy)
        self.assertEqual(verified.receipt_sha256, digest(receipt))
        self.assertEqual(verified.input_sha256, admitted.input_sha256)
        self.assertEqual(verified.output_sha256, receipt['comparison']['raw_archive_sha256'])
        self.assertFalse(verified.native_qualified)
        with self.assertRaises(TypeError): verified.target['module_id'] = 'other'

    def test_misbound_input_target_source_recipe_policy_and_native_refuse(self):
        m, admitted, receipt, outputs, evidence, policy = self.setup_contract()
        for change in ('input', 'target', 'source', 'recipe', 'policy', 'native'):
            bad = copy.deepcopy(receipt)
            if change == 'input': bad['input_sha256'] = 'f'*64
            elif change == 'target': bad['target']['module_id'] = 'other'
            elif change in ('source','recipe'): bad['builds']['B'][change+'_sha256'] = 'f'*64
            elif change == 'policy': bad['audit']['policy_sha256'] = 'f'*64
            else: bad['qualification_scope'] = 'native'
            with self.subTest(change=change), self.assertRaises(ValueError): self.verify(m, admitted, bad, outputs, evidence, policy)

    def test_reused_builder_store_source_or_missing_exit_refuse(self):
        m, admitted, receipt, outputs, evidence, policy = self.setup_contract()
        for change in ('build_id','execution_id','store_id','runroot_id','container_id','source_id','exit'):
            bad = copy.deepcopy(receipt)
            if change == 'exit': bad['builds']['B']['operations'][2].pop('exit_code')
            else: bad['builds']['B']['observed_builder'][change] = bad['builds']['A']['observed_builder'][change]
            with self.subTest(change=change), self.assertRaises(ValueError): self.verify(m, admitted, bad, outputs, evidence, policy)

    def test_archive_size_media_oci_and_actual_bytes_refuse(self):
        m, admitted, receipt, outputs, evidence, policy = self.setup_contract()
        for change in ('bytes', 'size', 'media', 'oci'):
            bad, actual = copy.deepcopy(receipt), dict(outputs)
            archive = next(r for r in bad['builds']['B']['outputs'] if r['role']=='archive')
            if change == 'bytes': actual[archive['path']] += b'foreign'
            elif change == 'size': archive['bytes'] += 1
            elif change == 'media': archive['media_type'] = 'application/json'
            else: bad['builds']['B']['oci']['config'] = 'f'*64
            with self.subTest(change=change), self.assertRaises(ValueError): self.verify(m, admitted, bad, actual, evidence, policy)

    def test_missing_audit_index_custody_cleanup_and_remaining_resources_refuse(self):
        m, admitted, receipt, outputs, evidence, policy = self.setup_contract()
        for change in ('audit', 'index', 'custody', 'cleanup', 'remaining', 'raw'):
            bad, actual = copy.deepcopy(receipt), dict(evidence)
            if change == 'audit': bad['audit']['reports']['B'].pop('cache')
            elif change == 'index': actual.pop(bad['evidence']['index']['path'])
            elif change == 'custody': bad['custody']['complete'] = False
            elif change == 'cleanup': bad['cleanup']['complete'] = False
            elif change == 'remaining': bad['cleanup']['remaining_owned_resources'] = ['fixture-B-store_id']
            else: actual.pop(bad['builds']['A']['operations'][2]['evidence']['path'])
            with self.subTest(change=change), self.assertRaises(ValueError): self.verify(m, admitted, bad, outputs, actual, policy)


if __name__ == '__main__': unittest.main(verbosity=2)
