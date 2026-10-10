"""Execute the actual source guard assignments, without importing API runtime."""
import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[3]
MAIN = ROOT / 'packages/api/main.py'
MIGRATIONS = ROOT / 'packages/schema/migrations'


def flagged(sql):
    tree = ast.parse(MAIN.read_text())
    function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == 'apply_schema_migrations')
    assignments = [n for n in function.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id in
                           {'nontransactional_pattern', 'nontransactional'} for t in n.targets)]
    assert len(assignments) == 2
    helpers = [n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name == '_sql_transaction_scan_text']
    namespace = {'re': re, 'pending': [{'id': 'fixture.sql'}],
                 'files_by_id': {'fixture.sql': {'sql': sql}}}
    module = ast.Module(body=helpers + assignments, type_ignores=[])
    exec(compile(module, str(MAIN), 'exec'), namespace)
    return 'fixture.sql' in namespace['nontransactional']


class MigrationTransactionScan(unittest.TestCase):
    def test_real_agent_identity_function_is_not_nontransactional(self):
        sql = (MIGRATIONS / '2026-09-17-01-service-auth-agent-identities.sql').read_text()
        self.assertFalse(flagged(sql))

    def test_real_agent_revocation_function_is_not_nontransactional(self):
        sql = (MIGRATIONS / '2026-09-17-03-service-auth-agent-revocation.sql').read_text()
        self.assertFalse(flagged(sql))

    def test_top_level_commit_and_end_still_refused(self):
        for sql in ['COMMIT;', 'END;', 'BEGIN;', 'ROLLBACK;', 'START TRANSACTION;']:
            with self.subTest(sql=sql):
                self.assertTrue(flagged(sql))

    def test_top_level_online_index_still_refused(self):
        for sql in ['CREATE INDEX CONCURRENTLY x ON t(id);',
                    'CREATE UNIQUE INDEX CONCURRENTLY x ON t(id);',
                    'DROP INDEX CONCURRENTLY x;', 'REINDEX INDEX CONCURRENTLY x;']:
            with self.subTest(sql=sql):
                self.assertTrue(flagged(sql))

    def test_top_level_vacuum_and_materialized_refresh_still_refused(self):
        for sql in ['VACUUM t;', 'REFRESH MATERIALIZED VIEW CONCURRENTLY m;']:
            with self.subTest(sql=sql):
                self.assertTrue(flagged(sql))

    def test_untagged_dollar_body_not_transaction_control(self):
        self.assertFalse(flagged('CREATE FUNCTION f() RETURNS void AS $$\nBEGIN\nEND;\n$$ LANGUAGE plpgsql;'))

    def test_tagged_dollar_body_not_transaction_control(self):
        self.assertFalse(flagged('CREATE FUNCTION f() RETURNS void AS $fn$\nBEGIN\nEND;\n$fn$ LANGUAGE plpgsql;'))

    def test_unicode_tagged_body_not_transaction_control(self):
        self.assertFalse(flagged('SELECT $ñ_9$\nEND;\n$ñ_9$;'))

    def test_dollar_delimiter_case_is_significant(self):
        self.assertFalse(flagged('SELECT $Fn$\n$fn$\nCOMMIT;\n$Fn$;'))

    def test_single_quoted_literal_not_transaction_control(self):
        self.assertFalse(flagged("SELECT 'text\nEND;\ntext';"))

    def test_doubled_single_quote_remains_inside_literal(self):
        self.assertFalse(flagged("SELECT 'text''quoted\nEND;\ntext';"))

    def test_escape_string_quote_remains_inside_literal(self):
        self.assertFalse(flagged("SELECT E'text\\'quoted\nEND;\ntext';"))

    def test_line_comment_not_transaction_control(self):
        self.assertFalse(flagged('SELECT 1;\n-- END;\nSELECT 2;'))

    def test_block_comment_not_transaction_control(self):
        self.assertFalse(flagged('SELECT 1;\n/*\nEND;\n*/\nSELECT 2;'))

    def test_nested_block_comment_not_transaction_control(self):
        self.assertFalse(flagged('/* outer /* inner */\nEND;\n*/\nSELECT 1;'))

    def test_real_control_after_closed_dollar_body_still_refused(self):
        self.assertTrue(flagged('SELECT $fn$\nEND;\n$fn$;\nCOMMIT;'))

    def test_real_control_after_closed_literal_still_refused(self):
        self.assertTrue(flagged("SELECT 'END;';\nCOMMIT;"))

    def test_standard_string_backslash_does_not_escape_closing_quote(self):
        self.assertTrue(flagged("SELECT 'text\\';\nCOMMIT;"))

    def test_real_control_after_closed_comment_still_refused(self):
        self.assertTrue(flagged('/*\nEND;\n*/\nCOMMIT;'))

    def test_control_after_line_comment_newline_still_refused(self):
        self.assertTrue(flagged('-- comment\nCOMMIT;'))

    def test_identifier_dollar_signs_do_not_start_a_body(self):
        self.assertTrue(flagged('SELECT foo$fn$bar;\nCOMMIT;\nSELECT $fn$tail$fn$;'))

    def test_quoted_identifier_dollar_signs_do_not_start_a_body(self):
        self.assertTrue(flagged('SELECT "foo$fn$bar";\nCOMMIT;\nSELECT $fn$tail$fn$;'))

    def test_comment_tokens_inside_literals_do_not_hide_real_control(self):
        self.assertTrue(flagged("SELECT '/*';\nCOMMIT;"))

    def test_dollar_tokens_inside_literals_do_not_hide_real_control(self):
        self.assertTrue(flagged("SELECT '$fn$';\nCOMMIT;\nSELECT $fn$tail$fn$;"))

    def test_line_comment_cannot_hide_following_vacuum(self):
        self.assertTrue(flagged('-- ignored\nVACUUM t;'))
