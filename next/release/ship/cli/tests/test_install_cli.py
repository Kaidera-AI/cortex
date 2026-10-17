"""Spec for ship/cli/install_cli.sh: render the v2-only launchers, refuse PATH collisions."""
import hashlib
import json
import pathlib
import re
import subprocess

import pytest

HERE = pathlib.Path(__file__).resolve().parent
INSTALL = HERE.parent / "install_cli.sh"
# Versioned v2 commands only (Kai Option A, cox ruling 11d7ebce); v1 names are never written.
COMMANDS = ["cortex-v2-boot", "cortex-v2-log", "cortex-v2-search"]
V1 = ["cortex-boot", "cortex-log", "cortex-search"]
BASH_DIAGNOSTIC = re.compile(r": line \d+: ")


class ChildError(Exception):
    """The installer crashed (bash diagnostic or undeclared exit): never a behavioral assertion."""


@pytest.fixture
def world(tmp_path):
    clients = tmp_path / "clients"
    clients.mkdir()
    for name in COMMANDS:
        path = clients / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o555)
    core_python = tmp_path / "core" / "bin" / "python3"
    core_python.parent.mkdir(parents=True)
    core_python.write_text("#!/bin/sh\nexit 0\n")
    core_python.chmod(0o755)
    link = tmp_path / "link"
    link.mkdir()
    other = tmp_path / "other-bin"
    other.mkdir()
    return {"tmp": tmp_path, "clients": clients, "core_python": core_python, "link": link,
            "other": other, "prefix": tmp_path / "prefix",
            "config": tmp_path / "vault" / "config" / "cortex.json"}


def install(world, *extra, path_dirs=None):
    assert INSTALL.is_file(), f"missing {INSTALL}"
    dirs = [str(d) for d in (path_dirs or [])] + ["/usr/bin", "/bin"]
    argv = ["/bin/bash", str(INSTALL), "--prefix", str(world["prefix"]),
            "--client-dir", str(world["clients"]), "--core-python", str(world["core_python"]),
            "--config", str(world["config"]), "--link-dir", str(world["link"]), *extra]
    return child(argv, {"PATH": ":".join(dirs), "HOME": str(world["tmp"])})


def child(argv, env):
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env)
    # The installer declares exit 0 and exit 2 (typed refusal); anything else is a crash.
    if proc.returncode not in (0, 2) or BASH_DIAGNOSTIC.search(proc.stderr):
        raise ChildError(proc.returncode, proc.stderr[-2000:])
    return proc


def refusal(proc):
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    body = json.loads(proc.stderr.strip().splitlines()[-1])
    assert body["unit"] == "ship/cli"
    return body["reason"]


def test_links_wrappers_when_path_is_free(world):
    proc = install(world, path_dirs=[world["link"]])
    assert proc.returncode == 0, proc.stderr
    for name in COMMANDS:
        wrapper = world["prefix"] / "bin" / name
        text = wrapper.read_text()
        assert "@" not in text
        assert text.startswith(f"#!{world['core_python']} -IS\n")
        assert f'CLIENT = "{world["clients"] / name}"' in text
        assert f'CORE_PYTHON = "{world["core_python"]}"' in text
        assert f'CONFIG = "{world["config"]}"' in text
        assert (world["link"] / name).resolve() == wrapper.resolve()
    assert (world["prefix"] / "cli" / "agents-empty").is_dir()
    assert list((world["prefix"] / "cli" / "agents-empty").iterdir()) == []


def test_path_collision_refused_and_nothing_written(world):
    foreign = world["other"] / "cortex-v2-log"
    foreign.write_text("#!/bin/sh\n")
    foreign.chmod(0o755)
    proc = install(world, path_dirs=[world["other"], world["link"]])
    assert refusal(proc) == f"path_collision:{foreign}"
    assert list(world["link"].iterdir()) == []
    assert not world["prefix"].exists()


def test_no_link_keeps_wrappers_private(world):
    foreign = world["other"] / "cortex-v2-boot"
    foreign.write_text("#!/bin/sh\n")
    foreign.chmod(0o755)
    proc = install(world, "--no-link", path_dirs=[world["other"], world["link"]])
    assert proc.returncode == 0, proc.stderr
    assert sorted(p.name for p in (world["prefix"] / "bin").iterdir()) == COMMANDS
    assert list(world["link"].iterdir()) == []


def test_rerun_over_own_links_is_not_a_collision(world):
    assert install(world, path_dirs=[world["link"]]).returncode == 0
    proc = install(world, path_dirs=[world["link"]])
    assert proc.returncode == 0, proc.stderr


def test_missing_client_refused(world):
    (world["clients"] / "cortex-v2-search").unlink()
    proc = install(world, path_dirs=[world["link"]])
    assert refusal(proc) == "client_missing:cortex-v2-search"
    assert not world["prefix"].exists()


def test_missing_core_python_refused(world):
    world["core_python"].unlink()
    proc = install(world, path_dirs=[world["link"]])
    assert refusal(proc) == f"core_python_missing:{world['core_python']}"
    assert not world["prefix"].exists()


@pytest.mark.parametrize("flag", ["--core-python", "--config"])
@pytest.mark.parametrize("bad", ['/tmp/a"b', "/tmp/a$b", "/tmp/a`b", "relative/path"])
def test_unsafe_path_refused(world, flag, bad):
    assert INSTALL.is_file(), f"missing {INSTALL}"
    values = {"--core-python": str(world["core_python"]), "--config": str(world["config"]), flag: bad}
    argv = ["/bin/bash", str(INSTALL), "--prefix", str(world["prefix"]),
            "--client-dir", str(world["clients"]), "--core-python", values["--core-python"],
            "--config", values["--config"], "--link-dir", str(world["link"])]
    proc = child(argv, {"PATH": "/usr/bin:/bin", "HOME": str(world["tmp"])})
    assert refusal(proc) == f"unsafe_path:{bad}"
    assert not world["prefix"].exists()


def test_v1_commands_are_never_touched(world):
    # Live v1 commands on PATH and in the link folder keep their names, bytes and links.
    v1 = {}
    for folder in (world["other"], world["link"]):
        for name in V1:
            path = folder / name
            path.write_text(f"#!/bin/sh\n# v1 {name} in {folder.name}\n")
            path.chmod(0o755)
            v1[path] = hashlib.sha256(path.read_bytes()).hexdigest()
    proc = install(world, path_dirs=[world["other"], world["link"]])
    assert proc.returncode == 0, proc.stderr
    for path, digest in v1.items():
        assert not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == digest, path
    assert sorted(p.name for p in (world["prefix"] / "bin").iterdir()) == COMMANDS
    assert sorted(p.name for p in world["link"].iterdir()) == sorted(COMMANDS + V1)
    for name in COMMANDS:
        assert (world["link"] / name).resolve() == (world["prefix"] / "bin" / name).resolve()
