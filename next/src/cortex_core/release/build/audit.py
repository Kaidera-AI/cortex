"""Causal bytecode and semantic audit; never normalizes an unequal image."""

import importlib.util
import io
import marshal
import struct
import types


def _code_equal(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, types.CodeType):
        fields = (
            "co_argcount",
            "co_posonlyargcount",
            "co_kwonlyargcount",
            "co_nlocals",
            "co_stacksize",
            "co_flags",
            "co_code",
            "co_consts",
            "co_names",
            "co_varnames",
            "co_filename",
            "co_name",
            "co_qualname",
            "co_firstlineno",
            "co_linetable",
            "co_exceptiontable",
            "co_freevars",
            "co_cellvars",
        )
        return all(_code_equal(getattr(left, k), getattr(right, k)) for k in fields)
    if isinstance(left, tuple):
        return len(left) == len(right) and all(
            _code_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, frozenset):
        if len(left) != len(right):
            return False
        remaining = list(right)
        for value in left:
            for index, candidate in enumerate(remaining):
                if _code_equal(value, candidate):
                    remaining.pop(index)
                    break
            else:
                return False
        return not remaining
    return left == right


def audit_cache(raw, source, filename):
    if (
        len(raw) < 16
        or raw[:4] != importlib.util.MAGIC_NUMBER
        or struct.unpack("<I", raw[4:8])[0] != 3
    ):
        raise ValueError("checked-hash magic/flags required")
    if raw[8:16] != importlib.util.source_hash(source):
        raise ValueError("cache source hash differs")
    try:
        stream = io.BytesIO(raw[16:])
        actual = marshal.load(stream)
        if stream.read():
            raise ValueError("trailing cache payload")
    except (ValueError, EOFError, TypeError):
        raise ValueError("cache body malformed") from None
    expected = compile(source, filename, "exec", dont_inherit=True, optimize=0)
    if not isinstance(actual, types.CodeType) or not _code_equal(actual, expected):
        raise ValueError("cache body or final filename differs")
    return True


def audit_all_caches(caches, sources):
    if not caches:
        raise ValueError("empty cache inventory is not exhaustive proof")
    for row in caches:
        if row["source"] not in sources:
            raise ValueError("orphan cache")
        audit_cache(row["bytes"], sources[row["source"]], row["filename"])
    return {"cache_count": len(caches), "all_source_bound": True}


def compare_metadata(before, after):
    if before != after:
        raise ValueError("account/log/label/cache/metadata drift")
    return True


def require_useful_result(status, nodes, errors):
    if status != "ok" or type(nodes) is not int or nodes < 1 or errors:
        raise ValueError("runtime useful-result admission refused")
    return True


def validate_offline_assets(expected, actual):
    if not expected or set(expected) != set(actual):
        raise ValueError("offline inventory missing or extra")
    if any(actual[k] != v for k, v in expected.items()):
        raise ValueError("offline asset digest differs")
    return True


