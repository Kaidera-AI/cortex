"""Controls for reviewed indirect inputs; source-only, no runtime imports."""
import importlib.util
from pathlib import Path
import unittest

NEXT = Path(__file__).resolve().parents[2]
BACKUP = 'src/cortex_core/backup_validation.py'
MIGRATIONS = 'src/cortex_core/migrations.py'
REFLECTION = {
    'src/cortex_core/release/artifacts.py': ('_compiled_fields_equal', 'getattr(actual, field)', "'co_cellvars'"),
    'src/cortex_core/release/build/audit.py': ('_code_equal', 'getattr(left, k)', '"co_cellvars"'),
    'src/vector_baseline/product_path.py': ('snapshot_corpus', 'getattr(staged, name)', '"weights"'),
}


class SourceRecognition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('reviewed_inventory_checker', NEXT/'scripts/verify_writer_inventory.py')
        cls.checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.checker)

    def source(self, file):
        return (NEXT/file).read_text()

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

    def test_updated_records_module_retains_private_read_recognition(self):
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


if __name__ == '__main__':
    unittest.main()
