"""Exact static data classification; no seed, SQL, native or installed execution."""
import hashlib
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

NEXT=Path(__file__).resolve().parents[2]
FILE='src/cortex_core/schema/tests/test_seed.py'


class SeedFixtureRecognition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('seed_fixture_writer_checker',NEXT/'scripts/verify_writer_inventory.py')
        cls.checker=importlib.util.module_from_spec(spec)
        sys.modules[spec.name]=cls.checker
        spec.loader.exec_module(cls.checker)

    def test_reviewed_model_data_is_explicit_and_source_bound(self):
        rows=[r for r in self.checker.scan(NEXT) if r['file']==FILE]
        self.assertEqual(len(rows),2)
        unclassified=[r for r in rows if r['classification'] is None]
        self.assertEqual(unclassified, [])
        self.assertEqual({r['owner'] for r in rows},{'_SeedConnection.execute'})
        self.assertTrue(all('DATA' in r['classification'] and 'no_SQL_execution' in r['classification'] for r in rows))
        self.assertTrue(self.checker.audit(NEXT)['passed'])

    def test_new_sql_or_model_bytes_refuse_even_with_renewed_file_pin(self):
        original=(NEXT/FILE).read_text()
        anchor="        self.db.events.append(('sql', self.purpose, text, params))"
        self.assertEqual(original.count(anchor),1)
        for before,after in ((anchor,anchor+"\n        connection.execute('INSERT INTO core.unreviewed VALUES (1)')"),
                            ("r'insert into ([a-z_.]+)","r'insert into ([a-z0-9_.]+)"),
                            ("def execute(self, sql, params=None):","def unreviewed_execute(self, sql, params=None):")):
            self.assertEqual(original.count(before),1)
            changed=original.replace(before,after,1)
            with patch.dict(self.checker.REVIEWED_WRITER_SOURCE_SHA256,{FILE:hashlib.sha256(changed.encode()).hexdigest()}):
                rows=[r for r in self.checker.scan(NEXT,overrides={FILE:changed}) if r['file']==FILE]
                self.assertTrue(any(r['classification'] is None for r in rows),before)
                self.assertFalse(self.checker.audit(NEXT,overrides={FILE:changed})['passed'])


if __name__=='__main__':unittest.main()
