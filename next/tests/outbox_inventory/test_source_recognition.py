"""Controls for reviewed indirect inputs; source-only, no runtime imports."""
import importlib.util
import ast
import hashlib
from pathlib import Path
import unittest
import sys
from contextlib import ExitStack
from unittest.mock import patch

NEXT = Path(__file__).resolve().parents[2]
BACKUP = 'src/cortex_core/backup_validation.py'
MIGRATIONS = 'src/cortex_core/migrations.py'
CODEC = 'src/cortex_core/release/codec.py'
WRAPPER = 'src/cortex_core/schema/migrate.py'
WRAPPER_TEST = 'src/cortex_core/schema/tests/test_readiness.py'
PROFILE = NEXT/'contracts/core-writer-inventory.json'
REFLECTION = {
    'src/cortex_core/release/_pyc_check.py': ('_compiled_fields_equal', 'getattr(actual, field)', "'co_cellvars'"),
    'src/cortex_core/release/build/audit.py': ('_code_equal', 'getattr(left, k)', '"co_cellvars"'),
    'src/vector_baseline/product_path.py': ('snapshot_corpus', 'getattr(staged, name)', '"weights"'),
}


class SourceRecognition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('reviewed_inventory_checker', NEXT/'scripts/verify_writer_inventory.py')
        cls.checker = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.checker
        spec.loader.exec_module(cls.checker)

    def test_day2_catalog_fixed_read_inputs_are_recognized(self):
        file='src/cortex_core/runtime/catalog.py'
        self.assertEqual(self.unresolved(file,self.source(file)),[])

    def test_day2_catalog_query_and_helper_rebinding_refuse_even_with_new_pins(self):
        file='src/cortex_core/runtime/catalog.py'
        for before,after in (("SELECT datname,oid::bigint,datdba::bigint","SELECT dangerous_side_effect(),oid::bigint,datdba::bigint"),
                             ("connection.execute(sql,parameters)","connection.execute(other_sql,parameters)"),
                             ("databases=_rows(connection,", "databases=untrusted(connection,")):
            source=self.changed(file,before,after)
            with self.renew_all(file,source):
                self.assertTrue(self.unresolved(file,source),before)
        for extra in ('\n_rows = untrusted\n','\nrows_alias = _rows\n'):
            self.assertTrue(self.unresolved(file,self.source(file)+extra))

    def source(self, file):
        return (NEXT/file).read_text()

    def test_ast_pins_preserve_the_original_empty_field_representation(self):
        canonical = 'Module(body=[Pass()], type_ignores=[])'
        self.assertEqual(self.checker.normalized_function_digest(ast.parse('pass')),
                         hashlib.sha256(canonical.encode()).hexdigest())

    def changed(self, file, before, after):
        source = self.source(file)
        self.assertEqual(source.count(before), 1, (file, before))
        return source.replace(before, after)

    def unresolved(self, file, source):
        rows = self.checker.scan_source(file, source) + self.checker.source_reference_flags(file, source)
        return [row for row in rows if row['classification'] is None]

    def test_additive_capture_and_installer_writers_are_classified(self):
        for file, count in [('schema/auth/004-first-identity-capture.sql', 5),
                            ('schema/auth/005-first-identity.sql', 5),
                            ('schema/coordination/005-session-source.sql', 1)]:
            with self.subTest(file=file):
                rows = self.checker.scan_source(file, self.source(file))
                self.assertEqual(len(rows), count)
                self.assertTrue(all(row['classification'] for row in rows), rows)

    def test_new_sql_classifications_are_exact_owner_verb_target(self):
        for file in ['schema/auth/004-first-identity-capture.sql',
                     'schema/auth/005-first-identity.sql',
                     'schema/coordination/005-session-source.sql']:
            for row in self.checker.scan_source(file, self.source(file)):
                for owner, verb, table in [('unreviewed.writer', row['verb'], row['table']),
                                            (row['owner'], 'DELETE', row['table']),
                                            (row['owner'], row['verb'], 'core.unreviewed')]:
                    with self.subTest(file=file, owner=owner, verb=verb, table=table):
                        self.assertIsNone(self.checker.category(file, owner, verb, table))

    def test_backup_named_snapshot_select_is_recognized(self):
        self.assertEqual(self.unresolved(BACKUP, self.source(BACKUP)), [])

    def test_backup_snapshot_rebinding_and_sql_changes_fail_closed(self):
        for before, after in [
            ('connection.execute(SNAPSHOT_SQL)', 'connection.execute(other_sql)'),
            ('def read_restored_snapshot(connection):', 'def read_restored_snapshot(connection, SNAPSHOT_SQL):'),
            ('SELECT CASE WHEN pg_is_in_recovery()', 'SELECT dangerous_side_effect(), CASE WHEN pg_is_in_recovery()'),
            ('row = connection.execute(SNAPSHOT_SQL)', "SNAPSHOT_SQL = ''.join(('DE', 'LETE FROM core.records'))\n    row = connection.execute(SNAPSHOT_SQL)"),
        ]:
            with self.subTest(before=before):
                self.assertTrue(self.unresolved(BACKUP, self.changed(BACKUP, before, after)))
        source = self.source(BACKUP)
        for extra in ["\nSNAPSHOT_SQL = other_query\n", "\nread_alias = read_restored_snapshot\n",
                      "\ndef sneaky(connection, query):\n    return connection.execute(query)\n"]:
            with self.subTest(extra=extra):
                self.assertTrue(self.unresolved(BACKUP, source+extra))

    def test_u13_manifest_verified_prepared_prefix_is_recognized(self):
        self.assertEqual(self.unresolved(MIGRATIONS, self.source(MIGRATIONS)), [])

    def test_u13_prefix_provenance_or_ledger_changes_fail_closed(self):
        for before, after in [
            ('prepared[:prepared_target + 1]:', 'prepared[prepared_target:]:'),
            ('prepared[:prepared_target + 1]:', 'untrusted[:prepared_target + 1]:'),
            ('hashlib.sha256(data).hexdigest() != entry["sha256"]', 'False'),
            ('ledger_ids != {item[0] for item in prepared[:len(ledger_ids)]}', 'False'),
            ('connection.execute(sql, prepare=False)', "sql = replacement\n            connection.execute(sql, prepare=False)"),
        ]:
            with self.subTest(before=before):
                self.assertTrue(self.unresolved(MIGRATIONS, self.changed(MIGRATIONS, before, after)))
        self.assertTrue(self.unresolved(MIGRATIONS, self.source(MIGRATIONS)+'\napply_alias = apply_migrations\n'))

    def test_updated_records_module_retains_private_select_recognition(self):
        file = 'src/cortex_core/records.py'
        self.assertEqual(self.unresolved(file, self.source(file)), [])

    def test_fixed_field_reflection_is_recognized(self):
        for file in REFLECTION:
            with self.subTest(file=file):
                self.assertEqual(self.checker.source_reference_flags(file, self.source(file)), [])

    def test_reflection_field_set_object_and_owner_changes_fail_closed(self):
        for file, (function, call, last_field) in REFLECTION.items():
            for before, after in [(call, call.replace(call.split(', ')[-1][:-1], 'untrusted_name')),
                                  (call, call.replace(call.split('(')[1].split(',')[0], 'untrusted_object')),
                                  (last_field, last_field+', "execute"'),
                                  ('def '+function+'(', 'def unreviewed_function(')]:
                with self.subTest(file=file, before=before, after=after):
                    self.assertTrue(self.checker.source_reference_flags(file, self.changed(file, before, after)))

    def test_reflection_module_alias_rebinding_and_extra_calls_fail_closed(self):
        for file in REFLECTION:
            for extra in ['\ngetattr = untrusted_getter\n',
                          '\ndef unrelated(obj, name):\n    return getattr(obj, name)\n',
                          '\nlookup = getattr\n']:
                with self.subTest(file=file, extra=extra):
                    self.assertTrue(self.checker.source_reference_flags(file, self.source(file)+extra))

    def test_reflection_exception_cannot_move_to_another_file(self):
        for file in REFLECTION:
            with self.subTest(file=file):
                self.assertTrue(self.checker.source_reference_flags('src/cortex_core/unreviewed.py', self.source(file)))

    def test_snapshot_exception_does_not_exempt_external_named_sql(self):
        source = 'SNAPSHOT_SQL = "SELECT 1"\ndef read_restored_snapshot(connection):\n    return connection.execute(SNAPSHOT_SQL)\n'
        self.assertTrue(self.unresolved('src/cortex_core/unreviewed.py', source))

    def renewed_pins(self, file, function, source):
        tree = ast.parse(source)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
        return (patch.dict(self.checker.KNOWN_INPUT_FILE_SHA256,
                           {file: self.checker.normalized_function_digest(tree)}),
                patch.dict(self.checker.KNOWN_INPUT_FUNCTION_SHA256,
                           {(file, function): self.checker.normalized_function_digest(node)}))

    def test_pins_alone_cannot_approve_different_reflection_fields_or_objects(self):
        for file, (function, call, last_field) in REFLECTION.items():
            for before, after in [(last_field, last_field+', "execute"'),
                                  (call, call.replace(call.split('(')[1].split(',')[0], 'untrusted_object'))]:
                with self.subTest(file=file, after=after):
                    source = self.changed(file, before, after)
                    module_pin, function_pin = self.renewed_pins(file, function, source)
                    with module_pin, function_pin:
                        self.assertTrue(self.checker.source_reference_flags(file, source))

    def test_pins_alone_cannot_approve_different_migration_slice(self):
        source = self.changed(MIGRATIONS, 'prepared[:prepared_target + 1]:', 'prepared[prepared_target:]:')
        module_pin, function_pin = self.renewed_pins(MIGRATIONS, 'apply_migrations', source)
        with module_pin, function_pin:
            self.assertTrue(self.unresolved(MIGRATIONS, source))


    def renew_all(self, file, source):
        tree = ast.parse(source)
        stack = ExitStack()
        stack.enter_context(patch.dict(self.checker.KNOWN_INPUT_FILE_SHA256,
                                       {file: self.checker.normalized_function_digest(tree)}))
        def visit(nodes, prefix=''):
            for node in nodes:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    owner = prefix + node.name
                    if not isinstance(node, ast.ClassDef):
                        stack.enter_context(patch.dict(self.checker.KNOWN_INPUT_FUNCTION_SHA256,
                            {(file, owner): self.checker.normalized_function_digest(node)}))
                    visit(node.body, owner + '.')
        visit(tree.body)
        return stack

    def test_day2_lazy_validator_and_schema_forwarder_are_recognized(self):
        for file in (CODEC, WRAPPER, WRAPPER_TEST):
            with self.subTest(file=file):
                self.assertEqual(self.unresolved(file, self.source(file)), [])

    def test_day2_inventory_is_source_bound(self):
        self.assertTrue(self.checker.audit(NEXT)['passed'])

    def test_day2_migration_helper_provenance_even_with_renewed_pins(self):
        for before, after in [
            ('prepared = prepare_migrations(directory)', 'prepared = untrusted(directory)'),
            ('prepared = prepare_migrations(directory)', 'prepared = prepare_migrations(other_directory)'),
            ('hashlib.sha256(data).hexdigest() != entry["sha256"]', 'False'),
            ('prepared[:prepared_target + 1]:', 'prepared[prepared_target:]:'),
            ('data.decode("utf-8")', 'untrusted_sql'),
            ('return prepared', 'return untrusted'),
        ]:
            with self.subTest(after=after):
                changed = self.changed(MIGRATIONS, before, after)
                with self.renew_all(MIGRATIONS, changed):
                    self.assertTrue(self.unresolved(MIGRATIONS, changed))

    def test_day2_validator_factory_is_not_a_general_call_exception(self):
        for before, after in [
            ('_validator_class()(schema)', 'other_factory()(schema)'),
            ('_validator_class()(schema)', '_validator_class()(other_schema)'),
            ('_validator_class()(schema)', '_validator_class()(schema, extra)'),
            ('value = extend(Draft202012Validator,', 'value = untrusted(Draft202012Validator,'),
            ("'maxLength': _byte_length", "'maxLength': untrusted"),
            ('return value\n\n\ndef __getattr__', 'return untrusted\n\n\ndef __getattr__'),
        ]:
            with self.subTest(after=after):
                changed = self.changed(CODEC, before, after)
                with self.renew_all(CODEC, changed):
                    self.assertTrue(self.unresolved(CODEC, changed))

    def test_day2_forwarder_and_test_reference_are_exact(self):
        for file, before, after in [
            (WRAPPER, 'migrations.apply_migrations(connection, directory)',
             'migrations.apply_migrations(connection, untrusted)'),
            (WRAPPER, 'migrations.apply_migrations(connection, directory)',
             'other.apply_migrations(connection, directory)'),
            (WRAPPER_TEST, "patch.object(migrations,'apply_migrations',",
             "patch.object(other,'apply_migrations',"),
        ]:
            with self.subTest(file=file, after=after):
                changed = self.changed(file, before, after)
                with self.renew_all(file, changed):
                    self.assertTrue(self.unresolved(file, changed))

    def test_day2_added_aliases_calls_and_file_moves_fail_closed(self):
        for file in (MIGRATIONS, CODEC, WRAPPER, WRAPPER_TEST):
            extra = ("\napply_alias = apply_migrations\n" if file == MIGRATIONS else
                     "\ndef unreviewed(obj, name):\n    return getattr(obj, name)\n")
            with self.subTest(file=file):
                self.assertTrue(self.unresolved(file, self.source(file) + extra))
                self.assertTrue(self.unresolved('src/cortex_core/unreviewed.py', self.source(file)))

    def test_day2_renewed_pins_cannot_approve_module_binding_replacement(self):
        for file, extra in [
            (MIGRATIONS, '\nprepare_migrations = untrusted\n'),
            (CODEC, '\n_validator_class = untrusted\n'),
            (WRAPPER, '\nmigrations = untrusted\n'),
        ]:
            with self.subTest(file=file):
                changed = self.source(file) + extra
                with self.renew_all(file, changed):
                    self.assertTrue(self.unresolved(file, changed))


if __name__ == '__main__':
    unittest.main()
