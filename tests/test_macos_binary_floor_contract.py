"""R154 frozen behavioral contract; simulated tools, no native build or install."""
import hashlib
import importlib.util
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NAMES = ("cortex-test", "cortex-agent")


def header(platform="1", floor="11.0", sdk="15.5"):
    return ("Load command 0\n cmd LC_SEGMENT_64\n cmdsize 72\n"
            "Load command 1\n cmd LC_BUILD_VERSION\n cmdsize 32\n"
            f" platform {platform}\n minos {floor}\n sdk {sdk}\n"
            " ntools 1\n tool 3\n version 1230.1\n")


class Tools:
    def __init__(self, out, *, target=None, bad_pre=None, bad_post=None, failure=None, sdk="15.5"):
        self.out = out
        self.target = target
        self.bad_pre = bad_pre
        self.bad_post = bad_post
        self.failure = failure
        self.sdk = sdk
        self.stamped = set()
        self.calls = []

    def run(self, args, *, check, stdout, text):
        assert check is True and text is True
        name = next((n for n in NAMES if any(n == Path(str(a)).name or a == "--name=" + n for a in args)), None)
        assert name is not None, args
        binary = self.out / "bin" / name
        post = name in self.stamped
        if "PyInstaller" in args:
            stage = "freeze"
        elif args[0] == "lipo":
            stage = "arch-post" if post else "arch-pre"
        elif args[:2] == ["otool", "-l"]:
            stage = "header-post" if post else "header-pre"
        elif args[:2] == ["otool", "-L"]:
            stage = "dependencies"
        elif args[0] == "vtool":
            stage = "stamp"
        elif args[0] == "codesign":
            stage = "signature-verify" if "--verify" in args else "sign"
        elif args == [str(binary), "--help"]:
            stage = "help"
        elif "PyInstaller.utils.cliutils.archive_viewer" in args:
            stage = "archive"
        else:
            pytest.fail(f"unexpected build command: {args}")
        self.calls.append((name, stage, list(args)))
        if name == self.target and stage == self.failure:
            raise subprocess.CalledProcessError(9, args)
        value = ""
        if stage == "freeze":
            binary.parent.mkdir(exist_ok=True)
            binary.write_bytes(("frozen-" + name).encode())
            binary.chmod(0o755)
        elif stage.startswith("arch"):
            bad = self.bad_post if post else self.bad_pre
            value = bad if name == self.target and bad in ("x86_64", "arm64 x86_64") else "arm64"
        elif stage.startswith("header"):
            value = header(floor="14.0" if post else "11.0", sdk=self.sdk)
            bad = self.bad_post if post else self.bad_pre
            if name == self.target:
                if bad == "absent":
                    value = "Load command 0\n cmd LC_SEGMENT_64\n cmdsize 72\n"
                elif bad == "duplicate":
                    value += header(floor="14.0" if post else "11.0", sdk=self.sdk)
                elif bad == "platform":
                    value = header(platform="2", floor="14.0" if post else "11.0", sdk=self.sdk)
                elif bad == "legacy":
                    value += "Load command 2\n cmd LC_VERSION_MIN_MACOSX\n version 14.0\n sdk 15.5\n"
                elif bad == "sdk-invalid":
                    value = header(floor="14.0" if post else "11.0", sdk="not-a-version")
                elif bad == "sdk-changed":
                    value = header(floor="14.0", sdk="99.1")
                elif bad == "floor-high":
                    value = header(floor="15.0", sdk=self.sdk)
                elif bad == "floor-low":
                    value = header(floor="11.0", sdk=self.sdk)
                elif bad == "floor-invalid":
                    value = header(floor="not-a-version", sdk=self.sdk)
        elif stage == "stamp":
            assert args[1:5] == ["-set-build-version", "macos", "14.0", self.sdk]
            assert "-output" in args and args[-1] == str(binary)
            output = Path(args[args.index("-output") + 1])
            assert output != binary
            assert not output.exists()
            output.write_bytes(binary.read_bytes() + b"-stamped")
            output.chmod(0o755)
            self.stamped.add(name)
        elif stage == "sign":
            assert "--force" in args and args[args.index("--sign") + 1] == "-"
            assert binary.read_bytes().endswith(b"-stamped")
            binary.write_bytes(binary.read_bytes() + b"-signed")
        elif stage == "signature-verify":
            assert "--strict" in args
            assert binary.read_bytes().endswith(b"-signed")
        elif stage == "archive":
            value = "modules-" + name
        elif stage == "dependencies":
            value = "/usr/lib/libSystem.B.dylib"
        return subprocess.CompletedProcess(args, 0, value if stdout is not None else None)


