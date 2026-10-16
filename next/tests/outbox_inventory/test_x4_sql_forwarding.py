"""X4 SQL forwarding remains visible to the one static inventory."""
import importlib.util
from pathlib import Path
import unittest
class Forwarding(unittest.TestCase):
    def scanner(self):
        p=Path(__file__).resolve().parents[2]/'scripts/verify_writer_inventory.py'
        s=importlib.util.spec_from_file_location('x4_inventory',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
    def test_guarded_indirect_sql_is_fail_closed(self):
        m=self.scanner()
        for source in ('def work(self,conn,query):\n self._execute(conn,query)\n',
                       'def work(query):\n execute(query)\n'):
            with self.subTest(source=source):
                rows=m.scan_source('src/cortex_core/runtime/role_grants.py',source)
                self.assertTrue(any(r['verb']=='DYNAMIC' and r['classification'] is None for r in rows),'X4_SQL_FORWARDING_INVISIBLE')
    def test_static_guarded_write_stays_visible(self):
        rows=self.scanner().scan_source('unknown.py',"def work(self,conn):\n self._execute(conn,'INSERT INTO core.records VALUES (1)')\n")
        self.assertEqual([(r['verb'],r['table']) for r in rows],[('INSERT','core.records')])
