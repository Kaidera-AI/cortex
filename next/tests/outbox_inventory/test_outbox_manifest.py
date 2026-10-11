"""Fail on a missing additive entry before dereferencing it."""
import hashlib
import json
from pathlib import Path
import unittest


class OutboxManifest(unittest.TestCase):
    def test_manifest_binds_exact_additive_outbox_sql(self):
        root=Path(__file__).resolve().parents[2]
        manifest=json.loads((root/'schema/manifest.json').read_bytes())
        rows=[m for m in manifest['migrations'] if m['id']=='outbox-0002']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['file'],'coordination/002-outbox.sql')
        self.assertEqual(rows[0]['sha256'],hashlib.sha256((root/'schema'/rows[0]['file']).read_bytes()).hexdigest())
