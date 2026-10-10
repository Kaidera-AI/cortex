"""Artifact and caller truth guards; these do not claim real adoption qualification."""
import hashlib
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

NEXT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(NEXT / 'scripts'))
try:
    consumer = importlib.import_module('mutate_identity')
except ModuleNotFoundError as error:
    if error.name != 'mutate_identity':
        raise
    consumer = None


class IdentityReceipts(unittest.TestCase):
    def receipt(self, code, failures=(), errors=()):
        return SimpleNamespace(returncode=code, stdout='CORTEX_TEST_RESULT='+json.dumps(
            {'tests_run':1,'failures':list(failures),'errors':list(errors)}), stderr='')

    def test_manifest_binds_exact_additive_identity_sql(self):
        manifest = json.loads((NEXT/'schema/manifest.json').read_bytes())
        rows = [row for row in manifest['migrations'] if row['id']=='auth-0003']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['path'], 'auth/003-identity.sql')
        self.assertEqual(rows[0]['sha256'], hashlib.sha256((NEXT/'schema/auth/003-identity.sql').read_bytes()).hexdigest())

    def test_identity_contract_keeps_authority_and_admission_boundaries(self):
        value = json.loads((NEXT/'contracts/identity-conformance.json').read_bytes())
        self.assertEqual(value['role_authority'], 'core_project_membership')
        self.assertFalse(value['roles_imply_permissions'])
        self.assertFalse(value['human_from_owner_permission'])
        self.assertFalse(value['plaintext_replay'])
        self.assertFalse(value['migration_or_boot_auto_adoption'])
        self.assertEqual(value['released_donor_adoption'], 'NOT_RUN_until_released_pin')
        self.assertEqual(value['live_issuer_adoption_installer_release'], 'HELD')
        self.assertEqual(value['legacy_registration_authority'],
            {'cortex-add-agent':'owner_or_admin','lead_registration_requires':'owner_or_admin'})

    def test_caller_refuses_signals_fixtures_wrong_body_and_operational_errors(self):
        self.assertIsNotNone(consumer, 'missing C04a mutation caller')
        body = {'id':'expected','traceback':'actual synthetic classifier probe','phase':'test','is_assertion':True}
        for result in (self.receipt(137,[body]), self.receipt(1,[dict(body,phase='fixture')]),
                       self.receipt(1,[dict(body,id='other')]), self.receipt(1,[body],[{'id':'error','traceback':'error'}]),
                       self.receipt(1,[dict(body,is_assertion=False)])):
            with self.subTest(probe='non-body-or-error'):
                self.assertEqual(consumer.mutation_status(result,{'expected'}), 'inconclusive')

    def test_caller_requires_declared_body_and_clean_success(self):
        self.assertIsNotNone(consumer, 'missing C04a mutation caller')
        body = {'id':'expected','traceback':'actual synthetic classifier probe','phase':'test','is_assertion':True}
        self.assertEqual(consumer.mutation_status(self.receipt(1,[body]),{'expected'}), 'killed')
        self.assertEqual(consumer.mutation_status(self.receipt(0),{'expected'}), 'survived')
