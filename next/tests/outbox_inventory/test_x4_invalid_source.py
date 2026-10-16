"""Malformed SQL-provider source must remain an explicit unresolved site."""
import importlib.util
from pathlib import Path
import unittest
class InvalidSource(unittest.TestCase):
 def test_unparseable_source_is_explicitly_unclassified(self):
  p=Path(__file__).resolve().parents[2]/'scripts/verify_writer_inventory.py'
  s=importlib.util.spec_from_file_location('invalid_inventory',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
  source='def unsafe(connection):\n    if True:\n  connection.execute(query)\n'
  for scan in (m.scan_source,m.source_reference_flags):
   with self.subTest(scanner=scan.__name__):
    try:rows=scan('src/cortex_core/migrations.py',source)
    except SyntaxError:self.fail('X4_INVALID_SOURCE_NOT_RETAINED')
    self.assertTrue(rows)
    self.assertTrue(all(row['classification'] is None for row in rows))
