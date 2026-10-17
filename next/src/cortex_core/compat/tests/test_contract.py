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


if __name__ == '__main__':
    unittest.main()
