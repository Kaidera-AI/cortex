#!/usr/bin/env python3
"""Receipt validation and immutable parser configuration; never downloads."""

from __future__ import annotations
import hashlib, importlib.metadata, json, os, platform, stat
from pathlib import Path, PurePosixPath

ROOT = Path("/opt/kaidera-grammars")
VERSION = "1.16.1"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_and_configure(root=ROOT):
    import tree_sitter_language_pack as pack
    from better_code_review_graph.parser import SUPPORTED_LANGUAGES

    pins = json.loads((root / "tree-sitter-grammar-pins.json").read_text())
    receipt = json.loads((root / "grammar-receipt.json").read_text())
    key = {
        "x86_64": "linux-x86_64",
        "aarch64": "linux-aarch64",
        "arm64": "linux-aarch64",
    }.get(platform.machine())
    if (
        root.resolve(strict=True) != root
        or pins.get("schema_version") != 1
        or receipt.get("schema_version", 1) != 1
    ):
        raise RuntimeError("grammar root/schema differs")
    if (
        platform.system() != "Linux"
        or key not in pins["platforms"]
        or receipt["platform"] != key
    ):
        raise RuntimeError("grammar platform differs")
    if (
        pins["language_pack_version"] != VERSION
        or importlib.metadata.version("tree-sitter-language-pack") != VERSION
        or importlib.metadata.version("better-code-review-graph")
        != pins["bcrg_version"]
    ):
        raise RuntimeError("grammar dependency identity differs")
    if set(pins["supported_languages"]) != set(SUPPORTED_LANGUAGES) or receipt[
        "supported_languages"
    ] != sorted(SUPPORTED_LANGUAGES):
        raise RuntimeError("supported grammar coverage differs")
    if (
        receipt["pins_sha256"] != digest(root / "tree-sitter-grammar-pins.json")
        or receipt["manifest"] != pins["manifest"]
        or receipt["bundle"] != pins["platforms"][key]
    ):
        raise RuntimeError("grammar receipt/pins differ")
    cache = root / "cache"
    version = cache / "tree-sitter-language-pack" / ("v" + VERSION)
    libs = version / "libs"
    manifest = version / "manifest.json"
    if (
        digest(manifest) != pins["manifest"]["sha256"]
        or manifest.stat().st_size != pins["manifest"]["size"]
    ):
        raise RuntimeError("immutable grammar manifest differs")
    if sorted(receipt["artifacts"], key=lambda r: r["path"]) != sorted(
        pins["grammar_artifacts"][key], key=lambda r: r["path"]
    ):
        raise RuntimeError("grammar receipt artifacts differ from pinned decoded bytes")
    expected = {r["path"] for r in receipt["artifacts"]}
    actual = {str(p.relative_to(root)) for p in libs.iterdir()}
    if actual != expected or len(actual) != len(SUPPORTED_LANGUAGES):
        raise RuntimeError("grammar artifact allowlist differs")
    for directory in [root, cache, cache / "tree-sitter-language-pack", version, libs]:
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or directory.is_symlink()
            or info.st_mode & 0o222
            or info.st_uid != os.geteuid()
        ):
            raise RuntimeError("grammar cache is not immutable and runtime-owned")
    for row in receipt["artifacts"]:
        rel = PurePosixPath(row["path"])
        if (
            rel.is_absolute()
            or ".." in rel.parts
            or len(rel.parts) != 5
            or rel.parts[:4]
            != ("cache", "tree-sitter-language-pack", "v1.16.1", "libs")
            or not rel.name.startswith("libtree_sitter_")
            or rel.suffix != ".so"
        ):
            raise RuntimeError("grammar artifact path escapes exact cache")
        path = root / row["path"]
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_mode & 0o222
            or info.st_uid != os.geteuid()
            or path.resolve() != path
            or info.st_size != row["size"]
            or digest(path) != row["sha256"]
        ):
            raise RuntimeError("immutable grammar bytes/custody differ")
    os.environ["TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"] = str(cache)
    os.environ["TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL"] = "file://" + str(manifest)
    pack.configure(pack.PackConfig(cache_dir=str(cache)))
    for language in sorted(SUPPORTED_LANGUAGES):
        # Availability is checked without downloading; a missing asset is never sent to the lazy resolver.
        if not pack.has_language(language):
            raise RuntimeError("offline grammar unavailable: " + language)
        pack.get_parser(language)
    return receipt


if __name__ == "__main__":
    print(json.dumps({"result": "PASS", "receipt": verify_and_configure()}))