def layer_inventory(
    layers, *, allowed_attributes=None, max_bytes=256 * 1024 * 1024, max_members=50000
):
    """Measure overlay contents without extracting or following image links.

    Bounds cover cumulative expanded bytes and headers, including overwritten files.
    Attribute acceptance is an exact policy map; unknown ACL/xattr values refuse.
    """
    import tarfile
    from pathlib import PurePosixPath

    allowed_attributes = {} if allowed_attributes is None else allowed_attributes
    final = {}
    expanded = members = 0
    if not layers:
        raise ValueError("empty layer inventory")
    for raw in layers:
        current = {}
        whiteouts = []
        try:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
                for item in archive:
                    members += 1
                    expanded += item.size
                    if members > max_members or expanded > max_bytes or item.size < 0:
                        raise ValueError("expanded layer budget exceeded")
                    name = item.name
                    while name.startswith("./"):
                        name = name[2:]
                    name = name.rstrip("/")
                    path = PurePosixPath(name)
                    if (
                        not name
                        or path.is_absolute()
                        or ".." in path.parts
                        or "\x00" in name
                        or str(path) != name
                    ):
                        raise ValueError("unsafe layer path")
                    if name in current:
                        raise ValueError("duplicate layer path")
                    for key, value in item.pax_headers.items():
                        if (
                            key.startswith(
                                ("SCHILY.xattr.", "SCHILY.acl.", "LIBARCHIVE.xattr.")
                            )
                            and allowed_attributes.get(key) != value
                        ):
                            raise ValueError("unadmitted layer attribute")
                    if path.name.startswith(".wh."):
                        if not item.isfile() or item.size != 0:
                            raise ValueError("invalid whiteout")
                        whiteouts.append(path)
                    if not (
                        item.isfile() or item.isdir() or item.issym() or item.islnk()
                    ):
                        raise ValueError("unsupported special layer member")
                    content = b""
                    if item.isfile():
                        stream = archive.extractfile(item)
                        if stream is None:
                            raise ValueError("missing layer payload")
                        with stream:
                            content = stream.read(item.size + 1)
                        if len(content) != item.size:
                            raise ValueError("truncated layer payload")
                    current[name] = {
                        "content": content,
                        "type": item.type,
                        "uid": item.uid,
                        "gid": item.gid,
                        "mode": item.mode,
                        "mtime": item.mtime,
                        "linkname": item.linkname,
                        "pax_headers": dict(item.pax_headers),
                    }
        except (tarfile.TarError, EOFError, OSError) as error:
            raise ValueError("malformed layer archive") from error
        for path in whiteouts:
            parent = str(path.parent)
            if path.name == ".wh..wh..opq":
                prefix = "" if parent == "." else parent + "/"
                final = {k: v for k, v in final.items() if not k.startswith(prefix)}
            else:
                target = str(path.with_name(path.name[4:]))
                final = {
                    k: v
                    for k, v in final.items()
                    if k != target and not k.startswith(target + "/")
                }
        final.update(
            {
                k: v
                for k, v in current.items()
                if not PurePosixPath(k).name.startswith(".wh.")
            }
        )
    return final


def audit_image(files, platform, expected_runtime, *, allowed_attributes=None):
    """Inspect a single-platform OCI image from its verified closure.

    This report proves image/config/metadata inspection only. Python and runtime
    execution are separate required reports; it does not grant native qualification.
    """
    import hashlib
    from .export import oci_closure, _json_object

    closure = oci_closure(files)
    index = _json_object(files["index.json"])
    if len(index["manifests"]) != 1:
        raise ValueError("single-platform image required")
    manifest_row = index["manifests"][0]
    if manifest_row["mediaType"] != "application/vnd.oci.image.manifest.v1+json":
        raise ValueError("concrete single-platform manifest required")

    def payload(row):
        return files["blobs/sha256/" + row["digest"][7:]]

    manifest = _json_object(payload(manifest_row))
    config = _json_object(payload(manifest["config"]))
    if platform not in ("linux/amd64", "linux/arm64") or platform != config.get(
        "os", ""
    ) + "/" + config.get("architecture", ""):
        raise ValueError("image platform differs from admitted input")
    runtime = config.get("config")
    if not isinstance(runtime, dict) or not expected_runtime:
        raise ValueError("explicit runtime contract required")
    for key, expected in expected_runtime.items():
        if key not in runtime or runtime[key] != expected:
            raise ValueError("runtime config differs: " + key)
    inventory = layer_inventory(
        [payload(row) for row in manifest["layers"]],
        allowed_attributes=allowed_attributes,
    )
    report = {}
    for name, row in inventory.items():
        content = row["content"]
        report[name] = {k: v for k, v in row.items() if k != "content"}
        report[name]["content_bytes"] = len(content)
        report[name]["content_sha256"] = hashlib.sha256(content).hexdigest()
    return {
        "complete": True,
        "classes": ["oci_closure", "runtime_config", "filesystem_metadata"],
        "file_count": len(inventory),
        "files": report,
        "oci_closure": closure,
    }
