"""Source-bound inventory artifact checks, never application qualification."""
import ast
import copy
import importlib.util
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
NEXT=Path(__file__).resolve().parents[2]


class WriterInventory(unittest.TestCase):
    def checker(self):
        p=NEXT/'scripts/verify_writer_inventory.py'
        self.assertTrue(p.is_file(),'source-bound writer checker is missing')
        spec=importlib.util.spec_from_file_location('c06_inventory_checker',p)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_all_executable_writer_sites_match_classified_source(self):
        result=self.checker().audit(NEXT)
        self.assertTrue(result['passed'],result)
        self.assertEqual(result['unclassified'],[])

    def test_missing_writer_binding_is_rejected(self):
        checker=self.checker();inventory=json.loads((NEXT/'contracts/core-writer-inventory.json').read_bytes())
        corrupt=copy.deepcopy(inventory);corrupt['writer_sites'].pop()
        self.assertFalse(checker.audit(NEXT,inventory=corrupt)['passed'])

    def test_new_unclassified_writer_is_rejected(self):
        checker=self.checker();name='src/cortex_core/records.py'
        changed=(NEXT/name).read_text()+"\ndef accidental_writer(connection):\n    connection.execute('DELETE FROM core.records WHERE true')\n"
        result=checker.audit(NEXT,overrides={name:changed})
        self.assertFalse(result['passed']);self.assertTrue(result['unclassified'])

    def test_quoted_qualified_writer_is_rejected(self):
        checker=self.checker();name='src/cortex_core/records.py'
        changed=(NEXT/name).read_text()+"\ndef accidental_writer(connection):\n    connection.execute('DELETE FROM \"core\".\"records\" WHERE true')\n"
        result=checker.audit(NEXT,overrides={name:changed})
        self.assertFalse(result['passed']);self.assertTrue(result['unclassified'])

    def test_unqualified_writer_with_search_path_is_rejected(self):
        checker=self.checker();name='src/cortex_core/records.py'
        changed=(NEXT/name).read_text()+"\ndef accidental_writer(connection):\n    connection.execute('SET search_path=core; DELETE FROM records WHERE true')\n"
        result=checker.audit(NEXT,overrides={name:changed})
        self.assertFalse(result['passed']);self.assertTrue(result['unclassified'])

    def test_composed_sql_writer_is_rejected(self):
        checker=self.checker();name='src/cortex_core/records.py'
        changed=(NEXT/name).read_text()+"\ndef accidental_writer(connection):\n    connection.execute('DELETE ' + 'FROM core.records WHERE true')\n"
        result=checker.audit(NEXT,overrides={name:changed})
        self.assertFalse(result['passed']);self.assertTrue(result['unclassified'])

    def test_assigned_variable_dynamic_writer_fails_closed(self):
        checker=self.checker();name='src/cortex_core/records.py'
        changed=(NEXT/name).read_text()+"\ndef accidental_writer(connection, table):\n    verb = 'DELETE'\n    statement = verb + ' FROM ' + table + ' WHERE true'\n    connection.execute(statement)\n"
        result=checker.audit(NEXT,overrides={name:changed})
        self.assertFalse(result['passed']);self.assertTrue(result['unclassified'])

    def test_helper_return_dynamic_writer_fails_closed(self):
        checker=self.checker();name='src/cortex_core/records.py'
        changed=(NEXT/name).read_text()+"\ndef accidental_statement(table):\n    verb = 'DELETE'\n    return verb + ' FROM ' + table + ' WHERE true'\ndef accidental_writer(connection, table):\n    connection.execute(accidental_statement(table))\n"
        result=checker.audit(NEXT,overrides={name:changed})
        self.assertFalse(result['passed']);self.assertTrue(result['unclassified'])

    def test_dynamic_composed_sql_writer_fails_closed(self):
        checker=self.checker();name='src/cortex_core/records.py'
        changed=(NEXT/name).read_text()+"\ndef accidental_writer(connection, table):\n    connection.execute('DELETE ' + table + ' WHERE true')\n"
        result=checker.audit(NEXT,overrides={name:changed})
        self.assertFalse(result['passed']);self.assertTrue(result['unclassified'])

    def _captured_private_query(self, source):
        function=next(node for node in ast.parse(source).body if isinstance(node,ast.FunctionDef) and node.name=='_private')
        namespace={'psycopg':SimpleNamespace(errors=SimpleNamespace(UniqueViolation=RuntimeError)),
                   'RecordError':RuntimeError}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'isolated-records-private','exec'),namespace)
        class Connection:
            def execute(self,query,arguments):return query
        return namespace['_private'](Connection(),'SELECT * FROM coordination.c05_request(%s,%s)',())

    def test_private_query_walrus_rebind_fails_closed(self):
        checker=self.checker();name='src/cortex_core/records.py'
        source=(NEXT/name).read_text()
        before='    try:\n        return connection.execute(query, arguments)'
        after="    try:\n        verb = 'DE' + 'LETE'\n        if (query := verb + ' FROM core.records WHERE true'):\n            return connection.execute(query, arguments)"
        self.assertEqual(source.count(before),1)
        changed=source.replace(before,after)
        self.assertEqual(self._captured_private_query(changed),'DELETE FROM core.records WHERE true')
        result=checker.audit(NEXT,overrides={name:changed})
        self.assertFalse(result['passed'],result);self.assertTrue(result['unclassified'])

    def test_private_query_augassign_rebind_fails_closed(self):
        checker=self.checker();name='src/cortex_core/records.py'
        source=(NEXT/name).read_text()
        before='    try:\n        return connection.execute(query, arguments)'
        after="    try:\n        verb = 'DE' + 'LETE'\n        query += '; ' + verb + ' FROM core.records WHERE true'\n        return connection.execute(query, arguments)"
        self.assertEqual(source.count(before),1)
        changed=source.replace(before,after)
        self.assertIn('; DELETE FROM core.records WHERE true',self._captured_private_query(changed))
        result=checker.audit(NEXT,overrides={name:changed})
        self.assertFalse(result['passed'],result);self.assertTrue(result['unclassified'])

    def test_private_query_match_capture_fails_closed(self):
        checker=self.checker();name='src/cortex_core/records.py'
        source=(NEXT/name).read_text()
        before='    try:\n        return connection.execute(query, arguments)'
        after="    try:\n        verb = 'DE' + 'LETE'\n        match verb + ' FROM core.records WHERE true':\n            case query:\n                return connection.execute(query, arguments)"
        self.assertEqual(source.count(before),1)
        changed=source.replace(before,after)
        self.assertEqual(self._captured_private_query(changed),'DELETE FROM core.records WHERE true')
        capture=next(n for n in ast.walk(ast.parse(changed)) if isinstance(n,ast.MatchAs) and n.name=='query')
        self.assertIsNotNone(capture)
        result=checker.audit(NEXT,overrides={name:changed})
        self.assertFalse(result['passed'],result);self.assertTrue(result['unclassified'])

    def test_private_query_harmless_statement_fails_closed(self):
        checker=self.checker();name='src/cortex_core/records.py'
        source=(NEXT/name).read_text()
        before='    try:\n        return connection.execute(query, arguments)'
        after='    try:\n        pass\n        return connection.execute(query, arguments)'
        self.assertEqual(source.count(before),1)
        changed=source.replace(before,after)
        self.assertTrue(self._captured_private_query(changed).startswith('SELECT'))
        result=checker.audit(NEXT,overrides={name:changed})
        self.assertFalse(result['passed'],result);self.assertTrue(result['unclassified'])

    def test_private_alias_caller_fails_closed(self):
        checker=self.checker();name='src/cortex_core/records.py'
        source=(NEXT/name).read_text()
        probe="\ndef _probe_writer(connection):\n    helper = _private\n    statement = ''.join(('DE', 'LETE FROM core.records WHERE true'))\n    return helper(connection, statement, ())\n"
        base=ast.parse(source);private=next(n for n in base.body if isinstance(n,ast.FunctionDef) and n.name=='_private')
        helper=ast.parse(probe).body[0]
        namespace={'psycopg':SimpleNamespace(errors=SimpleNamespace(UniqueViolation=RuntimeError)),
                   'RecordError':RuntimeError}
        exec(compile(ast.Module(body=[private,helper],type_ignores=[]),'isolated-alias-probe','exec'),namespace)
        class Connection:
            def execute(self,query,arguments):return query
        self.assertEqual(namespace['_probe_writer'](Connection()),'DELETE FROM core.records WHERE true')
        direct=probe.replace('helper = _private','pass').replace('return helper(connection','return _private(connection')
        self.assertFalse(checker.audit(NEXT,overrides={name:source+direct})['passed'])
        result=checker.audit(NEXT,overrides={name:source+probe})
        self.assertFalse(result['passed'],result);self.assertTrue(result['unclassified'])

    def test_cross_file_private_import_alias_fails_closed(self):
        checker=self.checker();name='src/cortex_core/outbox.py'
        source=(NEXT/name).read_text()
        probe="\nfrom .records import _private as imported_private\ndef accidental_writer(connection):\n    statement = ''.join(('DE', 'LETE FROM core.records WHERE true'))\n    return imported_private(connection, statement, ())\n"
        self.assertTrue(any(isinstance(n,ast.alias) and n.name=='_private' and n.asname=='imported_private'
                            for n in ast.walk(ast.parse(probe))))
        result=checker.audit(NEXT,overrides={name:source+probe})
        self.assertFalse(result['passed'],result);self.assertTrue(result['unclassified'])

    def test_dynamic_name_primitives_fail_closed(self):
        checker=self.checker();name='src/cortex_core/outbox.py';source=(NEXT/name).read_text()
        calls=("getattr(object(), name)","setattr(object(), name, None)","exec(name)",
               "eval(name)","__import__(name)","importlib.import_module(name)")
        for call in calls:
            with self.subTest(call=call):
                changed=source+"\ndef accidental_dynamic_name(name):\n    "+call+"\n"
                result=checker.audit(NEXT,overrides={name:changed})
                self.assertFalse(result['passed'],result);self.assertTrue(result['unclassified'])


    def test_composition_fixture_sites_are_retained_and_source_bound(self):
        checker=self.checker();name='src/cortex_core/runtime/tests/test_installer_composition.py'
        rows=[r for r in checker.scan(NEXT) if r['file']==name]
        self.assertEqual(len(rows),9)
        self.assertEqual({r['table'] for r in rows if r['verb']=='INSERT'},
                         {'core.schema_migrations','core.installations','core.tenants',
                          'core.projects','auth.runtime_login_bindings'})
        self.assertEqual([(owner,len([r for r in rows if r['owner']==owner])) for owner in
                          ('NonsecretSQLFixture.rows','cross_file:apply_migrations','dynamic_name:setattr')],
                         [('NonsecretSQLFixture.rows',5),('cross_file:apply_migrations',2),('dynamic_name:setattr',2)])
        self.assertTrue(all(r['classification'] and 'DATA' in r['classification']
                            and 'no_SQL_execution' in r['classification'] for r in rows),rows)
        self.assertTrue(checker.audit(NEXT)['passed'])

    def test_complete_composed_caller_sources_remain_globally_bound(self):
        checker=self.checker()
        self.assertTrue(checker.audit(NEXT)['passed'])
        for name in ('src/cortex_core/runtime/resources.py',
                     'src/cortex_core/runtime/tests/test_installer_composition.py'):
            with self.subTest(name=name):
                source=(NEXT/name).read_text()
                changed=source+'\n# Unreviewed caller-source change.\n'
                result=checker.audit(NEXT,overrides={name:changed})
                self.assertFalse(result['passed'])
                # Even unchanged seed sites lose their closed classifications:
                # a local per-file bypass must not replace the global gate.
                seed=[r for r in result['unclassified'] if r['file']=='src/cortex_core/schema/seed.py']
                self.assertEqual(len(seed),4)
                self.assertTrue(checker.audit(NEXT)['passed'])

    def test_composition_site_changes_refuse_even_with_renewed_source_pin(self):
        checker=self.checker();name='src/cortex_core/runtime/tests/test_installer_composition.py'
        original=(NEXT/name).read_text()
        mutations=(('INSERT INTO core.schema_migrations (migration_id,sha256)',
                    'INSERT INTO core.unreviewed (migration_id,sha256)'),
                   ('def rows(self, connection, sql, params):',
                    'def unreviewed_rows(self, connection, sql, params):'),
                   ('setattr(self.fixture,fault,True)','setattr(connection,fault,True)'),
                   ('setattr(self.fixture,fault,False)','setattr(connection,fault,False)'))
        self.assertTrue(checker.audit(NEXT)['passed'])
        for before,after in mutations:
            with self.subTest(after=after):
                self.assertEqual(original.count(before),1)
                changed=original.replace(before,after,1)
                with patch.dict(checker.REVIEWED_WRITER_SOURCE_SHA256,
                                {name:hashlib.sha256(changed.encode()).hexdigest()}):
                    result=checker.audit(NEXT,overrides={name:changed})
                    self.assertFalse(result['passed'])
                    self.assertTrue(any(r['file']==name for r in result['unclassified']))
        changed=original+"\ndef accidental_writer(connection):\n    connection.execute('DELETE FROM core.records WHERE true')\n"
        with patch.dict(checker.REVIEWED_WRITER_SOURCE_SHA256,
                        {name:hashlib.sha256(changed.encode()).hexdigest()}):
            result=checker.audit(NEXT,overrides={name:changed})
            self.assertFalse(result['passed'])
            self.assertTrue(any(r['owner']=='accidental_writer' for r in result['unclassified']))

    def test_composition_classifications_do_not_follow_source_to_another_file(self):
        checker=self.checker();name='src/cortex_core/runtime/tests/test_installer_composition.py'
        moved='src/cortex_core/runtime/tests/test_resources.py'
        self.assertTrue(checker.audit(NEXT)['passed'])
        rows=checker.scan_source(moved,(NEXT/name).read_text())+checker.source_reference_flags(moved,(NEXT/name).read_text())
        self.assertEqual(len(rows),9)
        self.assertTrue(all(r['classification'] is None for r in rows))
