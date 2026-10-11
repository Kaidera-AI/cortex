"""Fail-capable review repro: unchanged old API caches must not pass new source overlay.

Usage: python3 this-file.py <clean Cortex checkout at PR #105 head>
Expected at reviewed head 53c8dc8: one test-body failure, ValueError not raised.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

checkout = Path(sys.argv.pop(1)).resolve()
sys.path[:0] = [str(checkout / 'packages/deploy/tests'), str(checkout / 'packages/deploy')]
from test_api_source_predicates import fixture, caches, OLD, NEW
from test_api_source_wrapper import archive
import api_source_predicates as predicate
import verify_api_signed_reference as wrapper


class StaleUnchangedAPICache(unittest.TestCase):
    def test_old_cache_cannot_survive_approved_source_change(self):
        f = fixture()
        caches(f)
        stale = [
            'app/__pycache__/db_tls.cpython-314.pyc',
            'app/__pycache__/main.cpython-314.pyc',
        ]
        for path in stale:
            f[3][path] = f[2][path]
            f[1][path] = copy.deepcopy(f[0][path])
        for side, revision in ((0, OLD), (1, NEW)):
            raw = json.dumps({'source_revision': revision}).encode()
            f[side + 2]['app/release_identity.json'] = raw
            f[side]['app/release_identity.json'] = {
                'type': '0', 'mode': 420, 'uid': 0, 'gid': 0, 'xattrs': {},
                'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            }
        with tempfile.TemporaryDirectory(prefix='vera-pr105-stale-', dir='/private/tmp') as tmp:
            old, new = Path(tmp) / 'old.tar', Path(tmp) / 'new.tar'
            archive(old, f[2], f[0], OLD, 100)
            archive(new, f[3], f[1], NEW, 200)
            with self.assertRaises(ValueError):
                wrapper.verify(old, new, OLD, NEW, {
                    'old_source_revision': OLD, 'new_source_revision': NEW,
                })


if __name__ == '__main__':
    unittest.main()
