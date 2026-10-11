#!/usr/bin/env python3
"""Builder-only verified bundles, decoded by the already pinned upstream library."""

from __future__ import annotations
import argparse, hashlib, importlib.metadata, json, os, platform, shutil, stat, urllib.request
from pathlib import Path

ROOT = Path("/opt/kaidera-grammars")
CACHE = ROOT / "cache"
VERSION = "1.16.1"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(pin, target):
    if not pin["url"].startswith(
        "https://github.com/xberg-io/tree-sitter-language-pack/releases/download/v1.16.1/"
    ):
        raise RuntimeError("unexpected grammar source")
    with (
        urllib.request.urlopen(pin["url"], timeout=60) as source,
        target.open("xb") as out,
    ):
        total = 0
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > pin["size"]:
                raise RuntimeError("grammar artifact exceeds pin")
            out.write(chunk)
    if total != pin["size"] or digest(target) != pin["sha256"]:
        raise RuntimeError("grammar artifact hash/size mismatch")


def provision(pins_path, root=ROOT):
    import tree_sitter_language_pack as pack
    from better_code_review_graph.parser import SUPPORTED_LANGUAGES

    pins = json.loads(Path(pins_path).read_text())
    if (
        pins["schema_version"] != 1
        or pins["language_pack_version"] != VERSION
        or importlib.metadata.version("tree-sitter-language-pack") != VERSION
        or importlib.metadata.version("better-code-review-graph")
        != pins["bcrg_version"]
    ):
        raise RuntimeError("grammar dependency identity differs")
    if set(pins["supported_languages"]) != set(SUPPORTED_LANGUAGES):
        raise RuntimeError("incomplete grammar scope")
    key = {
        "x86_64": "linux-x86_64",
        "aarch64": "linux-aarch64",
        "arm64": "linux-aarch64",
    }.get(platform.machine())
    if platform.system() != "Linux" or key not in pins["platforms"]:
        raise RuntimeError("unsupported grammar build platform")
    if root.exists():
        raise RuntimeError("fresh grammar artifact root required")
    root.mkdir(mode=0o700)
    cache = root / "cache"
    version = cache / "tree-sitter-language-pack" / ("v" + VERSION)
    libs = version / "libs"
    libs.mkdir(parents=True, mode=0o700)
    version.chmod(0o700)
    manifest_path = version / "manifest.json"
    fetch(pins["manifest"], manifest_path)
    manifest = json.loads(manifest_path.read_text())
    bundle = pins["platforms"][key]
    if manifest["version"] != VERSION or manifest["platforms"][key] != bundle:
        raise RuntimeError("pinned manifest/bundle binding differs")
    bundles = version / "bundles"
    bundles.mkdir(mode=0o700)
    archive = bundles / (key + "-" + bundle["sha256"] + ".tar.zst")
    fetch(bundle, archive)
    # Both native decoder inputs are already independently hash checked. No resolver or moving head.
    os.environ["TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL"] = "file://" + str(
        manifest_path
    )
    pack.configure(pack.PackConfig(cache_dir=str(cache)))
    pack.download(pins["supported_languages"])
    rows = []
    for path in sorted(libs.iterdir()):
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not path.name.startswith("libtree_sitter_")
            or path.suffix != ".so"
        ):
            raise RuntimeError("unexpected grammar cache member")
        rows.append(
            {
                "path": str(path.relative_to(root)),
                "size": info.st_size,
                "sha256": digest(path),
            }
        )
    if len(rows) != len(set(SUPPORTED_LANGUAGES)):
        raise RuntimeError("grammar cache is not exact supported set")
    if sorted(rows, key=lambda r: r["path"]) != sorted(
        pins["grammar_artifacts"][key], key=lambda r: r["path"]
    ):
        raise RuntimeError("decoded grammar artifact hashes differ from pins")
    for language in sorted(SUPPORTED_LANGUAGES):
        if not pack.has_language(language):
            raise RuntimeError("supported grammar unavailable offline: " + language)
        pack.get_parser(language)
    shutil.rmtree(bundles)
    lock = version / ".download.lock"
    if lock.exists():
        lock.unlink()
    shutil.copyfile(pins_path, root / "tree-sitter-grammar-pins.json")
    receipt = {
        "schema_version": 1,
        "platform": key,
        "language_pack_version": VERSION,
        "bcrg_version": pins["bcrg_version"],
        "supported_languages": sorted(SUPPORTED_LANGUAGES),
        "manifest": pins["manifest"],
        "bundle": bundle,
        "pins_sha256": digest(root / "tree-sitter-grammar-pins.json"),
        "artifacts": rows,
        "builder_all_parsers_loaded": True,
    }
    (root / "grammar-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pins", type=Path, default=Path("/build/tree-sitter-grammar-pins.json")
    )
    a = parser.parse_args()
    print(json.dumps(provision(a.pins)))
