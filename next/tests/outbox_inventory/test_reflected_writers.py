"""OI-REFLECT-001: Hale's exact callable witness, frozen before walker repair."""
import ast
import importlib.util
from pathlib import Path
import unittest

NEXT=Path(__file__).resolve().parents[2]
FILE='src/cortex_core/outbox.py'  # unpinned module; no incidental pin refusal
HALE='''
def future_writer(connection):
    return getattr(connection, "execute")(
        "".join(("DE", "LETE FROM core.records WHERE true"))
    )
'''


class ReflectedWriters(unittest.TestCase):
    def checker(self):
        spec=importlib.util.spec_from_file_location('reflected_writer_checker',NEXT/'scripts/verify_writer_inventory.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def unresolved(self,source):
        return self.checker().source_reference_flags(FILE,source)

    def test_hale_future_reflected_writer_is_unclassified(self):
        # Prove valid callable data and real writer intent without a database.
        # The only receiver is this local observation double; no product import.
        observed=[]
        class Connection:
            def execute(self,query):observed.append(query);return 'synthetic-executed'
        namespace={}
        exec(compile(ast.parse(HALE),'<hale-reflection-witness>','exec'),namespace)
        self.assertEqual(namespace['future_writer'](Connection()),'synthetic-executed')
        self.assertEqual(observed,['DELETE FROM core.records WHERE true'])
        result=self.checker().audit(NEXT,overrides={FILE:(NEXT/FILE).read_text()+HALE})
        self.assertFalse(result['passed'],'OI-REFLECT-001:future reflected execute silently passed')
        self.assertTrue(result['unclassified'],result)
        self.assertTrue(all(row['classification'] is None for row in result['unclassified']))
        self.assertGreater(result['writer_count'],86)

    def test_literal_reflected_callable_targets_fail_without_sql_fragments(self):
        for target in ('execute','executemany','unknown_callable'):
            with self.subTest(target=target):
                self.assertTrue(self.unresolved(f'def writer(receiver,opaque):\n    return getattr(receiver,{target!r})(opaque)\n'))

    def test_literal_sql_getters_cannot_escape_through_local_alias(self):
        for target in ('execute','executemany'):
            with self.subTest(target=target):
                source=f'def writer(receiver,opaque):\n    callback=getattr(receiver,{target!r})\n    return callback(opaque)\n'
                self.assertTrue(self.unresolved(source))

    def test_folded_sql_method_name_is_not_a_static_name_exemption(self):
        self.assertTrue(self.unresolved("def writer(receiver,opaque):\n    callback=getattr(receiver,'ex'+'ecute')\n    return callback(opaque)\n"))

    def test_reflection_builtin_alias_and_rebinding_fail_closed(self):
        for source in (
            "lookup=getattr\ndef writer(receiver,opaque):\n    callback=lookup(receiver,'execute')\n    return callback(opaque)\n",
            "getattr=replacement\ndef writer(receiver,opaque):\n    return getattr(receiver,'execute')(opaque)\n",
        ):
            with self.subTest(source=source):self.assertTrue(self.unresolved(source))

    def test_unreviewed_higher_order_reflection_target_fails_closed(self):
        self.assertTrue(self.unresolved("def writer(receiver,opaque):\n    return method_factory('execute')(receiver)(opaque)\n"))

    def test_ordinary_noninvoked_literal_metadata_reads_stay_data(self):
        self.assertEqual(self.unresolved("def status(error):\n    return getattr(error,'code',None)\n"),[])


if __name__=='__main__':unittest.main()
