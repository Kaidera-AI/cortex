"""Measure reviewed v1 artifact bytes and construct external signing input.

This module neither builds nor signs. Native source/image/install qualification
and the release authority still decide whether its output may be signed.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import stat


VERSION = "0.1.003-manual.1"
RELEASE = "v" + VERSION
ROLE_MAP = {"db": "db", "migrate": "api", "api": "api", "graph": "graph-worker",
            "embed": "embed-worker", "pdf": "pdf-worker"}


class ManifestRefusal(RuntimeError):
    pass


def _fail(reason):
    raise ManifestRefusal(reason)


def _inventory(value, source_revision):
    path = Path(__file__).with_name("image_manifest.py")
    spec = importlib.util.spec_from_file_location("cortex_manual_image_inventory", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        module.validate_inventory(value, {"version": VERSION, "source_revision": source_revision})
    except (ValueError, TypeError, KeyError):
        _fail("invalid_v1_image_inventory")
    if set(value["platforms"]) != {"linux/amd64"}:
        _fail("manual_release_requires_linux_amd64_only")
    for role, lock in value["platforms"]["linux/amd64"].items():
        if lock["repository"] != "ghcr.io/kaidera-ai/cortex-" + role or lock["tag"] != RELEASE:
            _fail("image_repository_or_tag_does_not_name_manual_release")
    return copy.deepcopy(value)


def _measure(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        _fail("artifact_path_invalid")
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        try:
            info = cursor.lstat()
        except OSError:
            _fail("artifact_missing")
        if stat.S_ISLNK(info.st_mode) or info.st_mode & 0o022:
            _fail("artifact_path_unsafe")
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or before.st_size <= 0):
            _fail("artifact_not_owner_controlled_regular_file")
        digest = hashlib.sha256()
        size = 0
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
        after = os.fstat(fd)
        named = path.lstat()
        if (size != before.st_size or (before.st_dev, before.st_ino, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino,
                after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)):
            _fail("artifact_changed_during_measurement")
        return {"size": size, "sha256": digest.hexdigest()}
    except OSError:
        _fail("artifact_unreadable")
    finally:
        if fd >= 0:
            os.close(fd)


def build_manifest(*, source_revision, image_inventory, artifacts):
    if not isinstance(source_revision, str) or re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        _fail("source_revision_invalid")
    inventory = _inventory(image_inventory, source_revision)
    if not isinstance(artifacts, dict) or not artifacts:
        _fail("artifact_set_empty_or_invalid")
    measured = {}
    for name, path in artifacts.items():
        if (not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}", name) is None
                or name in {"release.json", "release.json.minisig"} or Path(path).name != name):
            _fail("artifact_name_or_manifest_cycle_invalid")
        measured[name] = _measure(path)
    locks = inventory["platforms"]["linux/amd64"]
    images = {cm1: locks[role]["repository"] + "@" + locks[role]["manifest_digest"]
              for cm1, role in ROLE_MAP.items()}
    return {"schema": "cortex.release.v1", "release_id": RELEASE,
            "release_lineage": "cortex-v1-manual", "release_sequence": 1,
            "api_contract": "cortex-kos-v02009.v1", "source_revision": source_revision,
            "version": VERSION, "images": {"linux/amd64": images},
            "oci_inventory": inventory, "artifacts": measured,
            "podman": {"supported_family": "6.0.x", "tested_baseline": "6.0.2"}}
