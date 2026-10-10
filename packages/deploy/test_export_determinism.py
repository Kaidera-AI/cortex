"""Actual producer main export boundary; fake engine, never native qualification."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
s = importlib.util.spec_from_file_location('export_test_producer', HERE/'build-manual-linux.py')
producer = importlib.util.module_from_spec(s)
s.loader.exec_module(producer)


class ExportBoundary(unittest.TestCase):
    def test_same_image_exports_seconds_apart_have_identical_raw_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'source'
            source.mkdir()
            for context, recipe in producer.ROLES.values():
                p = source/context/recipe
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b'FROM scratch\n')
            tool = root/'podman'
            tool.write_text('#!/bin/sh\nexit 99\n')
            tool.chmod(0o755)
            admission = root/'admission.json'
            admission.write_text(json.dumps({'source_revision': 'a'*40, 'source_accepted': True,
                                            'pipeline_accepted': True, 'decision_id': 'fixture-only'}))
            calls = []

            def engine(argv, **kw):
                calls.append(argv)
                if 'save' in argv:
                    destination = Path(argv[argv.index('--output')+1])
                    kind = argv[argv.index('--format')+1]
                    fixture = root/'oci-fixture'
                    if not fixture.exists():
                        fixture.mkdir()
                        (fixture/'oci-layout').write_bytes(b'{"imageLayoutVersion":"1.0.0"}\n')
                        (fixture/'index.json').write_bytes(b'{"schemaVersion":2,"manifests":[]}\n')
                    # Deliberately varying input clock and creation order, same modes/content.
                    for p in fixture.iterdir():
                        os.utime(p, None)
                    if kind == 'oci-archive':
                        with tarfile.open(destination, 'w', format=tarfile.USTAR_FORMAT) as tar:
                            for p in fixture.iterdir():
                                tar.add(p, arcname=p.name)
                    elif kind == 'oci-dir':
                        shutil.copytree(fixture, destination)
                    else:
                        raise AssertionError(kind)
                return None

            def git(root, *args):
                return 'a'*40 if args == ('rev-parse', 'HEAD') else ''

            exports = []
            for label in ['A', 'B']:
                out = root/label
                argv = ['producer', '--source', str(source), '--source-sha', 'a'*40,
                        '--output', str(out), '--execute', '--admission', str(admission), '--podman', str(tool)]
                with patch.object(sys, 'argv', argv), patch.object(producer, 'native_check'), \
                     patch.object(producer, 'git', side_effect=git), \
                     patch.object(producer, 'normalize_checkout_mtimes', return_value={'fixture_only': True}), \
                     patch.object(producer.subprocess, 'check_output', return_value=b'FROM scratch\n'), \
                     patch.object(producer.subprocess, 'run', side_effect=engine), \
                     patch.object(producer.oci_archive, 'verify', return_value={'manifest_digest': 'sha256:'+'b'*64}):
                    producer.main()
                exports.append(sorted(out.glob('*.oci.tar')))
                if label == 'A':
                    time.sleep(1.1)
            self.assertEqual(len(exports[0]), 7)
            self.assertEqual(len(exports[1]), 7)
            for a, b in zip(*exports):
                self.assertEqual(a.read_bytes(), b.read_bytes(), 'raw export differs: '+a.name)
            self.assertEqual(len([c for c in calls if 'save' in c]), 14)


if __name__ == '__main__':
    unittest.main()