def setup(tmp_path, monkeypatch, **options):
    spec = importlib.util.spec_from_file_location("floor_candidate", ROOT / "scripts/release/build-candidate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = Tools(tmp_path, **options)
    monkeypatch.setattr(module.subprocess, "run", tools.run)
    original = module.digest

    def digest(binary):
        tools.calls.append((binary.name, "digest", [str(binary)]))
        return original(binary)

    monkeypatch.setattr(module, "digest", digest)
    return module, tools


@pytest.mark.parametrize("sdk", ["15.5", "15.5.1"])
def test_both_frozen_entrypoints_verified_before_any_final_inventory(tmp_path, monkeypatch, sdk):
    module, tools = setup(tmp_path, monkeypatch, sdk=sdk)
    programs = module.freeze_host_programs(tmp_path)
    assert set(programs) == set(NAMES)
    stages = [(name, stage) for name, stage, _ in tools.calls]
    last_help = max(i for i, (_, stage) in enumerate(stages) if stage == "help")
    assert all(i > last_help for i, (_, stage) in enumerate(stages) if stage in ("archive", "digest", "dependencies"))
    for name in NAMES:
        own = [stage for n, stage in stages if n == name]
        assert own[:9] == ["freeze", "arch-pre", "header-pre", "stamp", "sign", "signature-verify", "arch-post", "header-post", "help"]
        assert own.count("help") == own.count("stamp") == own.count("sign") == 1
        binary = tmp_path / "bin" / name
        assert binary.read_bytes() == ("frozen-" + name).encode() + b"-stamped-signed"
        assert binary.stat().st_mode & 0o111
        assert programs[name]["sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
        assert programs[name]["dependencies"] == ["/usr/lib/libSystem.B.dylib"]
        assert (tmp_path / programs[name]["archive_inventory"]).read_text() == "modules-" + name + "\n"
    assert not list((tmp_path / "bin").glob("*.floor-*"))
    assert (tmp_path / "bin/cortex-projects").read_bytes() == (ROOT / "scripts/agent-shims/cortex-projects").read_bytes()


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("bad", ["x86_64", "arm64 x86_64", "absent", "duplicate", "platform", "legacy", "sdk-invalid", "floor-high", "floor-invalid"])
def test_invalid_input_refuses_before_stamp_or_any_inventory(tmp_path, monkeypatch, name, bad):
    module, tools = setup(tmp_path, monkeypatch, target=name, bad_pre=bad)
    with pytest.raises(RuntimeError):
        module.freeze_host_programs(tmp_path)
    assert not any(n == name and stage == "stamp" for n, stage, _ in tools.calls)
    assert not any(stage in ("archive", "digest") for _, stage, _ in tools.calls)
    assert not list(tmp_path.glob("*-archive-inventory.txt"))


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("bad", ["x86_64", "absent", "duplicate", "platform", "legacy", "sdk-invalid", "sdk-changed", "floor-high", "floor-low", "floor-invalid"])
def test_independent_post_stamp_inspection_refuses_before_help_or_inventory(tmp_path, monkeypatch, name, bad):
    module, tools = setup(tmp_path, monkeypatch, target=name, bad_post=bad)
    with pytest.raises(RuntimeError):
        module.freeze_host_programs(tmp_path)
    assert any(n == name and stage == "signature-verify" for n, stage, _ in tools.calls)
    assert not any(n == name and stage == "help" for n, stage, _ in tools.calls)
    assert not any(stage in ("archive", "digest") for _, stage, _ in tools.calls)
    assert not list(tmp_path.glob("*-archive-inventory.txt"))


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("stage", ["freeze", "arch-pre", "header-pre", "stamp", "sign", "signature-verify", "arch-post", "header-post", "help"])
def test_tool_or_entrypoint_failure_stops_before_any_final_inventory(tmp_path, monkeypatch, name, stage):
    module, tools = setup(tmp_path, monkeypatch, target=name, failure=stage)
    with pytest.raises(subprocess.CalledProcessError) as error:
        module.freeze_host_programs(tmp_path)
    assert error.value.returncode == 9
    assert tools.calls[-1][:2] == (name, stage)
    assert not any(s in ("archive", "digest") for _, s, _ in tools.calls)
    assert not list(tmp_path.glob("*-archive-inventory.txt"))


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("stage", ["dependencies", "archive"])
def test_inventory_helper_failure_refuses_the_candidate(tmp_path, monkeypatch, name, stage):
    module, tools = setup(tmp_path, monkeypatch, target=name, failure=stage)
    with pytest.raises(subprocess.CalledProcessError) as error:
        module.freeze_host_programs(tmp_path)
    assert error.value.returncode == 9
    assert tools.calls[-1][:2] == (name, stage)
    assert {n for n, s, _ in tools.calls if s == "help"} == set(NAMES)
