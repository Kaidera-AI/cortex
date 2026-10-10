import datetime
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import native_argv_preflight as preflight


def fixture(path, label):
    layer = io.BytesIO()
    body = b'H-D449 native argv fixture\n'
    with tarfile.open(fileobj=layer, mode='w') as tar:
        member = tarfile.TarInfo('preflight-input')
        member.size, member.mode, member.mtime = len(body), 0o644, preflight.EPOCH
        tar.addfile(member, io.BytesIO(body))
    clock = datetime.datetime.fromtimestamp(preflight.EPOCH, datetime.timezone.utc).isoformat()
    config = {'created': clock, 'history': [{'created': clock}], 'config': {'Labels': {}}}
    if label:
        config['config']['Labels']['io.buildah.version'] = '1.43.1'
    blobs = {}
    def blob(raw):
        h = hashlib.sha256(raw).hexdigest()
        blobs['blobs/sha256/'+h] = raw
        return {'digest': 'sha256:'+h, 'size': len(raw)}
    config_ref = blob(json.dumps(config).encode())
    layer_ref = blob(layer.getvalue())
    manifest_ref = blob(json.dumps({'config': config_ref, 'layers': [layer_ref]}).encode())
    with tarfile.open(path, 'w') as tar:
        for name, raw in {**blobs, 'index.json': json.dumps({'manifests': [manifest_ref]}).encode()}.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            tar.addfile(member, io.BytesIO(raw))


class NativeIdentityInspector(unittest.TestCase):
    def test_actual_OCI_config_without_builder_label_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d)/'missing.oci.tar'
            fixture(archive, False)
            with self.assertRaisesRegex(ValueError, 'identity label'):
                preflight.inspect_archive(archive)

    def test_actual_OCI_config_with_label_keeps_clock_COPY_proof(self):
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d)/'present.oci.tar'
            fixture(archive, True)
            self.assertTrue(preflight.inspect_archive(archive))


if __name__ == '__main__':
    unittest.main()
