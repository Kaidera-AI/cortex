"""Deterministic outer OCI-layout archive; opaque image blobs are never rewritten."""

import hashlib
import io
import json
import tarfile
from pathlib import Path


def snapshot_files(root):
    root = Path(root)
    result = {}
    if root.is_symlink() or not root.is_dir():
        raise ValueError("physical OCI layout required")
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError("unsafe layout member")
        if path.is_file():
            before = path.stat()
            raw = path.read_bytes()
            after = path.stat()
            if (
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("layout changed during snapshot")
            result[rel] = raw
    return result


def _json_object(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError("duplicate OCI JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("nonfinite OCI JSON number")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, RecursionError) as error:
        raise ValueError("malformed OCI JSON") from error
    if not isinstance(value, dict):
        raise ValueError("OCI JSON object required")
    return value


def oci_closure(files):
    try:
        layout = _json_object(files["oci-layout"])
        index = _json_object(files["index.json"])
    except (KeyError, ValueError):
        raise ValueError("OCI layout/index absent or malformed") from None
    if layout != {"imageLayoutVersion": "1.0.0"} or index.get("schemaVersion") != 2:
        raise ValueError("unsupported OCI format")
    measured = {}
    visited = set()

    def descriptor(row, role="manifest", depth=0):
        if not isinstance(row, dict) or depth > 32:
            raise ValueError("invalid or excessive OCI descriptor nesting")
        allowed = {
            "manifest": {
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.oci.image.index.v1+json",
            },
            "config": {"application/vnd.oci.image.config.v1+json"},
            "layer": {
                "application/vnd.oci.image.layer.v1.tar",
                "application/vnd.oci.image.layer.v1.tar+gzip",
                "application/vnd.oci.image.layer.v1.tar+zstd",
            },
        }
        if row.get("mediaType") not in allowed[role]:
            raise ValueError("unsupported OCI descriptor media type")
        digest = row.get("digest", "")
        size = row.get("size")
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or len(digest) != 71
            or any(c not in "0123456789abcdef" for c in digest[7:])
        ):
            raise ValueError("invalid OCI digest")
        name = "blobs/sha256/" + digest[7:]
        if (
            name not in files
            or type(size) is not int
            or size != len(files[name])
            or hashlib.sha256(files[name]).hexdigest() != digest[7:]
        ):
            raise ValueError("OCI blob missing/hash/size mismatch")
        measured[name] = {"bytes": size, "sha256": digest[7:]}
        if name in visited:
            return
        visited.add(name)
        media = row.get("mediaType")
        if media in [
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.oci.image.index.v1+json",
        ]:
            body = _json_object(files[name])
            if body.get("schemaVersion") != 2:
                raise ValueError("invalid OCI descriptor schema")
            if media.endswith("manifest.v1+json"):
                if not isinstance(body.get("layers"), list) or "config" not in body:
                    raise ValueError("incomplete OCI manifest")
                descriptor(body["config"], "config", depth + 1)
                for layer in body["layers"]:
                    descriptor(layer, "layer", depth + 1)
            else:
                if not isinstance(body.get("manifests"), list) or not body["manifests"]:
                    raise ValueError("empty OCI nested index")
                for item in body["manifests"]:
                    descriptor(item, "manifest", depth + 1)

    manifests = index.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        raise ValueError("empty OCI manifest inventory")
    for row in manifests:
        descriptor(row)
    if set(files) - {"oci-layout", "index.json"} != set(measured):
        raise ValueError("OCI closure has extra or absent members")
    return measured


def write_archive(layout, destination, epoch):
    if type(epoch) is not int or epoch < 0:
        raise ValueError("invalid archive epoch")
    files = snapshot_files(layout)
    closure = oci_closure(files)
    destination = Path(destination)
    if destination.exists():
        raise ValueError("fresh archive output required")
    with (
        destination.open("xb") as target,
        tarfile.open(fileobj=target, mode="w", format=tarfile.USTAR_FORMAT) as archive,
    ):
        for name, raw in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = epoch
            archive.addfile(info, io.BytesIO(raw))
    with destination.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "path": str(destination),
        "media_type": "application/x-tar",
        "bytes": destination.stat().st_size,
        "sha256": digest,
        "oci_closure": closure,
    }
