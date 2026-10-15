"""OI-REFLECT-001 round2: exact Hale getter/local-name witnesses, RED first."""
import ast
import unittest
import test_reflected_writers as witness

QUALIFIED='''
import builtins
def future_writer(connection):
    method_name = "execute"
    callback = builtins.getattr(connection, method_name)
    return callback("".join(("DE", "LETE FROM core.records WHERE true")))
'''
MAPPING='''
def future_writer(connection):
    method_name = "execute"
    callback = __builtins__["getattr"](connection, method_name)
    return callback("".join(("DE", "LETE FROM core.records WHERE true")))
'''


class ReflectionNameAliases(unittest.TestCase):
    checker=witness.ReflectedWriters.checker

    def _writer_refused(self, source):
        namespace={}
        observed=[]
        class Receiver:
            def execute(self, query):
                observed.append(query)
                return query
        exec(compile(ast.parse(source),'<hale-local-getter-witness>','exec'),namespace)
        self.assertEqual(namespace['future_writer'](Receiver()),'DELETE FROM core.records WHERE true')
        self.assertEqual(observed,['DELETE FROM core.records WHERE true'])
        result=self.checker().audit(witness.NEXT,overrides={
            witness.FILE:(witness.NEXT/witness.FILE).read_text()+source})
        self.assertFalse(result['passed'],'OI-REFLECT-001: getter/local-name writer silently passed')
        self.assertTrue(result['unclassified'],result)
        self.assertTrue(all(row['classification'] is None for row in result['unclassified']))
        self.assertGreater(result['writer_count'],86)

    def test_hale_qualified_getter_local_name_refused(self):
        self._writer_refused(QUALIFIED)

    def test_hale_mapping_getter_local_name_refused(self):
        self._writer_refused(MAPPING)

    def test_unresolved_connection_and_cursor_names_refuse_but_literal_metadata_remains_data(self):
        checker=self.checker()
        for getter in ('builtins.getattr', '__builtins__["getattr"]'):
            for receiver in ('connection','cursor'):
                with self.subTest(getter=getter,receiver=receiver):
                    source=f'''import builtins
def writer({receiver},method_name,opaque):
    callback={getter}({receiver},method_name)
    return callback(opaque)
'''
                    self.assertTrue(checker.source_reference_flags(witness.FILE,source))
            metadata=f"import builtins\ndef status(error):\n    return {getter}(error,'code',None)\n"
            self.assertEqual(checker.source_reference_flags(witness.FILE,metadata),[])
