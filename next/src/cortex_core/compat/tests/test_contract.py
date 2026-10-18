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

    def test_custody_proposals_require_owner_reconciliation(self):
        d = day2(self)
        self.assertEqual(d['custody'].get('design_state'), 'PROPOSED; accepted reconciliation receipt required')
        self.assertEqual(d['custody']['reconciliation_owner'], 'cox with finn')
        self.assertEqual(set(d['custody']['design_receipts']), {'J06-custody-adapter.md', 'J06-seed-replay.md'})
        self.assertEqual(d['custody']['open_reconciliation'], ['unlock_protocol', 'AAD_identity',
            'record_reference', 'lock_semantics', 'absence_observation', 'adapter_and_client_api'])
        self.assertEqual(d['custody']['pre_sql_scope'], 'before mutating SQL; fresh read-only admission needs owner ruling')
        self.assertFalse(d['custody']['drafts_authorize_build'])

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



def accepted_v1(case):
    m = field_map(case)
    case.assertIn('accepted_j06_v1', m, 'accepted superseding J06 v1 obligations missing')
    return m['accepted_j06_v1']


class AcceptedJ06V1Conformance(unittest.TestCase):
    """Accepted design obligations only; no runtime/source/installed admission."""

    def test_controlling_acceptance_preserves_historical_proposals(self):
        a = accepted_v1(self)
        self.assertEqual(a['authority'], {'interface': 'J06-interface-v1.md',
            'sha256': '25f208b916fa71f7c42be52399c0a92b1be1b2241d412427ab395712893d6d49',
            'disposition': 'ACCEPT_FOR_BOUNDED_SOURCE_WITH_INTERFACE_RESOLUTIONS',
            'controls_over_conflicting_proposals': True, 'historical_section': 'day2',
            'source_accepted': False, 'installed_accepted': False})

    def test_metadata_never_grants_purpose_authority(self):
        a = accepted_v1(self)
        self.assertEqual(a['config_metadata'], {'immutable_all_modes': ['source_sha256', 'role_logins', 'client_ref'],
            'source_sha256': 'SHA256 of full canonical decoded config',
            'role_logins': 'read-only five-purpose nonsecret login map',
            'client_ref': 'nonsecret metadata retained in ALL modes',
            'resolve_guard': 'mode and purpose checked BEFORE filesystem access',
            'metadata_grants_credential_access': False, 'combined_login_reference_uniqueness': True})

    def test_explicit_unlock_and_no_replacement_key(self):
        a = accepted_v1(self)
        self.assertEqual(a['unlock'], {'crypto': 'cryptography==50.0.0 AESGCM', 'key_bytes': 32,
            'key_terminator': 'EOF', 'key_id': 'explicit', 'read_max_bytes': 33, 'absolute_seconds': 1,
            'fd0': 'explicit selection AND validated anonymous pipe', 'fd1_fd2': 'refuse',
            'private_regular_fd': 'owned/private/single-link/read-only validation',
            'recovery': 'same operator-held key or refuse', 'fallbacks': []})

    def test_one_encrypted_record_and_authenticated_identity(self):
        a = accepted_v1(self)
        self.assertEqual(a['record'], {'reference': 'existing client.credential_ref',
            'second_seed_intent_file': False, 'states': ['prepared', 'committed'],
            'aad': ['schema', 'installation_id', 'tenant_id', 'project_id', 'reference', 'purpose',
                    'key_id', 'clear authenticated generation'],
            'physical_path_is_identity': False, 'offline_malicious_rollback_proof': False})

    def test_closed_seed_payload_has_no_secret_in_intent_or_receipt(self):
        a = accepted_v1(self)
        self.assertEqual(a['payload'], {'schema': 'cortex.seed-custody.v1',
            'closed_fields': ['schema', 'intent', 'credential', 'state', 'receipt'],
            'credential': 'strict base64 of private Bearer bytes INSIDE encryption',
            'receipt_before_commit': None, 'secret_in_intent_result_log_receipt': False})

    def test_readonly_creation_then_durable_prepared_before_writes(self):
        a = accepted_v1(self)
        self.assertEqual(a['seed_order'], ['bounded authoritative read-only empty-state observation',
            'durable encrypted prepared before ANY SQL writes', 'bootstrap anchors/bindings transaction',
            'actual kaidera-runtime-core-installer login calls auth.first_identity',
            'api_request confirms current readiness', 'durable encrypted committed checkpoint', 'finite close/serve'])
        self.assertEqual(a['seed_refusals'], {'missing_custody_existing_or_partial_sql': 'refuse; no replacement',
            'ambiguous_commit': 'replay/reconcile same intent and credential; never assume rollback',
            'db_filesystem_atomicity': False, 'local_lock_replaces_sql_guards': False})

    def test_fixed_adapter_and_seed_material_lifetime(self):
        a = accepted_v1(self)
        self.assertEqual(a['adapter'], {'import': 'cortex_core.runtime.custody',
            'open': 'open_custody(config, *, unlock_fd, key_id)',
            'seed_record': 'seed mode only; owned stable nonblocking lock',
            'methods': ['load()', 'create(intent=..., credential=...)', 'commit(receipt=...)'],
            'load_none': 'absent leaf ONLY', 'create': 'absent only',
            'commit': 'retain intent/credential; verify loaded identity/digest/generation; generation+1',
            'same_committed_receipt': 'idempotent no-write', 'changed_receipt': 'refuse',
            'durability_unknown': 'stop SQL/closure; later reload and reconcile',
            'material': 'frozen/repr-suppressed; defensive intent/receipt copies; private credential',
            'after_close': 'refuse', 'unlink_live_lock': False, 'public_injectable_authority': False})

    def test_operator_resources_bind_actual_type_identity_deadline(self):
        a = accepted_v1(self)
        self.assertEqual(a['resources'], {'open': 'runtime.resources.open_seed_resources(config, *, custody, deadline)',
            'connections': ['bootstrap', 'installer_enrollment', 'api_request'],
            'connector': 'actual psycopg; verify-full and admitted root certificate',
            'accessor': 'current_seed_resources(config, deadline)',
            'requires': ['SAME config object', 'SAME absolute monotonic deadline', 'live context',
                         'actual fixed SeedResources type'],
            'carrier': 'private context variable; actual carrier.custody',
            'cleanup': 'partial-open cleanup; reverse close; reset context on every exit',
            'public_seed': 'seed_runtime_bindings(config, operation_key, deadline)',
            'serve_only': ['api_request', 'probe'], 'driver_exception_text': False,
            'unit_doubles_grant_production_authority': False})

    def test_exact_operator_argv_and_explicit_projection_pipe(self):
        a = accepted_v1(self)
        self.assertEqual(a['projection']['argv'], ['python', '-m', 'cortex_core.runtime.operator',
            'config', 'client', '--config', 'FILE', '--unlock-fd', 'N', '--key-id', 'ID',
            '--projection-fd', 'M', '[--project NAME]'])
        self.assertEqual(a['projection']['output_fd'], 'explicit owned anonymous pipe; no tty/regular file/default stdout')
        self.assertEqual(a['projection']['selection'], 'ONE admitted config map; default selected project UUID')
        self.assertEqual(a['projection']['prefix_before_full_validation'], False)

    def test_seven_nul_terminated_fields_and_eof(self):
        a = accepted_v1(self)
        self.assertEqual(a['projection']['fields'], ['cortex.client.v1', 'admitted loopback URL',
            'installation UUID', 'selected project UUID', 'admitted project name',
            'committed client credential', 'integer total request seconds'])
        self.assertEqual(a['projection']['framing'], 'exactly seven UTF-8 fields, EACH NUL-terminated, then EOF')
        self.assertEqual(a['projection']['reject_in_every_field'], ['NUL', 'CR', 'LF'])
        self.assertEqual(a['projection']['request_seconds'], 'min(config.request_seconds, 10)')
        self.assertEqual(a['projection']['helper'], 'packages/cli/_cortex_api.sh; fixed framing only; no config/decryption')

    def test_exact_credential_and_committed_receipt_schema(self):
        a = accepted_v1(self)
        self.assertEqual(a['credential'], {'format': '64 lower-case ASCII hex',
            'entropy_bytes': 32, 'generate': 'once', 'sha256_input': 'raw ASCII Bearer bytes',
            'base64_is_storage_encoding_only': True})
        self.assertEqual(a['intent']['fields'], ['operation_key', 'installation_id', 'tenant_id', 'project_id',
            'principal_id', 'credential_id', 'client_ref', 'schema_prefix_sha256', 'credential_sha256',
            'database', 'roles'])
        self.assertEqual(a['intent']['operation_key_bound'], 'nonempty; <=256 UTF-8 characters and <=1024 bytes')
        self.assertEqual(a['intent']['database_fields'], ['name', 'oid', 'owner_oid'])
        self.assertEqual(a['intent']['role_fields'], ['login', 'oid', 'revision', 'flags', 'memberships'])
        self.assertEqual(a['receipt']['fields'], ['installation_id', 'tenant_id', 'project_id', 'principal_id',
            'credential_id', 'operation_key', 'credential_sha256', 'intent_sha256'])
        self.assertEqual(a['receipt']['intent_sha256'], 'SHA256 of Core canonical JSON bytes of intent')
        self.assertEqual(a['receipt']['readiness_is_reusable_authority'], False)
        self.assertEqual(a['receipt']['retry'], 'reconcile exact SQL receipt, actual purpose identity and CURRENT readiness')

    def test_installed_crypto_closure_and_no_plaintext_client_fallback(self):
        a = accepted_v1(self)
        self.assertEqual(a['closure'], {'pure_config_render': 'stdlib only',
            'encrypted_projection': 'actual installed Core cryptography closure; Nix qualifies BOTH targets',
            'ad_hoc_install_bootstrap': False,
            'bob_transfer': 'actual private FD transfer and minimal selected record mount; no whole-vault/finite-role serve mount',
            'old_env_or_credential_file_wire_fallback': False, 'plaintext_host_client_fallback': False,
            'secret_in_argv_env_temp_log': False,
            'missing_uncommitted_unsafe_custody': 'typed refusal before networking',
            'failure_output': 'one sanitized typed JSON line; no driver/OS details'})

    def test_design_acceptance_does_not_close_source_ci_or_installed_gates(self):
        a = accepted_v1(self)
        self.assertEqual(a['open_proofs'], ['exact published Cox resources source before Finn integration',
            'Nix executable/native cryptography closure', 'Bob actual private FD/mount transfer',
            'Mike published RUN addition AND exact composed inventory', 'Cox commissions ONE Hale exact-final-head review',
            'paired native/installed proof'])
        self.assertEqual(a['runtime_implementation_pin'], None)
        self.assertEqual(a['fixture_as_live_credit'], False)
        self.assertEqual(a['ai'], 'off; .005 deferred; no provider/model defaults or calls')



