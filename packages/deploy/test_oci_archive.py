"""Native exported OCI blobs must verify without adding host packages."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import oci_archive

class ArchiveTests(unittest.TestCase):
    def archive(self, path, *, corrupt=False, architecture='amd64', duplicate=False):
        blobs = {}
        def blob(value):
            raw = json.dumps(value).encode() if isinstance(value, dict) else value
            digest = hashlib.sha256(raw).hexdigest()
            blobs['blobs/sha256/'+digest] = raw
            return {'digest':'sha256:'+digest,'size':len(raw)}
        config = blob({'os':'linux','architecture':architecture,'config':{'Labels':{
            'org.opencontainers.image.source':'https://github.com/Kaidera-AI/cortex',
            'org.opencontainers.image.version':'0.1.003-manual.1',
            'org.opencontainers.image.revision':'a'*40}}})
        layer = blob(b'test layer')
        manifest = blob({'schemaVersion':2,'mediaType':'application/vnd.oci.image.manifest.v1+json',
                         'config':config,'layers':[layer]})
        blobs['oci-layout'] = b'{"imageLayoutVersion":"1.0.0"}'
        blobs['index.json'] = json.dumps({'schemaVersion':2,'manifests':[manifest]}).encode()
        if corrupt: blobs['blobs/sha256/'+layer['digest'][7:]] = b'corrupt!!!'
        with tarfile.open(path,'w') as tar:
            for name,raw in blobs.items():
                member=tarfile.TarInfo(name);member.size=len(raw);tar.addfile(member,io.BytesIO(raw))
            if duplicate:
                member=tarfile.TarInfo('index.json');member.size=len(blobs['index.json']);tar.addfile(member,io.BytesIO(blobs['index.json']))
        return manifest['digest']

    def test_every_blob_and_native_identity_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'image.tar'; digest=self.archive(p)
            self.assertEqual(oci_archive.verify(p,'a'*40)['manifest_digest'],digest)

    def test_corrupt_layer_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'image.tar';self.archive(p,corrupt=True)
            with self.assertRaises(ValueError):oci_archive.verify(p,'a'*40)

    def test_wrong_architecture_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'image.tar';self.archive(p,architecture='arm64')
            with self.assertRaises(ValueError):oci_archive.verify(p,'a'*40)

    def test_duplicate_members_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'image.tar';self.archive(p,duplicate=True)
            with self.assertRaises(ValueError):oci_archive.verify(p,'a'*40)

if __name__=='__main__':unittest.main()
