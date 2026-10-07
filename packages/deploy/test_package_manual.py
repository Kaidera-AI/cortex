"""Public-source payload identity must replace the old private projection pin."""
import importlib.util
from pathlib import Path
import unittest
import json
import tempfile

SPEC=importlib.util.spec_from_file_location('manual_package',Path(__file__).with_name('package-manual.py'))
module=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(module)

class PublicPackageTests(unittest.TestCase):
    def test_source_revision_and_migrations_determine_identity(self):
        files={'packages/schema/migrations/001.sql':(b'SELECT 1;',0o644)}
        identity=module.payload_identity(files,'a'*40)
        self.assertEqual(identity['version'],'0.1.003-manual.1')
        self.assertEqual(identity['source_revision'],'a'*40)
        changed=module.payload_identity({'packages/schema/migrations/001.sql':(b'SELECT 2;',0o644)},'a'*40)
        self.assertNotEqual(identity['schema_revision'],changed['schema_revision'])

    def test_missing_migration_refuses(self):
        with self.assertRaises(ValueError):module.payload_identity({},'a'*40)

    def test_untyped_provenance_refuses_as_validation_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            (root/'PROJECTION_MANIFEST.json').write_text(json.dumps({'schema':'cortex.projection_manifest.v1','source_repo':[], 'source_provenance':'x'}))
            with self.assertRaises(ValueError):module.packager.snapshot(root)

    def test_alias_revision_refuses(self):
        with self.assertRaises(ValueError):module.payload_identity({'packages/schema/migrations/001.sql':(b'X',0o644)},'main')

if __name__=='__main__':unittest.main()
