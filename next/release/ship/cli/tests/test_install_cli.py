"""Spec for ship/cli/install_cli.sh: render wrappers, refuse PATH collisions."""
import json
import pathlib
import subprocess

import pytest

HERE = pathlib.Path(__file__).resolve().parent
INSTALL = HERE.parent / "install_cli.sh"
COMMANDS = ["cortex-boot", "cortex-log", "cortex-search"]


@pytest.fixture
def world(tmp_path):
    clients = tmp_path / "clients"
    clients.mkdir()
    for name in COMMANDS:
        path = clients / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o555)
    projector = tmp_path / "projector"
    projector.write_text("#!/bin/sh\nexit 0\n")
    projector.chmod(0o755)
    link = tmp_path / "link"
    link.mkdir()
    other = tmp_path / "other-bin"
    other.mkdir()
    return {"tmp": tmp_path, "clients": clients, "projector": projector, "link": link,
            "other": other, "prefix": tmp_path / "prefix"}


def install(world, *extra, path_dirs=None):
    assert INSTALL.is_file(), f"missing {INSTALL}"
    dirs = [str(d) for d in (path_dirs or [])] + ["/usr/bin", "/bin"]
    argv = ["/bin/bash", str(INSTALL), "--prefix", str(world["prefix"]),
            "--client-dir", str(world["clients"]), "--projector", str(world["projector"]),
            "--link-dir", str(world["link"]), *extra]
    return subprocess.run(argv, capture_output=True, text=True, timeout=30,
                          env={"PATH": ":".join(dirs), "HOME": str(world["tmp"])})


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
        assert "@" not in text.replace("\"$@\"", "")
        assert f'exec "{world["clients"] / name}" "$@"' in text
        assert str(world["projector"]) in text
        assert (world["link"] / name).resolve() == wrapper.resolve()
    assert (world["prefix"] / "cli" / "agents-empty").is_dir()
    assert list((world["prefix"] / "cli" / "agents-empty").iterdir()) == []


def test_path_collision_refused_and_nothing_written(world):
    foreign = world["other"] / "cortex-log"
    foreign.write_text("#!/bin/sh\n")
    foreign.chmod(0o755)
    proc = install(world, path_dirs=[world["other"], world["link"]])
    assert refusal(proc) == f"path_collision:{foreign}"
    assert list(world["link"].iterdir()) == []
    assert not world["prefix"].exists()


def test_no_link_keeps_wrappers_private(world):
    foreign = world["other"] / "cortex-boot"
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
    (world["clients"] / "cortex-search").unlink()
    proc = install(world, path_dirs=[world["link"]])
    assert refusal(proc) == "client_missing:cortex-search"
    assert not world["prefix"].exists()


@pytest.mark.parametrize("bad", ['/tmp/a"b', "/tmp/a$b", "/tmp/a`b", "relative/path"])
def test_unsafe_projector_path_refused(world, bad):
    assert INSTALL.is_file(), f"missing {INSTALL}"
    argv = ["/bin/bash", str(INSTALL), "--prefix", str(world["prefix"]),
            "--client-dir", str(world["clients"]), "--projector", bad,
            "--link-dir", str(world["link"])]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(world["tmp"])})
    assert refusal(proc) == f"unsafe_path:{bad}"
    assert not world["prefix"].exists()
