"""Source-bound inventory artifact checks, never application qualification."""
import ast
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
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
