"""C01 I0 behavior checks; unchanged between RED and GREEN."""
import copy
import json
from pathlib import Path
import sys
import unittest

NEXT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(NEXT / "src"))
from cortex_core.contracts import (  # noqa: E402
    ContractError, validate_event, validate_wire, validate_openapi,
)


def fixture(name):
    return json.loads((NEXT / "contracts" / "fixtures" / name).read_text())


class EventContract(unittest.TestCase):
    def test_upsert_and_delete_are_valid(self):
        for name in ["event-upsert.json", "event-delete.json"]:
            value = fixture(name)
            original = copy.deepcopy(value)
            self.assertEqual(validate_event(value), original)
            self.assertEqual(value, original)

    def test_all_required_fields_refuse_omission(self):
        event = fixture("event-upsert.json")
        for key in event:
            with self.subTest(key=key):
                value = dict(event)
                del value[key]
                with self.assertRaises(ContractError):
                    validate_event(value)

    def test_scope_and_object_identifiers_are_canonical_uuids(self):
        for key in ["event_id", "installation_id", "tenant_id", "project_id", "aggregate_id", "payload_ref"]:
            for bad in ["", "other-project", "../secret", None, 7, "00000000000000000000000000000000"]:
                with self.subTest(key=key, bad=bad):
                    value = fixture("event-upsert.json")
                    value[key] = bad
                    with self.assertRaises(ContractError):
                        validate_event(value)

    def test_revision_positive_and_schema_supported(self):
        for key, values in [("aggregate_revision", [0, -1, True, "1", 1.5]), ("schema_version", [0, 2, True, "1"])]:
            for bad in values:
                with self.subTest(key=key, bad=bad):
                    value = fixture("event-upsert.json")
                    value[key] = bad
                    with self.assertRaises(ContractError):
                        validate_event(value)

    def test_delete_and_tombstone_agree_in_both_directions(self):
        for operation, tombstone in [("delete", False), ("upsert", True), ("unknown", False), ("upsert", "false")]:
            value = fixture("event-upsert.json")
            value.update(operation=operation, tombstone=tombstone)
            with self.assertRaises(ContractError):
                validate_event(value)

    def test_unknown_fields_refused(self):
        value = fixture("event-upsert.json")
        value["published_cursor"] = "999"
        with self.assertRaises(ContractError):
            validate_event(value)

    def test_payload_identity_kind_digest_and_timestamp_checked(self):
        for key, bad in [("aggregate_kind", ""), ("aggregate_kind", "shell command!"), ("payload_sha256", "a" * 63), ("payload_sha256", "G" * 64), ("occurred_at", "yesterday")]:
            value = fixture("event-upsert.json")
            value[key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(ContractError):
                validate_event(value)

    def test_bad_input_is_contract_error(self):
        for value in [None, [], "event", 5]:
            with self.assertRaises(ContractError):
                validate_event(value)


class WireContract(unittest.TestCase):
    def test_minimal_valid_wire_fixtures(self):
        for schema, name in [("ProposedWriteReceipt", "write-committed.json"), ("ProposedCapabilityState", "capability-ready.json"), ("ProposedError", "capability-unavailable.json"), ("ProposedError", "core-unavailable.json")]:
            value = fixture(name)
            self.assertEqual(validate_wire(schema, value), value)

    def test_queued_delivery_is_not_a_commit_receipt(self):
        value = fixture("write-committed.json")
        value["durability"] = "queued"
        with self.assertRaises(ContractError):
            validate_wire("ProposedWriteReceipt", value)

    def test_write_receipt_revision_bounds_and_projection(self):
        for key, bad in [("aggregate_version", 0), ("event_id", "fake"), ("projection", "complete-ish")]:
            value = fixture("write-committed.json")
            value[key] = bad
            with self.assertRaises(ContractError):
                validate_wire("ProposedWriteReceipt", value)

    def test_capability_state_and_error_codes_are_typed(self):
        for schema, name, key, bad in [("ProposedCapabilityState", "capability-ready.json", "state", "green"), ("ProposedError", "core-unavailable.json", "code", "all good"), ("ProposedError", "core-unavailable.json", "retryable", "yes")]:
            value = fixture(name)
            value[key] = bad
            with self.assertRaises(ContractError):
                validate_wire(schema, value)

    def test_unknown_schema_refused(self):
        with self.assertRaises(ContractError):
            validate_wire("../../../secret", {})


class OpenAPIContract(unittest.TestCase):
    def document(self):
        return json.loads((NEXT / "contracts" / "openapi.json").read_text())

    def test_inventory_and_references(self):
        self.assertEqual(validate_openapi(self.document()), 129)

    def test_missing_operation_refused(self):
        value = self.document()
        del value["paths"]["/memory"]["post"]
        with self.assertRaises(ContractError):
            validate_openapi(value)

    def test_duplicate_operation_refused(self):
        value = self.document()
        value["paths"]["/memory"]["post"]["operationId"] = value["paths"]["/search"]["post"]["operationId"]
        with self.assertRaises(ContractError):
            validate_openapi(value)

    def test_unowned_route_refused(self):
        value = self.document()
        value["paths"]["/memory"]["post"]["x-c01-disposition"] = "ignore"
        with self.assertRaises(ContractError):
            validate_openapi(value)

    def test_broken_reference_refused(self):
        value = self.document()
        value["components"]["schemas"]["broken"] = {"$ref": "#/missing"}
        with self.assertRaises(ContractError):
            validate_openapi(value)

    def test_c02_gaps_cannot_be_removed(self):
        value = self.document()
        del value["paths"]["/memory"]["post"]["x-c02-freeze-gaps"]
        with self.assertRaises(ContractError):
            validate_openapi(value)



# J3 assertions describe the accepted wire contract, never installed enrollment.
class ProjectEnrollmentContract(unittest.TestCase):
    def vocabulary(self):
        text = (NEXT / 'src/cortex_core/compat/contract.md').read_text()
        self.assertEqual(text.count('```json\n'), 1)
        document = json.loads(text.split('```json\n', 1)[1].split('\n```', 1)[0])
        self.assertIn('project_enrollment', document,
                      'accepted J3 request/auth/receipt vocabulary is absent')
        return document['project_enrollment']

    def validator(self, name):
        from jsonschema import Draft202012Validator, FormatChecker
        schema = self.vocabulary()['schemas'][name]
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, format_checker=FormatChecker())

    def prepared(self):
        return dict(principal_id='11111111-1111-4111-8111-111111111111',
                    credential_id='22222222-2222-4222-8222-222222222222',
                    credential_sha256='a' * 64, name='new-worker')

    def receipt(self, *, lead=False, revoked=False):
        uuid = '11111111-1111-4111-8111-111111111111'
        return {'receipt': dict(schema='cortex.enrollment.receipt.v1',
            operation='revoke' if revoked else 'claim' if lead else 'register',
            installation_id=uuid, tenant_id=uuid, project_id=uuid,
            principal_id=uuid, credential_id=uuid, operation_key=uuid,
            intent_sha256='b' * 64, credential_sha256='a' * 64,
            audit_id=uuid, event_id=uuid, replayed=False, revoked=revoked,
            name=uuid + ':new-worker', generation=1,
            permissions=['read', 'write', 'control'] if lead else ['read'],
            expires_at=None if lead else '2026-10-20T01:00:00Z')}

    def test_j3_one_pending_vocabulary_and_exact_operations(self):
        j = self.vocabulary()
        self.assertEqual(j['version'], 'cortex.project-enrollment.v1')
        self.assertEqual(j['implementation'], 'pending_finn_j2_j3')
        self.assertFalse(j['runtime_reads_this_map'])
        self.assertEqual({key: (v['method'], v['path'], v['route_id'])
                          for key, v in j['operations'].items()}, {
            'claim': ('POST', '/enrollment/claim', 'J3-CLAIM'),
            'register': ('POST', '/agents', 'C01-R108'),
            'rotate': ('POST', '/agents/{principal_id}/credentials/rotate', 'J3-ROTATE'),
            'revoke': ('POST', '/agents/{principal_id}/credentials/{credential_id}/revoke', 'J3-REVOKE')})
        self.assertTrue(all(v['success_status'] == 200 and v['current_bound'] is False
                            for v in j['operations'].values()))
        self.assertEqual(j['public_policy'], 'unchanged D1/D2/D3; no J3 public operations')

    def test_j3_finite_claim_is_not_ordinary_or_header_authority(self):
        j = self.vocabulary()
        self.assertEqual(j['auth']['claim'], 'finite J2 project-claim Bearer capability only')
        self.assertEqual(j['auth']['ordinary'], 'current project lead C04 control; target same project')
        self.assertEqual(j['auth']['scope_source'], 'authoritative installation/tenant/project binding')
        self.assertFalse(j['auth']['headers_create_membership'])
        self.assertFalse(j['auth']['claim_capability_on_data_routes'])
        self.assertFalse(j['auth']['installer_login_in_serve'])
        self.assertEqual(j['auth']['duplicate_authority_headers'], 400)
        self.assertEqual(j['headers']['idempotency_key'], 'required canonical UUID; maps to operation_key')
        self.assertEqual(j['headers']['project'], 'X-Project; must match authenticated project')
        self.assertEqual(j['headers']['writer'], 'X-Agent-Name equals enrolled actor; claim uses J2 approved name, never header promotion')

    def test_j3_prepared_claim_and_worker_shapes_reject_escalation(self):
        claim = self.prepared()
        self.assertTrue(self.validator('ClaimRequest').is_valid(claim))
        worker = dict(claim, permissions=['read', 'write'], ttl_seconds=60)
        self.assertTrue(self.validator('WorkerRequest').is_valid(worker))
        for extra in [{'secret': 'synthetic'}, {'project_id': claim['principal_id']},
                      {'roles': ['admin']}, {'permissions': ['control']}, {'ttl_seconds': None}]:
            with self.subTest(extra=extra):
                self.assertFalse(self.validator('ClaimRequest').is_valid(dict(claim, **extra)))
        for field, bad in [('permissions', ['control']), ('permissions', ['owner']),
                           ('permissions', ['admin']), ('permissions', []),
                           ('permissions', ['read', 'read']), ('ttl_seconds', None),
                           ('ttl_seconds', True), ('ttl_seconds', 59), ('ttl_seconds', 2592001)]:
            value = dict(worker); value[field] = bad
            with self.subTest(field=field, bad=bad):
                self.assertFalse(self.validator('WorkerRequest').is_valid(value))
        for ttl in (60, 2592000):
            self.assertTrue(self.validator('WorkerRequest').is_valid(dict(worker, ttl_seconds=ttl)))

    def test_j3_ids_digests_names_and_required_fields_are_closed(self):
        good = self.prepared()
        for field in good:
            value = dict(good); del value[field]
            with self.subTest(omitted=field):
                self.assertFalse(self.validator('ClaimRequest').is_valid(value))
        for field, bad in [('credential_sha256', 'a' * 63), ('credential_sha256', 'A' * 64),
                           ('credential_sha256', 'a' * 64 + '\n'),
                           ('principal_id', '11111111111141118111111111111111'),
                           ('credential_id', '22222222-2222-4222-8222-222222222222\n'),
                           ('name', ''), ('name', 'other:worker'), ('name', 'worker\n'),
                           ('name', 'x' * 220)]:
            value = dict(good); value[field] = bad
            with self.subTest(field=field, bad=bad):
                self.assertFalse(self.validator('ClaimRequest').is_valid(value))
        self.assertTrue(self.validator('ClaimRequest').is_valid(dict(good, name='équipe-新')))

    def test_j3_lead_self_rotation_only_nonexpiring_children_finite(self):
        base = {key: value for key, value in self.prepared().items() if key != 'name'}
        base.pop('principal_id')
        base['old_credential_id'] = '33333333-3333-4333-8333-333333333333'
        self.assertTrue(self.validator('LeadRotateRequest').is_valid(dict(base, ttl_seconds=None)))
        self.assertFalse(self.validator('LeadRotateRequest').is_valid(dict(base, ttl_seconds=60)))
        self.assertTrue(self.validator('WorkerRotateRequest').is_valid(dict(base, ttl_seconds=60)))
        for ttl in (None, True, 59, 2592001):
            self.assertFalse(self.validator('WorkerRotateRequest').is_valid(dict(base, ttl_seconds=ttl)))
        self.assertEqual(self.vocabulary()['lifetime'], {
            'lead': 'until explicit revoke/rotation; expires_at=null',
            'lead_rotation': 'self only, preserves nonexpiry and project control',
            'worker_ttl_seconds': [60, 2592000],
            'worker_permissions': ['read', 'write'],
            'server_selects_target_policy': True,
            'reboot_or_old_backup_restores_revoked_authority': False})
        self.assertTrue(self.validator('RevokeRequest').is_valid({}))
        self.assertFalse(self.validator('RevokeRequest').is_valid({'revoke_all': True}))

    def test_j3_receipts_are_committed_identity_receipts_not_c05_fiction(self):
        for name, value in [('LeadReceipt', self.receipt(lead=True)),
                            ('WorkerReceipt', self.receipt()), ('RevokeReceipt', self.receipt(revoked=True))]:
            with self.subTest(schema=name):
                self.assertTrue(self.validator(name).is_valid(value))
                for field in ('audit_id', 'event_id', 'intent_sha256', 'credential_sha256'):
                    bad = copy.deepcopy(value); del bad['receipt'][field]
                    self.assertFalse(self.validator(name).is_valid(bad))
                for field in ('secret', 'credential', 'unlock', 'record_id', 'payload_sha256'):
                    bad = copy.deepcopy(value); bad['receipt'][field] = 'synthetic'
                    self.assertFalse(self.validator(name).is_valid(bad))
        bad = self.receipt(lead=True); bad['receipt']['expires_at'] = '2026-10-20T01:00:00Z'
        self.assertFalse(self.validator('LeadReceipt').is_valid(bad))
        bad = self.receipt(); bad['receipt']['expires_at'] = None
        self.assertFalse(self.validator('WorkerReceipt').is_valid(bad))
        self.assertEqual(self.vocabulary()['commit'], 'same Identity SQL transaction commits scope, credential digest, idempotency receipt, audit and non-null c06 event before 200')

    def test_j3_replay_conflict_and_revocation_contract(self):
        j = self.vocabulary()
        self.assertEqual(j['replay']['same_intent'], 'same committed receipt; only replayed may change; no new principal/credential/event')
        self.assertEqual(j['replay']['changed_intent_or_target'], 409)
        self.assertEqual(j['replay']['claim_consumed'], 'exact committed replay only while currently admitted; never new enrollment')
        self.assertEqual(j['replay']['expired_or_revoked_claim'], 401)
        self.assertEqual(j['replay']['revoked_lead_or_old_backup'], '401; consumed claim cannot restore authority')
        self.assertEqual(j['replay']['lost_response'], 'retain encrypted PREPARED preimage, stable IDs and key; reconcile exact receipt; never mint replacement')
        self.assertEqual(j['denials'], {'invalid_input': 400, 'unauthenticated': 401,
                                      'forbidden': 403, 'not_found': 404,
                                      'conflict': 409, 'core_unavailable': 503})
        self.assertEqual(j['replay']['intent_scope'], ['operation', 'installation_id', 'tenant_id',
            'project_id', 'actor_or_claim_id', 'path_targets', 'validated_request', 'operation_key'])

    def test_j3_project_names_and_first_console_are_not_promoted(self):
        j = self.vocabulary()
        self.assertEqual(j['names'], {'canonical': '<project UUID>:<exact name>',
            'name_max_characters': 219, 'canonical_max_characters': 256,
            'normalize_or_fixed_roster': False, 'collision': 409})
        self.assertEqual(j['first_console'], {'kind': 'human', 'permissions': ['read', 'write'],
                                            'roles': [], 'promoted_by_claim': False})
        self.assertEqual(j['lead_permissions'], ['read', 'write', 'control'])
        self.assertFalse(j['project_creation'])
        self.assertEqual(j['ownership']['handlers_identity_runtime'], 'finn')
        self.assertEqual(j['actual_bound_checks'], 'NOT RUN until Finn J2/J3 exact source; no fixture/export/installed credit')

    def test_j3_catalog_reference_matches_vocabulary_and_matrix_digest(self):
        import hashlib
        j = self.vocabulary()
        catalog = json.loads((NEXT / 'contracts/c11a-consumer-routes.json').read_text())
        self.assertIn('pending_j3_contract', catalog)
        pin = hashlib.sha256(json.dumps(j, ensure_ascii=False, sort_keys=True,
                                       separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        self.assertEqual(catalog['pending_j3_contract'], {
            'source': 'next/src/cortex_core/compat/contract.md',
            'pointer': '/project_enrollment', 'sha256': pin,
            'binding': 'pending_finn_j2_j3'})
        self.assertEqual(catalog['c01_matrix_sha256'],
                         hashlib.sha256((NEXT / 'contracts/route-matrix.md').read_bytes()).hexdigest())
        from cortex_core.api.c11a import load_routes
        rows = load_routes()
        pairs = {(r['method'], r['path']) for r in rows}
        self.assertIn(('POST', '/agents'), pairs)
        for key in ('claim', 'rotate', 'revoke'):
            op = j['operations'][key]
            self.assertNotIn((op['method'], op['path']), pairs)
        matrix = (NEXT / 'contracts/route-matrix.md').read_text()
        self.assertIn('J3 pending contract amendment', matrix)
        self.assertIn(pin, matrix)

    def test_j3_interim_agents_is_truthful_unbound_refusal(self):
        import asyncio
        from cortex_core.api.c11a import ConsumerGateway
        async def value(*args): return True
        async def principal(*args): return {'principal_id': 'fixture', 'project_id': 'fixture'}
        async def state(*args): return {}
        async def run():
            gateway = ConsumerGateway(core_probe=value, principal_resolver=principal,
                permission_recheck=value, capability_source=state, health=state, handlers={})
            messages = []
            async def receive(): return {'type': 'http.request', 'body': b'{}', 'more_body': False}
            async def send(message): messages.append(message)
            await gateway.app({'type': 'http', 'method': 'POST', 'path': '/agents',
                'query_string': b'', 'headers': [(b'idempotency-key', b'11111111-1111-4111-8111-111111111111')],
                'client': ('127.0.0.1', 12000)}, receive, send)
            return messages
        messages = asyncio.run(run())
        self.assertEqual(next(m['status'] for m in messages if m['type'] == 'http.response.start'), 503)
        body = json.loads(b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body'))
        self.assertEqual(body['error']['code'], 'capability_unavailable')
        self.assertEqual(body['error']['reason'], 'adapter_unbound')


if __name__ == "__main__":
    unittest.main()
