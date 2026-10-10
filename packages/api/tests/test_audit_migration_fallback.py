"""Frozen audit existence gate; actual source body, no API import/startup/egress."""
import ast,copy,os,unittest
from pathlib import Path
from unittest.mock import patch
WT=Path(__file__).resolve().parents[3]
SOURCE=WT/'packages/api/main.py'
def namespace():
    tree=ast.parse(SOURCE.read_text())
    node=copy.deepcopy(next(x for x in tree.body if getattr(x,'name','')=='configured_schema_migrations_dir'))
    node.decorator_list=[]
    ns={'__file__':str(SOURCE),'Path':Path,'os':os}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),str(SOURCE),'exec'),ns)
    return ns

class ActualAPIControls(unittest.IsolatedAsyncioTestCase):
    async def test_direct_source_migration_fallback_exists(self):
            ns=namespace()
            with patch.dict(os.environ,{'CORTEX_MIGRATIONS_DIR':str(WT/'does-not-exist')},clear=False):
                actual=ns['configured_schema_migrations_dir']()
            self.assertTrue((WT/'packages/schema/migrations').is_dir())
            self.assertTrue(actual.is_dir(),f'direct source fallback points to absent directory {actual}')

    async def test_existing_configured_mount_keeps_precedence(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            ns=namespace()
            with patch.dict(os.environ,{'CORTEX_MIGRATIONS_DIR':directory},clear=False):
                actual=ns['configured_schema_migrations_dir']()
            self.assertEqual(actual,Path(directory))

    async def test_fallback_points_to_owned_sql_inputs(self):
        ns=namespace()
        with patch.dict(os.environ,{'CORTEX_MIGRATIONS_DIR':str(WT/'does-not-exist')},clear=False):
            actual=ns['configured_schema_migrations_dir']()
        self.assertEqual(actual,WT/'packages/schema/migrations')
        self.assertTrue(list(actual.glob('*.sql')))