def versioned_cli(case):
    m = field_map(case)
    case.assertIn('versioned_cli', m, 'accepted versioned CLI mapping missing')
    return m['versioned_cli']


class VersionedCLIConformance(unittest.TestCase):
    """Mapping plus actual checkout byte/path checks; no v1/live execution."""

    def test_controlling_versioned_amendment(self):
        v = versioned_cli(self)
        self.assertEqual(v['ruling'], {'file': '2026-10-17_cox_a-day2-versioned-cli-ruling-v1.md',
            'sha256': '11d7ebce3a3f0ebac0ae4f3b85eb5a0a6a5c0e2190a712269ac6231a64ebf1a5',
            'authority': 'Kai Option A accepted by Cox', 'scope': 'SPEC only'})
        self.assertEqual(v['supersedes_helper_paths'], ['day2.transport.owner_path', 'accepted_j06_v1.projection.helper'])

    def test_one_v2_module_and_five_transport_test_paths(self):
        v = versioned_cli(self)
        self.assertEqual(v['home'], 'next/modules/cli-v2')
        self.assertEqual(v['helper'], 'next/modules/cli-v2/_cortex_api.sh')
        self.assertEqual(v['transport_tests'], ['next/modules/cli-v2/tests/test_transport_' + n + '.py'
            for n in ['admission', 'composition', 'processes', 'projection', 'results']])
        self.assertEqual(v['module_docs'], ['next/modules/cli-v2/README.md', 'next/modules/cli-v2/UPSTREAM.md'])

    def test_three_versioned_entries_retain_existing_mapping(self):
        v = versioned_cli(self)
        self.assertEqual(v['commands'], {('cortex-v2-' + n): {'source': 'next/modules/cli-v2/cortex-v2-' + n,
            'capability': n} for n in ['boot', 'log', 'search']})
        self.assertTrue(all(entry['capability'] in field_map(self)['mapping'] for entry in v['commands'].values()))
        self.assertEqual(v['new_API_capability_from_rename'], False)

    def test_v1_baseline_manifest_binds_all_retained_files(self):
        v = versioned_cli(self)
        self.assertEqual(v['v1']['baseline'], '7a1078213ac2497d6219e7778c7866001f97f30b')
        self.assertEqual(v['v1']['manifest'], 'tests/v1_baseline.json')
        self.assertEqual(v['v1']['helper_sha256'], 'd78ad0c87ffdf286c69b2485d3224186decc81b37608a4ae92fe40806d84f636')
        import hashlib
        p = CONTRACT.parent / v['v1']['manifest']
        self.assertTrue(p.is_file(), 'retained v1 path/byte manifest missing')
        self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), v['v1']['manifest_sha256'])
        self.assertEqual(v['v1']['preserve'], 'every manifest path, name, mode and exact baseline bytes')
        self.assertEqual(v['v1']['live_contract_tests'], 'retained unchanged; Mike owns actual v1 regression admission/execution')

    def test_actual_checkout_retains_every_v1_path_mode_and_byte(self):
        import hashlib
        manifest = json.loads((CONTRACT.parent / 'tests/v1_baseline.json').read_text())
        root = Path(__file__).resolve().parents[5]
        observed = {str(p.relative_to(root)) for scope in manifest['scopes']
            for p in (root / scope).rglob('*') if p.is_file() and '__pycache__' not in p.parts}
        self.assertEqual(observed, {r['path'] for r in manifest['files']}, 'v1 path/name set drift')
        self.assertTrue(manifest['files'], 'empty preservation set earns no credit')
        for row in manifest['files']:
            p = root / row['path']
            with self.subTest(path=row['path']):
                self.assertFalse(p.is_symlink(), 'baseline blob replaced by symlink')
                self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), row['sha256'], 'v1 bytes drift')
                self.assertEqual(bool(p.stat().st_mode & 0o111), row['mode'] == '100755', 'v1 executable mode drift')

    def test_private_pipe_and_one_reader_survive_move(self):
        v = versioned_cli(self)
        self.assertEqual(v['transport'], {'helper_owner': 'finn', 'launcher_owner': 'bob',
            'order': ['projector', 'wait/reap', 'exec same reader FD'], 'readers': 1,
            'private_projection': 'same accepted seven NUL-terminated fields then EOF',
            'new_HTTP_config_token_parser': False, 'v1_authority_fallback': False,
            'secret_argv_env_file': False})

    def test_provider_and_unshipped_capabilities_remain_refusals(self):
        v = versioned_cli(self)
        self.assertEqual(v['capability_guard'], 'existing accepted mapping and typed unshipped refusals; no capability from rename')
        self.assertEqual(v['provider_calls'], 0)
        self.assertEqual(v['live_effects'], {'installed': 0, 'PATH': 0, 'v1_service_DB': 0})

    def test_changed_head_requires_own_ci_and_accountable_review(self):
        v = versioned_cli(self)
        self.assertEqual(v['acceptance'], {'old_head_inherited': False, 'author_self_merge': False,
            'Mike': 'v1 regression plus v2 RUN and exact composed inventory/CI',
            'Cox': 'decides ONE bounded final exact-head Hale review',
            'fixtures_as_installed': False})


if __name__ == '__main__':
    unittest.main()
