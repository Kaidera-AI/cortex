"""Frozen C04a identity/issuer/adoption contract, real disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch
from uuid import UUID

import psycopg
from cortex_core.auth import AuthError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core_db_fixture import Fixture, API, REQUEST, READ_A, WRITE_A, OWNER_A, READ_B, uid, auth_fixture

try:
    identity_api = importlib.import_module('cortex_core.identity')
except ModuleNotFoundError as error:
    if error.name != 'cortex_core.identity':
        raise
    identity_api = None

CONTROL_A = auth_fixture.CONTROL_A

NEXT = Path(__file__).resolve().parents[2]
DONOR = json.dumps({'project': 'fixture-helix', 'agents': [
    {'source_id': 'legacy-write-a', 'name': '10', 'role': 'cortex-chief',
     'writer_scope': 'project', 'display_name': 'synthetic fixture'}]}, sort_keys=True).encode()
DONOR_SHA = 'a' * 40  # synthetic released-pin model, never a real released adoption claim


class IdentityTests(Fixture):
    def port(self, key=OWNER_A, connection=None, installation=1, project=3, donor=False):
        self.assertIsNotNone(identity_api, 'missing Core identity port')
        self.assertTrue(hasattr(identity_api, 'Identity'), 'missing Core identity class')
        pin = identity_api.DonorPin(DONOR_SHA, DONOR, released=True) if donor else None
        return identity_api.Identity(connection or self.request, key, UUID(uid(installation)),
                                     UUID(uid(project)), donor_pin=pin)

    def register(self, port=None, principal=70, name='fixture-agent', roles=('worker',),
                 permissions=('read', 'write'), request_key='register', ttl_seconds=86400):
        return (port or self.port()).register_agent(UUID(uid(principal)), name, roles,
            permissions, request_key=request_key, ttl_seconds=ttl_seconds)

    def manifest(self):
        return {'version': 1, 'installation_id': uid(1), 'project_id': uid(3),
            'donor': {'released_sha': DONOR_SHA, 'source_sha256': hashlib.sha256(DONOR).hexdigest()},
            'legacy_registration_authority': {'cortex-add-agent': 'owner_or_admin',
                                             'lead_registration_requires': 'owner_or_admin'},
            'agents': [{'source_project': 'fixture-helix', 'source_id': 'legacy-write-a',
                'principal_id': uid(10), 'name': '10', 'kind': 'agent',
                'roles': ['cortex-chief'], 'permissions': ['read', 'write']}]}

    def counts(self):
        return tuple(self.admin.execute('SELECT count(*) FROM ' + table).fetchone()[0]
            for table in ('auth.principals', 'auth.project_grants', 'auth.credentials',
                          'core.payloads', 'coordination.idempotency'))

    def test_additive_kind_roles_defaults_private_memberships_and_frozen_table_count(self):
        self.port()
        row = self.admin.execute('SELECT kind,identity_adopted FROM auth.principals WHERE tenant_id=%s AND id=%s',
                                 (uid(2), uid(10))).fetchone()
        self.assertEqual(row, ('agent', False))
        self.assertEqual(self.admin.execute('SELECT roles FROM auth.project_grants WHERE tenant_id=%s AND principal_id=%s',
                                           (uid(2), uid(10))).fetchone()[0], [])
        count = self.admin.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('core','auth','coordination','retrieval') AND c.relkind='r' AND c.relname<>'schema_migrations'").fetchone()[0]
        self.assertEqual(count, 41)
        self.assertEqual({f'{schema}.{table}' for schema,table in self.admin.execute("SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('core','auth','coordination','retrieval') AND c.relkind='r' AND c.relname<>'schema_migrations'").fetchall()} - {'coordination.session_sources', 'retrieval.search_state', 'retrieval.search_sources', 'retrieval.search_vectors', 'retrieval.query_embeddings', 'retrieval.graph_generations', 'retrieval.graph_state', 'retrieval.graph_applied', 'retrieval.graph_nodes', 'retrieval.graph_edges'}, {'auth.credentials', 'auth.permission_generations', 'auth.principals', 'auth.project_grants', 'coordination.consumer_aggregate_heads', 'coordination.consumer_checkpoints', 'coordination.consumer_event_outcomes', 'coordination.consumer_project_checkpoints', 'coordination.feed_state', 'coordination.idempotency', 'coordination.job_attempts', 'coordination.job_results', 'coordination.jobs', 'coordination.leases', 'coordination.outbox', 'coordination.published_events', 'coordination.quarantine', 'coordination.snapshot_floors', 'core.analytics_facts', 'core.blob_manifests', 'core.extraction_facts', 'core.installations', 'core.payloads', 'core.projects', 'core.record_aliases', 'core.record_revisions', 'core.records', 'core.tenants', 'retrieval.embedding_models', 'retrieval.embeddings', 'retrieval.generations'})
        self.assertIsNotNone(self.admin.execute("SELECT to_regclass('auth.project_role_memberships')").fetchone()[0])

    def test_owner_registers_stable_agent_roles_separate_permissions_and_digest_only(self):
        result = self.register(roles=('worker', 'code-reviewer'), permissions=('read',))
        self.assertEqual(result.principal_id, UUID(uid(70)))
        self.assertIsInstance(result.secret, bytes)
        self.assertGreaterEqual(len(result.secret), 43)
        result.secret.decode('ascii')
        self.assertNotIn(result.secret.decode(), repr(result))
        row = self.admin.execute('SELECT principal_id::text,key_digest,expires_at>clock_timestamp() FROM auth.credentials WHERE tenant_id=%s AND id=%s',
                                 (uid(2), result.credential_id)).fetchone()
        self.assertEqual(row, (uid(70), hashlib.sha256(result.secret).hexdigest(), True))
        actor = self.port(result.secret).lookup(UUID(uid(70)))
        self.assertEqual((actor.kind, actor.roles, actor.adopted), ('agent', ('code-reviewer', 'worker'), True))
        self.assertEqual(self.admin.execute('SELECT permissions FROM auth.project_grants WHERE tenant_id=%s AND principal_id=%s',
                                           (uid(2), uid(70))).fetchone()[0], ['read'])

    def test_read_write_control_only_and_wrong_scope_cannot_register(self):
        self.port()
        before = self.counts()
        for key in (READ_A, WRITE_A, CONTROL_A):
            with self.subTest(key_class='nonowner'), self.assertRaises(AuthError):
                self.register(self.port(key))
        for args in ({'installation': 20}, {'project': 99}):
            with self.subTest(scope=args), self.assertRaises(AuthError):
                self.register(self.port(**args))
        self.assertEqual(self.counts(), before)

    def test_fixed_human_registration_requires_owner_attestation(self):
        port = self.port()
        with self.assertRaises(AuthError):
            port.register_human(UUID(uid(71)), 'fixture-human', ('reviewer',), ('owner',),
                                owner_attestation='', request_key='human-empty')
        with self.assertRaises(AuthError):
            self.port(WRITE_A).register_human(UUID(uid(71)), 'fixture-human', ('reviewer',), ('owner',),
                                            owner_attestation='owner checked person', request_key='human-writer')
        result = port.register_human(UUID(uid(71)), 'fixture-human', ('reviewer',), ('owner',),
                                    owner_attestation='owner checked person', request_key='human')
        human = self.port(result.secret).lookup(UUID(uid(71)))
        self.assertEqual((human.kind, human.adopted), ('human', True))

    def test_unadopted_existing_principals_have_no_role_or_human_authority(self):
        self.assertIsNone(self.port(WRITE_A).lookup(UUID(uid(10))))
        self.assertIsNone(self.port().lookup(UUID(uid(12))))
        self.assertEqual(self.admin.execute('SELECT identity_adopted FROM auth.principals WHERE id=%s',
                                           (uid(10),)).fetchone()[0], False)

    def test_owner_permission_never_infers_human_and_roles_never_infer_permission(self):
        result = self.register(roles=('owner', 'human'), permissions=('read',))
        self.assertEqual(self.port(result.secret).lookup(UUID(uid(70))).kind, 'agent')
        with self.assertRaises(AuthError):
            self.register(self.port(result.secret), principal=71, name='unauthorized-second', request_key='second')
        owner = self.register(principal=72, name='owner-agent', roles=('worker',),
                              permissions=('owner',), request_key='owner-agent')
        self.assertEqual(self.port(owner.secret).lookup(UUID(uid(72))).kind, 'agent')

    def test_roles_are_normalized_bounded_and_sql_constraints_refuse_duplicates(self):
        result = self.register(roles=(' Worker ', 'worker', 'CODE-REVIEWER'))
        self.assertEqual(self.port(result.secret).lookup(UUID(uid(70))).roles, ('code-reviewer', 'worker'))
        before = self.counts()
        for roles in (('bad\nrole',), ('x' * 65,), tuple('r' + str(n) for n in range(33)), (True,)):
            with self.subTest(roles_kind='invalid'), self.assertRaises(AuthError):
                self.register(principal=71, name='invalid', roles=roles, request_key='invalid')
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.admin.execute("UPDATE auth.project_grants SET roles=ARRAY['worker','worker'] WHERE principal_id=%s", (uid(70),))
        self.assertEqual(self.counts(), before)

    def test_role_change_increments_generation_and_lookup_is_fresh(self):
        result = self.register()
        port = self.port(result.secret)
        before = port.lookup(UUID(uid(70)))
        self.port().set_roles(UUID(uid(70)), ('reviewer',), request_key='roles')
        after = port.lookup(UUID(uid(70)))
        self.assertGreater(after.generation, before.generation)
        self.assertEqual(after.roles, ('reviewer',))
        with self.assertRaises(AuthError):
            port.set_roles(UUID(uid(70)), ('owner',), request_key='self-promote')

    def test_cross_tenant_missing_disabled_and_cross_project_identity_is_hidden(self):
        result = self.register()
        self.assertIsNone(self.port(READ_B, installation=20).lookup(UUID(uid(70))))
        self.assertIsNone(self.port().lookup(UUID(uid(99))))
        self.admin.execute('UPDATE auth.principals SET disabled=true WHERE tenant_id=%s AND id=%s', (uid(2), uid(70)))
        self.assertIsNone(self.port().lookup(UUID(uid(70))))
        with self.assertRaises(AuthError):
            self.port(result.secret, project=99).lookup(UUID(uid(70)))

    def test_expired_revoked_and_disabled_credentials_cannot_lookup(self):
        self.port()
        for column, expression in [('expires_at', "clock_timestamp()-interval '1 second'"),
                                   ('revoked_at', 'clock_timestamp()')]:
            result = self.register(principal=70 if column == 'expires_at' else 71, name=column, request_key=column)
            self.admin.execute('UPDATE auth.credentials SET ' + column + '=' + expression + ' WHERE id=%s', (result.credential_id,))
            with self.assertRaises(AuthError):
                self.port(result.secret).lookup(result.principal_id)

    def test_concurrent_duplicate_registration_has_one_durable_outcome(self):
        self.port()
        gate = threading.Barrier(2)
        def register(key):
            with psycopg.connect(os.environ['TEST_DATABASE_URL'].replace('postgres@', API+'@'), autocommit=True) as c:
                c.execute(f'SET ROLE "{REQUEST}"')
                gate.wait(timeout=10)
                try:
                    return self.register(self.port(connection=c), request_key=key)
                except AuthError as error:
                    return error
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(register, ('concurrent-a', 'concurrent-b')))
        self.assertEqual(sum(not isinstance(x, AuthError) for x in results), 1)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM auth.principals WHERE id=%s', (uid(70),)).fetchone()[0], 1)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM auth.credentials WHERE principal_id=%s', (uid(70),)).fetchone()[0], 1)

    def test_exception_after_actual_registration_rolls_back_identity_credentials_audit_and_receipt(self):
        port = self.port()
        before = self.counts()
        original = identity_api.Identity._register
        def fail(instance, *args, **kwargs):
            result = original(instance, *args, **kwargs)
            self.assertEqual(self.request.execute('SELECT 1').fetchone()[0], 1)
            raise RuntimeError('synthetic post-registration fault')
        with patch.object(identity_api.Identity, '_register', fail), self.assertRaisesRegex(RuntimeError, 'post-registration'):
            self.register(port)
        self.assertEqual(self.counts(), before)

    def test_registration_replay_returns_no_plaintext_and_changed_request_refuses(self):
        first = self.register()
        before = self.counts()
        again = self.register()
        self.assertEqual((again.principal_id, again.credential_id), (first.principal_id, first.credential_id))
        self.assertIsNone(again.secret)
        self.assertTrue(again.replayed)
        self.assertEqual(self.counts(), before)
        with self.assertRaises(AuthError):
            self.register(name='changed')
        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s', (hashlib.sha256(OWNER_A).hexdigest(),))
        with self.assertRaises(AuthError):
            self.register()

    def test_plaintext_never_enters_payload_receipt_or_sql_credential_storage(self):
        result = self.register()
        bodies = [bytes(row[0]) for row in self.admin.execute('SELECT body FROM core.payloads').fetchall()]
        receipts = [row[0].encode() for row in self.admin.execute('SELECT receipt::text FROM coordination.idempotency').fetchall()]
        self.assertTrue(bodies)
        self.assertTrue(receipts)
        self.assertTrue(all(result.secret not in value for value in bodies + receipts))
        self.assertNotIn(result.secret.decode(), repr(result))
        self.assertFalse(self.admin.execute("SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='auth' AND table_name='credentials' AND column_name IN ('secret','plaintext','key'))").fetchone()[0])

    def test_rotation_revokes_old_key_and_replay_never_redelivers_new_secret(self):
        old = self.register()
        port = self.port()
        new = port.rotate(old.principal_id, old.credential_id, request_key='rotate')
        self.assertNotEqual(new.secret, old.secret)
        with self.assertRaises(AuthError):
            self.port(old.secret).lookup(old.principal_id)
        self.assertEqual(self.port(new.secret).lookup(new.principal_id).kind, 'agent')
        again = port.rotate(old.principal_id, old.credential_id, request_key='rotate')
        self.assertIsNone(again.secret)
        self.assertEqual(again.credential_id, new.credential_id)

    def test_revoke_requires_owner_and_immediately_refuses_the_issued_key(self):
        issued = self.register()
        with self.assertRaises(AuthError):
            self.port(issued.secret).revoke(issued.credential_id, request_key='self-revoke')
        self.port().revoke(issued.credential_id, request_key='revoke')
        with self.assertRaises(AuthError):
            self.port(issued.secret).lookup(issued.principal_id)

    def test_project_owner_cannot_rotate_a_multi_project_principal_key(self):
        issued = self.register()
        self.admin.execute("INSERT INTO core.projects(tenant_id,id,name) VALUES (%s,%s,'second-owner-domain')", (uid(2), uid(90)))
        self.admin.execute('INSERT INTO auth.project_grants(tenant_id,project_id,principal_id,permissions) VALUES (%s,%s,%s,%s)',
                           (uid(2), uid(90), issued.principal_id, ['read']))
        before = self.counts()
        with self.assertRaises(AuthError):
            self.port().rotate(issued.principal_id, issued.credential_id, request_key='cross-owner-rotate')
        self.assertEqual(self.counts(), before)
        self.assertIsNone(self.admin.execute('SELECT revoked_at FROM auth.credentials WHERE id=%s', (issued.credential_id,)).fetchone()[0])

    def test_missing_or_unreleased_donor_pin_is_not_run_without_side_effects(self):
        port = self.port()
        before = self.counts()
        self.assertEqual(port.dry_run_adoption(self.manifest()).status, 'not_run')
        with self.assertRaises(AuthError):
            port.apply_adoption(self.manifest(), approved_sha256='0'*64, request_key='missing-pin')
        pin = identity_api.DonorPin(DONOR_SHA, DONOR, released=False)
        port = identity_api.Identity(self.request, OWNER_A, UUID(uid(1)), UUID(uid(3)), donor_pin=pin)
        self.assertEqual(port.dry_run_adoption(self.manifest()).status, 'not_run')
        self.assertEqual(self.counts(), before)

    def test_owner_dry_run_has_exact_mapping_hash_and_no_writes(self):
        port = self.port(donor=True)
        before = self.counts()
        preview = port.dry_run_adoption(self.manifest())
        self.assertEqual((preview.status, preview.count), ('ready', 1))
        self.assertEqual(preview.mapping[0]['principal_id'], uid(10))
        self.assertEqual(len(preview.manifest_sha256), 64)
        self.assertEqual(self.counts(), before)
        with self.assertRaises(AuthError):
            self.port(WRITE_A, donor=True).dry_run_adoption(self.manifest())

    def test_explicit_hash_bound_adoption_preserves_existing_key_and_records_immutable_audit(self):
        port = self.port(donor=True)
        manifest = self.manifest()
        preview = port.dry_run_adoption(manifest)
        credential = self.admin.execute('SELECT id::text,key_digest FROM auth.credentials WHERE principal_id=%s', (uid(10),)).fetchall()
        result = port.apply_adoption(manifest, approved_sha256=preview.manifest_sha256, request_key='adopt')
        self.assertEqual((result.count, result.manifest_sha256), (1, preview.manifest_sha256))
        self.assertEqual(self.admin.execute('SELECT id::text,key_digest FROM auth.credentials WHERE principal_id=%s', (uid(10),)).fetchall(), credential)
        actor = self.port(WRITE_A).lookup(UUID(uid(10)))
        self.assertEqual((actor.kind, actor.roles, actor.adopted), ('agent', ('cortex-chief',), True))
        audit = bytes(self.admin.execute('SELECT body FROM core.payloads WHERE id=%s', (result.audit_id,)).fetchone()[0])
        self.assertEqual(json.loads(audit)['manifest_sha256'], preview.manifest_sha256)
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            self.admin.execute('UPDATE core.payloads SET body=body WHERE id=%s', (result.audit_id,))

    def test_changed_manifest_duplicates_ambiguous_ids_and_human_conversion_refuse(self):
        port = self.port(donor=True)
        original = self.manifest()
        preview = port.dry_run_adoption(original)
        before = self.counts()
        changed = self.manifest(); changed['agents'][0]['permissions'] = ['owner']
        with self.assertRaises(AuthError):
            port.apply_adoption(changed, approved_sha256=preview.manifest_sha256, request_key='changed-manifest')
        for mutation in ('duplicate', 'human', 'source', 'name', 'uuid'):
            manifest = self.manifest()
            if mutation == 'duplicate': manifest['agents'] *= 2
            elif mutation == 'human': manifest['agents'][0]['kind'] = 'human'
            elif mutation == 'source': manifest['agents'][0]['source_id'] = 'not-in-donor'
            elif mutation == 'name': manifest['agents'][0]['name'] = 'different-donor-name'
            else: manifest['agents'][0]['principal_id'] = uid(12)
            with self.subTest(mutation=mutation), self.assertRaises(AuthError):
                port.dry_run_adoption(manifest)
        self.assertEqual(self.counts(), before)

    def test_manifest_donor_and_scope_hashes_cannot_be_substituted(self):
        port = self.port(donor=True)
        for field in ('released_sha', 'source_sha256'):
            manifest = self.manifest(); manifest['donor'][field] = '0' * len(manifest['donor'][field])
            with self.subTest(field=field), self.assertRaises(AuthError): port.dry_run_adoption(manifest)
        manifest = self.manifest(); manifest['project_id'] = uid(99)
        with self.assertRaises(AuthError): port.dry_run_adoption(manifest)

    def test_adoption_retry_replays_one_mapping_and_audit_without_automatic_enrollment(self):
        port = self.port(donor=True)
        manifest = self.manifest(); preview = port.dry_run_adoption(manifest)
        first = port.apply_adoption(manifest, approved_sha256=preview.manifest_sha256, request_key='adopt')
        before = self.counts()
        again = port.apply_adoption(manifest, approved_sha256=preview.manifest_sha256, request_key='adopt')
        self.assertEqual((again.audit_id, again.count), (first.audit_id, 1))
        self.assertTrue(again.replayed)
        self.assertEqual(self.counts(), before)
        self.assertIsNone(port.lookup(UUID(uid(12))))

    def test_legacy_add_agent_maps_kind_and_role_but_tightens_lead_to_owner_admin(self):
        port = self.port()
        result = port.legacy_add_agent('fixture-lead', 'lead', principal_id=UUID(uid(70)),
                                      permissions=('read', 'write'), request_key='legacy-lead')
        actor = self.port(result.secret).lookup(result.principal_id)
        self.assertEqual((actor.kind, actor.roles), ('agent', ('lead',)))
        with self.assertRaises(AuthError):
            self.port(result.secret).legacy_add_agent('self-register', 'worker', principal_id=UUID(uid(71)),
                permissions=('read',), request_key='lead-register')
        fixture = json.loads((NEXT/'contracts/identity-conformance.json').read_text())
        self.assertEqual(fixture['legacy_registration_authority'], {'cortex-add-agent': 'owner_or_admin',
                                                                  'lead_registration_requires': 'owner_or_admin'})
        self.assertFalse(fixture['human_from_owner_permission'])
        self.assertFalse(fixture['migration_or_boot_auto_adoption'])

    def test_private_lookup_mutators_view_and_public_acl_cannot_bypass_binding(self):
        port = self.port()
        self.register(port)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.request.execute('SELECT * FROM auth.project_role_memberships')
        self.assertEqual(self.request.execute('SELECT * FROM auth.identity_lookup(%s)', (UUID(uid(70)),)).fetchall(), [])
        functions = self.admin.execute("SELECT proname,prosecdef,proconfig,pg_get_userbyid(proowner),has_function_privilege('public',oid,'EXECUTE') FROM pg_proc WHERE pronamespace='auth'::regnamespace AND proname LIKE 'identity_%' AND prosecdef").fetchall()
        self.assertGreaterEqual(len(functions), 5)
        for name, definer, config, owner, public in functions:
            with self.subTest(function=name):
                self.assertTrue(definer)
                self.assertTrue(any(value.startswith('search_path=pg_catalog, auth, core') for value in config))
                self.assertEqual(owner, 'kaidera-runtime-core-verifier')
                self.assertFalse(public)
        for sql in ('SELECT * FROM auth.credentials', 'UPDATE auth.principals SET kind=\'human\''):
            with self.assertRaises(psycopg.errors.InsufficientPrivilege): self.request.execute(sql)

    def test_owner_expiry_after_actual_registration_rolls_back_before_plaintext_delivery(self):
        port = self.port()
        self.admin.execute("UPDATE auth.credentials SET expires_at=clock_timestamp()+interval '1 second' WHERE key_digest=%s", (hashlib.sha256(OWNER_A).hexdigest(),))
        before = self.counts()
        original = identity_api.Identity._register
        def delayed(instance, *args, **kwargs):
            result = original(instance, *args, **kwargs)
            self.request.execute('SELECT pg_sleep(1.2)')
            return result
        with patch.object(identity_api.Identity, '_register', delayed), self.assertRaises(AuthError):
            self.register(port)
        self.assertEqual(self.counts(), before)
