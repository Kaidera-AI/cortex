import hashlib, importlib.util, json, os, sys, types
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    s = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def setup(tmp_path, monkeypatch):
    m = load("verify-tree-sitter-grammars")
    pins = json.loads((ROOT / "tree-sitter-grammar-pins.json").read_text())
    languages = set(pins["supported_languages"])
    key = "linux-x86_64"
    root = tmp_path / "artifact"
    libs = root / "cache/tree-sitter-language-pack/v1.16.1/libs"
    libs.mkdir(parents=True)
    version = libs.parent
    manifest = b'{"version":"1.16.1"}'
    pins["manifest"]["size"] = len(manifest)
    pins["manifest"]["sha256"] = hashlib.sha256(manifest).hexdigest()
    (version / "manifest.json").write_bytes(manifest)
    pinfile = root / "tree-sitter-grammar-pins.json"
    pinfile.write_text(json.dumps(pins))
    rows = []
    for lang in languages:
        f = libs / ("libtree_sitter_" + lang + ".so")
        f.write_bytes(lang.encode())
        rows.append(
            {
                "path": str(f.relative_to(root)),
                "size": f.stat().st_size,
                "sha256": m.digest(f),
            }
        )
    pins["grammar_artifacts"][key] = rows
    pinfile.write_text(json.dumps(pins))
    receipt = {
        "platform": key,
        "supported_languages": sorted(languages),
        "pins_sha256": m.digest(pinfile),
        "manifest": pins["manifest"],
        "bundle": pins["platforms"][key],
        "artifacts": rows,
    }
    (root / "grammar-receipt.json").write_text(json.dumps(receipt))
    calls = []
    pack = types.SimpleNamespace(
        PackConfig=lambda **kw: kw,
        configure=lambda c: calls.append(("configure", c)),
        has_language=lambda lang: True,
        get_parser=lambda lang: calls.append(("parser", lang)),
    )
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", pack)
    monkeypatch.setitem(
        sys.modules,
        "better_code_review_graph.parser",
        types.SimpleNamespace(SUPPORTED_LANGUAGES=languages),
    )
    monkeypatch.setattr(
        m.importlib.metadata,
        "version",
        lambda name: "1.16.1" if name == "tree-sitter-language-pack" else "3.24.0",
    )
    monkeypatch.setattr(m.platform, "system", lambda: "Linux")
    monkeypatch.setattr(m.platform, "machine", lambda: "x86_64")
    for f in root.rglob("*"):
        f.chmod(0o555 if f.is_dir() else 0o444)
    root.chmod(0o555)
    return m, root, libs, calls


def test_exact_immutable_receipt_loads_every_supported_parser(tmp_path, monkeypatch):
    m, root, libs, calls = setup(tmp_path, monkeypatch)
    m.verify_and_configure(root)
    assert len([c for c in calls if c[0] == "parser"]) == 16


def test_missing_grammar_refused_before_lazy_loader(tmp_path, monkeypatch):
    m, root, libs, calls = setup(tmp_path, monkeypatch)
    libs.chmod(0o755)
    next(libs.iterdir()).unlink()
    libs.chmod(0o555)
    with pytest.raises(RuntimeError):
        m.verify_and_configure(root)
    assert not calls


def test_changed_grammar_refused_before_loader(tmp_path, monkeypatch):
    m, root, libs, calls = setup(tmp_path, monkeypatch)
    f = next(libs.iterdir())
    f.chmod(0o644)
    f.write_bytes(b"changed")
    f.chmod(0o444)
    with pytest.raises(RuntimeError):
        m.verify_and_configure(root)
    assert not calls


def test_writable_cache_refused(tmp_path, monkeypatch):
    m, root, libs, calls = setup(tmp_path, monkeypatch)
    libs.chmod(0o755)
    with pytest.raises(RuntimeError, match="immutable"):
        m.verify_and_configure(root)
    assert not calls


def test_platform_mismatch_refused(tmp_path, monkeypatch):
    m, root, libs, calls = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(m.platform, "machine", lambda: "aarch64")
    with pytest.raises(RuntimeError, match="platform"):
        m.verify_and_configure(root)
    assert not calls


def test_builder_hash_mismatch_is_fatal(tmp_path, monkeypatch):
    m = load("fetch-tree-sitter-grammars")
    monkeypatch.setattr(
        m.urllib.request, "urlopen", lambda *a, **kw: __import__("io").BytesIO(b"wrong")
    )
    pin = {
        "url": "https://github.com/xberg-io/tree-sitter-language-pack/releases/download/v1.16.1/parsers.json",
        "size": 5,
        "sha256": "0" * 64,
    }
    with pytest.raises(RuntimeError, match="hash/size"):
        m.fetch(pin, tmp_path / "manifest")
