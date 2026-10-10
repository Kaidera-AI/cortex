import json
from pathlib import Path
import tempfile
import types
import unittest
import deterministic_oci_export as exporter


class WriterTests(unittest.TestCase):
    def fixture(self, root):
        root.mkdir()
        (root/'z-directory').mkdir(mode=0o750)
        (root/'z-directory/payload').write_bytes(b'unchanged OCI bytes\n')
        (root/'z-directory/payload').chmod(0o640)
        (root/'oci-layout').write_text('{"imageLayoutVersion":"1.0.0"}\n')
        (root/'index.json').write_text('{"schemaVersion":2,"manifests":[]}\n')

    def test_roundtrip_preserves_bytes_and_modes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            self.fixture(p/'oci')
            result = exporter.write_archive(p/'oci', p/'archive.tar')
            self.assertEqual(result['result'], 'PASS')
            payload = next(x for x in result['members'] if x['name'] == 'z-directory/payload')
            self.assertEqual(payload['mode'], 0o640)
            self.assertEqual(payload['size'], 20)
            with self.assertRaises(FileExistsError):
                exporter.write_archive(p/'oci', p/'archive.tar')

    def test_wall_clock_and_unsorted_writer_mutants_refuse(self):
        source = Path(exporter.__file__).read_text()
        for label, old, new in [('wall-clock', 'member.mtime = EPOCH', 'member.mtime = int(__import__("time").time())'),
                                ('unsorted', 'for row in rows:', 'for row in reversed(rows):')]:
            with self.subTest(mutant=label), tempfile.TemporaryDirectory() as d:
                p = Path(d)
                self.fixture(p/'oci')
                mutated = types.ModuleType('mutated_exporter')
                self.assertEqual(source.count(old), 1)
                exec(compile(source.replace(old, new), 'mutant-'+label, 'exec'), mutated.__dict__)
                with self.assertRaisesRegex(ValueError, 'outer header|order/members'):
                    mutated.write_archive(p/'oci', p/'mutant.tar')

    def test_symlink_and_corrupted_blob_refuse(self):
        for label in ['symlink', 'blob']:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as d:
                p = Path(d)
                self.fixture(p/'oci')
                if label == 'symlink':
                    (p/'oci/link').symlink_to('index.json')
                else:
                    blob = p/'oci/blobs/sha256'/('0'*64)
                    blob.parent.mkdir(parents=True)
                    blob.write_bytes(b'wrong digest')
                with self.assertRaises(ValueError):
                    exporter.write_archive(p/'oci', p/'archive.tar')
                self.assertFalse((p/'archive.tar').exists())


if __name__ == '__main__':
    unittest.main()
