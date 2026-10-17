"""Actual seed has four finite bootstrap writers and closed read inputs only."""
import ast
from contextlib import ExitStack
import hashlib
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
NEXT=Path(__file__).resolve().parents[2]
FILE='src/cortex_core/schema/seed.py'
class CurrentSeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('current_seed_checker',NEXT/'scripts/verify_writer_inventory.py')
        cls.checker=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.checker)
    def source(self):
        self.assertTrue((NEXT/FILE).is_file(),'actual seed must be composed')
        return (NEXT/FILE).read_text()
    def test_actual_bootstrap_writers_are_exact_and_source_bound(self):
        self.source()
        rows=[r for r in self.checker.scan(NEXT) if r['file']==FILE]
        self.assertEqual(len(rows),4)
        self.assertEqual({r['table'] for r in rows},{'core.installations','core.tenants','core.projects','auth.runtime_login_bindings'})
        self.assertTrue(all(r['owner']=='_bind' and r['verb']=='INSERT' and r['classification'] and 'native_HELD' in r['classification'] for r in rows))
        self.assertTrue(self.checker.audit(NEXT)['passed'])
    def test_seed_read_query_or_binding_drift_refuses_even_with_renewed_pins(self):
        original=self.source()
        for before,after in (("SELECT 1 FROM auth.principals","SELECT side_effect() FROM auth.principals"),
                ('connection.execute(sql,','connection.execute(other_sql,'),
                ('for sql in (','for sql in untrusted(')):
            self.assertEqual(original.count(before),1)
            changed=original.replace(before,after,1);tree=ast.parse(changed)
            with ExitStack() as stack:
                stack.enter_context(patch.dict(self.checker.KNOWN_INPUT_FILE_SHA256,{FILE:self.checker.normalized_function_digest(tree)}))
                functions={n.name:n for n in tree.body if isinstance(n,ast.FunctionDef)}
                stack.enter_context(patch.dict(self.checker.KNOWN_INPUT_FUNCTION_SHA256,{(FILE,n):self.checker.normalized_function_digest(f) for n,f in functions.items()}))
                stack.enter_context(patch.dict(self.checker.REVIEWED_WRITER_SOURCE_SHA256,{FILE:hashlib.sha256(changed.encode()).hexdigest()}))
                rows=[r for r in self.checker.scan(NEXT,{FILE:changed}) if r['file']==FILE]
                self.assertTrue(any(r['classification'] is None for r in rows),before)
    def test_new_seed_writer_cannot_be_authorized_by_pin_renewal(self):
        original=self.source();anchor="    return material\n"
        self.assertEqual(original.count(anchor),1)
        changed=original.replace(anchor,"    connection.execute('INSERT INTO core.unreviewed VALUES(1)')\n"+anchor)
        with patch.dict(self.checker.REVIEWED_WRITER_SOURCE_SHA256,{FILE:hashlib.sha256(changed.encode()).hexdigest()}):
            self.assertTrue(any(r['classification'] is None for r in self.checker.scan(NEXT,{FILE:changed}) if r['file']==FILE))
if __name__=='__main__':unittest.main()
