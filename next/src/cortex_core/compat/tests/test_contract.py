"""Option-A specification assertions, NOT runtime or installed acceptance.

The normative JSON is a field map in Markdown, not a second runtime registry.
Mike's generated HTTP and installed tests remain separate obligations.
"""
import json
from pathlib import Path
import unittest


CONTRACT = Path(__file__).resolve().parents[1] / 'contract.md'
EVENTS = ['commit', 'decision', 'lesson', 'started', 'stopped', 'blocked',
          'unblocked', 'bug', 'handoff', 'question']


def field_map(case):
    # Called from the assertion body: a missing spec is an intended BODY RED.
    case.assertTrue(CONTRACT.is_file(), 'option-A normative contract is missing')
    text = CONTRACT.read_text(encoding='utf-8')
    case.assertEqual(text.count('```json\n'), 1, 'exactly one normative field map')
    return json.loads(text.split('```json\n', 1)[1].split('\n```', 1)[0])


class OptionAContract(unittest.TestCase):
    def test_ai_mode_off(self):
        m = field_map(self)
        self.assertEqual(m['profile'], {'ai_mode': 'off', 'missing_mode': 'refuse',
                                      'provider_construction': False, 'provider_attempts': 0})

    def test_unshipped_provider_refusal(self):
        m = field_map(self)
        self.assertEqual(m['unshipped']['response'], {'status': 503, 'code': 'capability_unavailable',
            'reason': 'unshipped_operation', 'retryable': False})
        self.assertTrue(m['unshipped']['authenticate_authorize_first'])
        self.assertTrue(m['unshipped']['before_effects'])
        self.assertEqual(m['unshipped']['effects'], dict.fromkeys(
            ['provider_construction', 'provider_dns_connect', 'sql_mutations', 'events', 'jobs', 'files'], 0))
        self.assertEqual(m['unshipped']['provider_defaults'], [])

    def test_generated_coverage(self):
        m = field_map(self)
        required = {'boot', 'log', 'memory', 'record', 'verify', 'search'}
        self.assertEqual(set(m['mapping']), required)
        assertions = {'authorized_expected_outcome', 'status', 'content_type', 'request_schema',
                      'response_schema', 'auth_scope', 'links'}
        generated = {(name, assertion) for name in m['mapping'] for assertion in m['coverage']['assertions']}
        self.assertEqual(generated, {(name, assertion) for name in required for assertion in assertions})
        self.assertEqual(m['coverage']['scope'], 'selected-command-spec-only')
        self.assertEqual(m['coverage']['live_gate'], {'every_published_operation': True, 'requests_min': 1,
            'required_skips': 0, 'all_401_accepted': False, 'source': 'Cox bound registry/OpenAPI'})
        for entry in m['mapping'].values():
            self.assertTrue(entry['auth'])
            self.assertTrue(entry['path'].startswith('/'))
        self.assertEqual(set(m['unshipped']['families']), {'semantic', 'rerank', 'judge', 'provider_settings',
            'taxonomy', 'graph', 'handoff_dispatch', 'full_boot', 'history'})

    def test_log_fields_and_nulls(self):
        m = field_map(self)
        fields = m['fields']
        self.assertEqual(set(fields), {'event_type', 'summary', 'category', 'importance', 'files_affected', 'metadata'})
        self.assertEqual(fields['event_type'], {'required': True, 'type': 'enum', 'values': EVENTS,
            'payload': ['section', 'event_type']})
        self.assertEqual(fields['summary'], {'required': True, 'type': 'string', 'payload': ['content', 'summary']})
        for name, kind, default in [('category', 'string|null', None), ('importance', 'integer|null', 5),
                                  ('files_affected', 'array[string]|null', None), ('metadata', 'object|null', None)]:
            self.assertEqual(fields[name], {'required': False, 'type': kind, 'omitted': default, 'payload': [name]})
        self.assertEqual(m['validation']['unknown_or_supersession_fields'], 'reject')
        self.assertEqual(m['validation']['integer_booleans'], 'reject')

    def test_exact_utf8_and_bounds(self):
        m = field_map(self)
        self.assertEqual(m['validation']['utf8'], 'exact decoded strings; no trim, normalization or prefix')
        self.assertEqual(m['validation']['invalid_unicode'], 'reject')
        self.assertEqual(m['validation']['duplicate_keys_or_nonfinite_json'], 'reject')
        self.assertEqual(m['validation']['null_empty_distinct'], True)
        self.assertEqual(m['validation']['files_order_duplicates'], 'preserve')
        self.assertEqual(m['validation']['log_json_max_utf8_bytes'], 262144)
        self.assertEqual(m['validation']['projection_max_utf8_bytes'], 16384)
        self.assertEqual(m['validation']['projection'], 'section + LF + content')

    def test_single_writer_c06_guard(self):
        m = field_map(self)
        self.assertEqual(m['log_write']['kind'], 'memory')
        self.assertEqual(m['log_write']['transactions'], 1)
        self.assertEqual(m['log_write']['event_guard'], 'coordination.c06_record_event must return non-null')
        self.assertFalse(m['log_write']['c07_jobs'])
        self.assertEqual(m['log_write']['server_fields'], {'record_class': 'log', 'writer': 'authorized enrolled principal.name'})
        self.assertEqual(m['log_write']['normal_memory_log_source'], 'reject')

    def test_idempotency(self):
        m = field_map(self)
        self.assertEqual(m['replay'], {'key': 'required canonical UUID Idempotency-Key',
            'source': 'log:<key>', 'record_id': 'C11b source-scoped UUIDv5',
            'intent': 'normalized optional defaults + all fields + authorized writer; exact strings',
            'same_key_same_intent': 'same committed receipt; no new revision/event',
            'same_key_changed_intent': 409, 'foreign_writer_collision': 409,
            'lost_ack': 'uncertain; preserve key; never generate retry key'})

    def test_checked_verify_aliases(self):
        m = field_map(self)
        self.assertEqual(m['verify']['aliases'], {'decision': ['decision'], 'lesson': ['lesson'], 'team_event': EVENTS})
        self.assertTrue(m['verify']['log_marker_required'])
        self.assertTrue(m['verify']['stored_type_check'])
        self.assertEqual(m['verify']['mismatch'], 400)
        self.assertEqual(m['verify']['success'], 'authorized post-commit read matches exact fields and digest')
        self.assertFalse(m['verify']['fabricated_team_event_id'])

    def test_search_mode_taxonomy(self):
        m = field_map(self)
        self.assertEqual(m['search']['explicit'], {'mode': 'keyword', 'type': 'all',
            'rerank': False, 'graph': False, 'hall': 'project'})
        self.assertEqual(m['search']['taxonomy'], {'knowledge': 503, 'decisions': 503, 'lessons': 503, 'events': 503})
        self.assertEqual(m['search']['missing_mode'], 400)
        self.assertEqual(m['search']['scan_max_records'], 100)
        self.assertEqual(m['search']['incomplete'], '503; preserve partial/scan_limit/unsupported_text and freshness')

    def test_client_memory_translation_only(self):
        m = field_map(self)
        self.assertEqual(m['mapping']['memory'], {'method': 'POST', 'path': '/memory', 'auth': True,
            'client_alias': '/memory/write', 'server_alias': False})
        self.assertEqual(m['ownership'], {'spec': 'nemo', 'daily_adapter': 'finn', 'canonical_api_records': 'cox',
            'runtime_reads_this_map': False})



