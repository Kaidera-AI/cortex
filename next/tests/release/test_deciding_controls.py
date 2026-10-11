"""Bad producers maintain all unrelated custody; each asserted reason is causal."""
import unittest
from fixture import canonical, digest
from receipt_fixture import receipt_fixture
from test_unit1_red import product


class DecidingControls(unittest.TestCase):
    def verify(self, fixture, *, edit=None):
        value, inputs, receipt, outputs, evidence, policy = fixture
        input_api, receipt_api = product('inputs'), product('receipts')
        admitted = input_api.admit_build_input(input_api.parse_build_input(canonical(value)), inputs.__getitem__, policy)
        if edit: edit(receipt)
        return receipt_api.verify_build_receipt(receipt_api.parse_build_receipt(canonical(receipt)), admitted,
                                               outputs.__getitem__, evidence.__getitem__, policy)

    def test_independent_build_guard(self):
        for key in ('build_id','execution_id','store_id','runroot_id','container_id','source_id'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, '^independent_builds$'):
                self.verify(receipt_fixture(reuse_identity=key))

    def test_component_binding_guard(self):
        for key in ('source_sha256','recipe_sha256','dependency_sha256'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, '^build_input_components$'):
                self.verify(receipt_fixture(component_mutation=key))

    def test_raw_equality_guard(self):
        with self.assertRaisesRegex(ValueError, '^raw_equality_and_audit$'):
            self.verify(receipt_fixture(unequal_archive=True))

    def test_semantic_report_guard(self):
        with self.assertRaisesRegex(ValueError, '^semantic_report_binding$'):
            self.verify(receipt_fixture(bad_report=True))

    def test_exit_guard(self):
        with self.assertRaisesRegex(ValueError, '^operation_exit_or_budget$'):
            self.verify(receipt_fixture(bad_exit=True))

    def test_index_closure_guard(self):
        with self.assertRaisesRegex(ValueError, '^evidence_index_closure$'):
            self.verify(receipt_fixture(bad_index=True))

    def test_cleanup_guard(self):
        with self.assertRaisesRegex(ValueError, '^complete_cleanup$'):
            self.verify(receipt_fixture(), edit=lambda r:r['cleanup'].update(complete=False))

    def test_receipt_binding_guard(self):
        with self.assertRaisesRegex(ValueError, '^receipt_input_binding$'):
            self.verify(receipt_fixture(), edit=lambda r:r.update(input_sha256='f'*64))

    def test_exact_artifact_bytes_guard(self):
        api=product('artifacts');raw=b'actual blob';record={'path':'blob','bytes':len(raw),'sha256':digest({'foreign':True})}
        with self.assertRaisesRegex(ValueError, '^artifact_bytes_mismatch$'):
            api.measure(record,lambda path:raw,1024)


if __name__=='__main__': unittest.main(verbosity=2)
