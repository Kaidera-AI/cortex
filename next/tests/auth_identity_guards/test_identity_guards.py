"""Fresh real-effect guards for private SQL authority and adoption custody."""
import hashlib
import json
from pathlib import Path
import sys
from uuid import UUID

import psycopg
from cortex_core.auth import AuthError, authorized
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'auth_identity'))
import test_identity as frozen
from core_db_fixture import Fixture, WRITE_A, OWNER_A, uid


class IdentityGuards(Fixture):
    port = frozen.IdentityTests.port
    register = frozen.IdentityTests.register
    manifest = frozen.IdentityTests.manifest
    counts = frozen.IdentityTests.counts

    def test_actual_registration_commits_without_private_resource_privilege_failure(self):
        refusal = None
        try:
            result = self.register()
        except AuthError as error:
            refusal = error.code
        self.assertIsNone(refusal)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM auth.credentials WHERE id=%s', (result.credential_id,)).fetchone()[0], 1)

    def test_sql_registration_refuses_bound_writer_and_private_kind_selector(self):
        before = self.counts()
        args = (UUID(uid(70)), 'sql-agent', ['worker'], ['read'], UUID(uid(170)),
                hashlib.sha256(b'synthetic-sql-diagnostic').hexdigest(), 86400, 'sql-writer', 'f'*64)
        with authorized(self.request, WRITE_A, UUID(uid(1)), UUID(uid(3)), 'write'):
            with self.assertRaises(psycopg.errors.RaiseException) as failure:
                with self.request.transaction():
                    self.request.execute('SELECT auth.identity_register_agent(%s,%s,%s,%s,%s,%s,%s,%s,%s)', args)
            self.assertEqual(failure.exception.diag.message_primary, 'identity_forbidden')
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.request.transaction():
                    self.request.execute('SELECT auth.identity_register(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                                         ('human', *args[:7], 'forged attestation', *args[7:]))
        self.assertEqual(self.counts(), before)

    def test_fixed_agent_path_ignores_caller_kind_context(self):
        port = self.port()
        refusal = None
        try:
            with port._control() as scope:
                self.request.execute("SELECT set_config('cortex.principal_kind','human',true)")
                raw = port._register(scope, 'agent', UUID(uid(70)), 'sql-agent', ('worker',), ('owner',),
                    UUID(uid(170)), hashlib.sha256(b'synthetic-sql-diagnostic').hexdigest(),
                    86400, None, 'sql-agent', 'f'*64)
        except AuthError as error:
            refusal = error.code
        self.assertIsNone(refusal)
        self.assertEqual(port.lookup(UUID(uid(70))).kind, 'agent')

    def test_malformed_manifest_and_donor_inputs_always_have_typed_refusals(self):
        port = self.port(donor=True)
        values = []
        manifest = self.manifest(); manifest['agents'][0]['source_id'] = []; values.append((port, manifest))
        broken = json.dumps({'agents': [{'source_id': 'legacy-write-a', 'name': '10', 'role': 'cortex-chief'}]}).encode()
        pin = frozen.identity_api.DonorPin(frozen.DONOR_SHA, broken, released=True)
        broken_port = frozen.identity_api.Identity(self.request, OWNER_A, UUID(uid(1)), UUID(uid(3)), donor_pin=pin)
        manifest = self.manifest(); manifest['donor']['source_sha256'] = hashlib.sha256(broken).hexdigest()
        values.append((broken_port, manifest))
        for candidate, value in values:
            with self.subTest(input_kind='malformed'):
                error = None
                try:
                    candidate.dry_run_adoption(value)
                except Exception as caught:
                    error = caught
                self.assertIsInstance(error, AuthError)
                self.assertEqual(error.code, 'invalid_input')

    def test_sql_adoption_rechecks_private_installation_and_project_scope(self):
        port = self.port(donor=True)
        manifest = self.manifest(); manifest['project_id'] = uid(99)
        body = frozen.identity_api._canonical(manifest).decode()
        before = self.counts(); refusal = None
        try:
            with port._control():
                self.request.execute('SELECT auth.identity_adopt(%s,%s,%s,%s)',
                    (body, hashlib.sha256(body.encode()).hexdigest(), 'sql-wrong-project', 'f'*64))
        except AuthError as error:
            refusal = error.code
        self.assertEqual(refusal, 'scope_mismatch')
        self.assertEqual(self.counts(), before)

    def test_sql_adoption_digest_binds_exact_manifest_bytes(self):
        port = self.port(donor=True)
        body = frozen.identity_api._canonical(self.manifest()).decode()
        before = self.counts(); refusal = None
        try:
            with port._control():
                self.request.execute('SELECT auth.identity_adopt(%s,%s,%s,%s)',
                    (body, '0'*64, 'sql-wrong-digest', 'f'*64))
        except AuthError as error:
            refusal = error.code
        self.assertEqual(refusal, 'invalid_input')
        self.assertEqual(self.counts(), before)