def day2(case):
    m = field_map(case)
    case.assertIn('day2', m, 'Day-2 published-owner conformance obligations missing')
    return m['day2']


class Day2PublishedConformance(unittest.TestCase):
    """Document conformance to pinned owner publications; no live/runtime credit."""

    def test_core_closed_config_fields(self):
        d = day2(self)
        self.assertEqual(d['config']['path'], 'config/cortex.json')
        self.assertEqual(set(d['config']['required']), {'version', 'installation_id', 'tenant_id',
            'project_id', 'project_names', 'ai_mode', 'vault_root', 'bind', 'publish',
            'client', 'database', 'roles', 'limits'})
        self.assertEqual(d['config']['decoder'], 'runtime.bootstrap_config.decode_config(raw, mode=mode)')
        self.assertTrue(d['config']['closed_keys'])
        self.assertEqual(d['config']['extra_parser_or_persisted_client_config'], False)

    def test_core_mode_purpose_views(self):
        d = day2(self)
        self.assertEqual(d['config']['mode_purposes'], {'serve': ['api_request', 'probe'],
            'migrate': ['migrator'], 'seed': ['bootstrap', 'installer_enrollment', 'api_request'],
            'status': [], 'client': []})
        self.assertEqual(d['config']['client_secret'], 'client.credential_ref only in client mode')
        self.assertEqual(d['config']['unknown_or_missing_mode'], 'config_mode')

    def test_core_tls_bounds_and_ai_off(self):
        d = day2(self)
        self.assertEqual(d['config']['tls'], {'mode': 'verify-full',
            'root_cert_ref': 'database.tls.root_cert_ref', 'other_mode': 'config_transport_unratified'})
        self.assertEqual(d['config']['bounds'], {'config_bytes': 65536, 'credential_bytes': 4096,
            'request_seconds': [1, 120], 'shutdown_seconds': [1, 120]})
        self.assertEqual(d['config']['ai_mode'], 'explicit off; missing=config_shape; other=unshipped_provider_refusal')

    def test_core_private_refs_and_disposable_projections(self):
        d = day2(self)
        self.assertEqual(d['projections']['reference_pattern'], 'secrets/cortex/[a-zA-Z0-9_-]{1,96}')
        self.assertEqual(d['projections']['reader'], ['owned', 'private', 'regular',
            'no_symlink_any_component', 'no_hardlinks', 'bounded', 'stable_inode'])
        self.assertEqual(d['projections']['purposes_source'], 'config.mode_purposes')
        self.assertTrue(d['projections']['add_root_cert_for_database'])
        self.assertEqual(d['projections']['whole_vault_mount'], False)
        self.assertEqual(d['projections']['authoritative_secret_copy'], 'encrypted project vault')

    def test_bob_operator_and_bootstrap_order(self):
        d = day2(self)
        self.assertEqual(d['operator']['state'], 'published proposal; dispatcher not delivered by foundation')
        self.assertEqual(d['operator']['argv'], ['/opt/cortex/python/bin/python3', '-I', '-B', '-m',
            'cortex_core.cli', '<serve|migrate|seed|status>', '--config', '/run/cortex/config/cortex.json'])
        self.assertEqual(d['operator']['bootstrap_order'], ['core-init', 'temporary-db', 'roles-pre',
            'migrate', 'roles-post', 'seed', 'roles-close', 'stop-temporary-db', 'serve'])
        self.assertEqual(d['operator']['retries'], 0)

    def test_client_wire_remains_explicitly_proposed(self):
        d = day2(self)
        self.assertEqual(d['client_join']['state'], 'OPEN: Cox/Finn/Bob reconciliation before integration')
        self.assertEqual(d['client_join']['bob_proposed_keys'], ['CORTEX_API_URL', 'CORTEX_PROJECT',
            'CORTEX_CREDENTIAL_FILE'])
        self.assertEqual(d['client_join']['core_command'], 'cortex config client --config FILE')
        self.assertEqual(d['client_join']['secret_transport'], 'protected pipe/FD; no secret argv/log/temp evidence')
        self.assertEqual(d['client_join']['installed_credit'], 0)
        self.assertFalse(d['client_join']['infer_credential_file_acceptance'])

    def test_finn_sole_seed_and_first_identity(self):
        d = day2(self)
        self.assertEqual(d['seed']['entry'], 'seed_runtime_bindings(config, operation_key, deadline)')
        self.assertEqual(d['seed']['published_implementation'], 'ABSENT at Finn Day-1 pin')
        self.assertEqual(d['seed']['owner'], 'finn')
        self.assertEqual(d['seed']['first_identity'], {'kind': 'human', 'grant': 'read/write', 'roles': []})
        self.assertEqual(d['seed']['request_time_enrollment'], False)
        self.assertEqual(d['seed']['migration_prefix'], 'unchanged 21; migrate before seed')

    def test_encrypted_custody_design_before_build(self):
        d = day2(self)
        self.assertEqual(d['custody']['gate'], 'Cox/Finn explicit encrypted-adapter design accepted before build')
        self.assertEqual(d['custody']['required_design'], ['crypto_donor_or_dependency', 'unlock_key_source',
            'nofollow_layout', 'crash_replay_state_machine', 'ownership', 'typed_failures'])
        self.assertEqual(d['custody']['inferred_defaults'], [])
        self.assertEqual(d['custody']['plaintext_fallback'], False)
        self.assertEqual(d['custody']['second_store'], False)

    def test_seed_replay_crash_and_authority_obligations(self):
        d = day2(self)
        self.assertEqual(d['seed']['sequence'], ['persist stable IDs/operation key/secret encrypted before SQL',
            'validate actual login and database OIDs/owner/flags/memberships',
            'bind and enroll through existing protected SQL', 'atomically checkpoint after commit'])
        self.assertEqual(d['seed']['lost_ack'], 'reuse identical intent/secret/IDs; no replacement credential')
        self.assertEqual(d['seed']['missing_custody'], 'custody_unavailable; no replacement credential')
        self.assertEqual(d['seed']['required_cases'], ['exact_retry', 'changed_intent', 'every_crash_boundary',
            'same_name_new_oid', 'unsafe_role_or_membership', 'concurrent_seed', 'serve_separation'])

    def test_transport_refusal_and_uncertain_write_obligations(self):
        d = day2(self)
        self.assertEqual(d['transport']['owner_path'], 'packages/cli/_cortex_api.sh')
        self.assertEqual(d['transport']['missing_projection_http_attempts'], 0)
        self.assertEqual(d['transport']['discovery_admin_v1_fallback'], False)
        self.assertEqual(d['transport']['mutation_retry'], 'explicit identical replay with same key only')
        self.assertEqual(d['transport']['refusal'], 'preserve typed packet; nonzero exit; no fabricated success')
        self.assertEqual(d['transport']['seed_or_helper_fixture_as_installed'], False)

    def test_owner_publication_pins_and_claim_limits(self):
        d = day2(self)
        self.assertEqual(d['published_heads'], {'cox': '8e1babb2118a7b2ecb11115babb4dd39c90750d6',
            'bob': '887c4d6d3d9e1523d13297b5bab949edb7c48b7c',
            'finn': 'bc4514a66164c9a5cbee449d7ff5bd2dc839d74c'})
        self.assertEqual(d['evidence_class'], 'document assertions against pinned publications; not runtime integration')
        self.assertEqual(d['provider_settings'], 'deferred .005; missing/unconfigured remains typed refusal')
        self.assertEqual(set(d['source_receipts']), {'cox_config', 'cox_contract', 'cox_reader',
            'bob_run', 'bob_cli', 'finn_negative', 'finn_helper'})
        for row in d['source_receipts'].values():
            self.assertEqual(len(row['sha256']), 64)
            self.assertEqual(len(row['git_blob']), 40)
            self.assertIn(row['owner'], d['published_heads'])
            self.assertEqual(row['commit'], d['published_heads'][row['owner']])

    def test_compat_ci_closure_requires_composed_inventory(self):
        d = day2(self)
        self.assertEqual(d['ci'], {'finding': 'HALE-A1-NEMO-CI-001', 'owner': 'mike',
            'manifest': '.github/next-offline-suites.json',
            'required_run': 'next/src/cortex_core/compat/tests/test_contract.py',
            'closure': 'published RUN addition AND exact composed inventory pass; Hale adjudicates',
            'inventory_is_suite_or_installed_pass': False, 'hosted_acceptance_inherited': False})


if __name__ == '__main__':
    unittest.main()
