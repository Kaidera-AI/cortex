"""C07 accepted three-guard widening, with original C06 bytes retained."""
import hashlib
import json
from pathlib import Path
import re
import sys

wt = Path(sys.argv[1])
ledger = json.loads((wt/'docs/next/evidence/c07-source/frozen-table-guards.json').read_text())
root = wt/'next'
old = ledger['old_migrations']
assert len(old) == 8
for entry in old:
    assert hashlib.sha256((root/'schema'/entry['file']).read_bytes()).hexdigest() == entry['sha256']
manifest = json.loads((root/'schema/manifest.json').read_text())['migrations']
assert manifest[:8] == old and len(manifest) == 9
new_entry = manifest[8]
assert new_entry['id'] == 'module-consumers-0003'
assert hashlib.sha256((root/'schema'/new_entry['file']).read_bytes()).hexdigest() == new_entry['sha256']
names = sorted({name for entry in manifest for name in re.findall(
    r'^CREATE TABLE ([a-z_]+\.[a-z_]+) \(', (root/'schema'/entry['file']).read_text(), re.M)})
assert len(names) == 31 and set(ledger['new_tables']) <= set(names)
expected = '{' + ', '.join(repr(name) for name in names) + '}'
for guard in ledger['guards']:
    path = root/Path(guard['path']).relative_to('next')
    archive = wt/'docs/next/evidence/c07-source/frozen'/(
        Path(guard['archived']).name)
    original = archive.read_text()
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == guard['sha256']
    if path.name == 'test_authorization.py':
        needle = '        self.assertEqual(len(rows),28)\n'
        replacement = '        self.assertEqual(len(rows),31)\n' + \
            "        self.assertEqual({f'{schema}.{table}' for schema,table,*_ in rows}, " + expected + ")\n"
    elif path.name == 'test_identity.py':
        needle = '        self.assertEqual(count, 28)\n'
        replacement = '        self.assertEqual(count, 31)\n' + \
            "        self.assertEqual({f'{schema}.{table}' for schema,table in self.admin.execute(\"SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('core','auth','coordination','retrieval') AND c.relkind='r' AND c.relname<>'schema_migrations'\").fetchall()}, " + expected + ")\n"
    else:
        needle = "        self.assertEqual(self.admin.execute(\"SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('core','auth','coordination','retrieval') AND c.relkind='r' AND c.relname<>'schema_migrations'\").fetchone()[0],28)\n"
        replacement = needle.replace('],28)', '],31)') + \
            "        self.assertEqual({f'{schema}.{table}' for schema,table in self.admin.execute(\"SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('core','auth','coordination','retrieval') AND c.relkind='r' AND c.relname<>'schema_migrations'\").fetchall()}, " + expected + ")\n"
    assert original.count(needle) == 1
    assert path.read_text() == original.replace(needle, replacement)
print(json.dumps({'passed': True, 'guards': len(ledger['guards']), 'original_migrations': len(old),
                  'business_tables': names, 'new_migration_sha256': new_entry['sha256']}))
