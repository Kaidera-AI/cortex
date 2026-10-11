import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from cortex_core.release.build import export, compare


def layout(root):
    (root / "blobs/sha256").mkdir(parents=True)

    def blob(value, media):
        raw = (
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            if isinstance(value, dict)
            else value
        )
        digest = hashlib.sha256(raw).hexdigest()
        (root / "blobs/sha256" / digest).write_bytes(raw)
        return {"mediaType": media, "digest": "sha256:" + digest, "size": len(raw)}

    config = blob(
        {
            "architecture": "amd64",
            "os": "linux",
            "config": {"User": "10001", "Labels": {"io.buildah.version": "1.43.1"}},
        },
        "application/vnd.oci.image.config.v1+json",
    )
    layer = blob(b"opaque layer bytes", "application/vnd.oci.image.layer.v1.tar")
    manifest = blob(
        {"schemaVersion": 2, "config": config, "layers": [layer]},
        "application/vnd.oci.image.manifest.v1+json",
    )
    (root / "index.json").write_text(
        json.dumps({"schemaVersion": 2, "manifests": [manifest]})
    )
    (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')


class Export(unittest.TestCase):
    def test_two_independent_layouts_have_identical_whole_archive(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            a = p / "a"
            b = p / "b"
            a.mkdir()
            b.mkdir()
            layout(a)
            layout(b)
            before = {
                x.relative_to(a).as_posix(): x.read_bytes()
                for x in a.rglob("*")
                if x.is_file()
            }
            first = export.write_archive(a, p / "a.tar", 123)
            second = export.write_archive(b, p / "b.tar", 123)
            self.assertTrue(compare.compare_archives(p / "a.tar", p / "b.tar"))
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(
                before,
                {
                    x.relative_to(a).as_posix(): x.read_bytes()
                    for x in a.rglob("*")
                    if x.is_file()
                },
            )

    def test_opaque_changed_layer_cannot_be_repaired_by_outer_writer(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            layout(p)
            blob = next(
                x
                for x in (p / "blobs/sha256").iterdir()
                if x.read_bytes() == b"opaque layer bytes"
            )
            blob.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                export.write_archive(p, p.parent / (p.name + ".tar"), 123)

    def test_extra_or_missing_blob_refuses(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            layout(p)
            (p / "extra").write_bytes(b"unbound")
            with self.assertRaises(ValueError):
                export.oci_closure(export.snapshot_files(p))

    def test_link_escape_refuses(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            layout(p)
            (p / "evil").symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                export.snapshot_files(p)


class DescriptorRefusals(unittest.TestCase):
    def test_duplicate_json_key_refuses(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            layout(p)
            files = export.snapshot_files(p)
            files["index.json"] = files["index.json"].replace(
                b'"schemaVersion": 2', b'"schemaVersion": 1, "schemaVersion": 2'
            )
            with self.assertRaises(ValueError):
                export.oci_closure(files)

    def test_unknown_root_media_cannot_skip_manifest_audit(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            layout(p)
            files = export.snapshot_files(p)
            index = json.loads(files["index.json"])
            index["manifests"][0]["mediaType"] = "application/octet-stream"
            files["index.json"] = json.dumps(index).encode()
            with self.assertRaises(ValueError):
                export.oci_closure(files)

    def test_descriptor_not_object_is_typed_refusal(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            layout(p)
            files = export.snapshot_files(p)
            files["index.json"] = b'{"schemaVersion":2,"manifests":[null]}'
            with self.assertRaises(ValueError):
                export.oci_closure(files)
