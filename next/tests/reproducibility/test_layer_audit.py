import io
import tarfile
import unittest
from cortex_core.release.build import audit


def layer(rows):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, raw, uid, pax in rows:
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            info.uid = uid
            info.pax_headers = pax
            archive.addfile(info, io.BytesIO(raw))
    return output.getvalue()


class LayerAudit(unittest.TestCase):
    def test_whiteout_resolves_final_files_without_extracting(self):
        first = layer([("etc/log", b"old", 0, {}), ("etc/account", b"locked", 0, {})])
        second = layer([("etc/.wh.log", b"", 0, {}), ("etc/account", b"new", 0, {})])
        result = audit.layer_inventory([first, second])
        self.assertEqual(set(result), {"etc/account"})
        self.assertEqual(result["etc/account"]["content"], b"new")

    def test_escape_duplicate_and_unknown_acl_are_refused(self):
        for rows in [
            [("../escape", b"bad", 0, {})],
            [("etc/a", b"first", 0, {}), ("etc/a", b"last", 0, {})],
            [("etc/a", b"a", 0, {"SCHILY.acl.access": "unknown"})],
        ]:
            with self.assertRaises(ValueError):
                audit.layer_inventory([layer(rows)])

    def test_account_content_and_owner_drift_are_deciding(self):
        original = audit.layer_inventory([layer([("etc/shadow", b"locked:0", 0, {})])])
        for raw, uid in [(b"locked:1", 0), (b"locked:0", 10001)]:
            mutant = audit.layer_inventory([layer([("etc/shadow", raw, uid, {})])])
            with self.assertRaises(ValueError):
                audit.compare_metadata(original, mutant)


class ImageAudit(unittest.TestCase):
    def test_config_and_layer_bytes_are_actually_audited(self):
        import hashlib
        import json

        def blob(value, media, files):
            raw = json.dumps(value).encode() if isinstance(value, dict) else value
            digest = hashlib.sha256(raw).hexdigest()
            files["blobs/sha256/" + digest] = raw
            return {"mediaType": media, "digest": "sha256:" + digest, "size": len(raw)}

        def image(user):
            files = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}'}
            config = blob(
                {
                    "architecture": "amd64",
                    "os": "linux",
                    "config": {"User": user, "Labels": {"builder": "pinned"}},
                },
                "application/vnd.oci.image.config.v1+json",
                files,
            )
            archive = blob(
                layer([("etc/shadow", b"locked:0", 0, {})]),
                "application/vnd.oci.image.layer.v1.tar",
                files,
            )
            manifest = blob(
                {"schemaVersion": 2, "config": config, "layers": [archive]},
                "application/vnd.oci.image.manifest.v1+json",
                files,
            )
            files["index.json"] = json.dumps(
                {"schemaVersion": 2, "manifests": [manifest]}
            ).encode()
            return files

        expected = {"User": "10001", "Labels": {"builder": "pinned"}}
        report = audit.audit_image(image("10001"), "linux/amd64", expected)
        self.assertEqual(report["file_count"], 1)
        self.assertTrue(report["complete"])
        with self.assertRaises(ValueError):
            audit.audit_image(image("0"), "linux/amd64", expected)
        with self.assertRaises(ValueError):
            audit.audit_image(image("10001"), "linux/arm64", expected)
