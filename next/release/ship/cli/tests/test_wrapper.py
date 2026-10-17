"""Spec for ship/cli/wrapper.sh.in: env pinning and projection refusal paths."""
import json
import os
import pathlib
import stat
import subprocess

import pytest

HERE = pathlib.Path(__file__).resolve().parent
TEMPLATE = HERE.parent / "wrapper.sh.in"

FAKE_CLIENT = """#!/usr/bin/env python3
import json, os, sys
keys = [k for k in os.environ if k.startswith("CORTEX_")]
json.dump({"env": {k: os.environ[k] for k in keys}, "argv": sys.argv[1:], "cwd": os.getcwd()},
          open(os.environ["FAKE_CLIENT_OUT"], "w"))
"""


def write_exec(path, text):
    path.write_text(text)
    path.chmod(0o755)
    return path


@pytest.fixture
def world(tmp_path):
    cred = tmp_path / "cred" / "bob.token"
    cred.parent.mkdir()
    cred.write_text("not-a-real-token\n")
    cred.chmod(0o600)
    empty = tmp_path / "empty-agents"
    empty.mkdir()
    client = write_exec(tmp_path / "client", FAKE_CLIENT)
    lines = ["CORTEX_API_URL=http://127.0.0.1:18601", "CORTEX_PROJECT=helix",
             f"CORTEX_CREDENTIAL_FILE={cred}"]
    return {"tmp": tmp_path, "cred": cred, "empty": empty, "client": client,
            "out": tmp_path / "client-out.json", "lines": lines}


def render(world, lines=None, rc=0):
    assert TEMPLATE.is_file(), f"missing {TEMPLATE}"
    lines = world["lines"] if lines is None else lines
    body = "".join(f"echo '{line}'\n" for line in lines)
    projector = write_exec(world["tmp"] / "projector", f"#!/bin/sh\n{body}exit {rc}\n")
    text = TEMPLATE.read_text()
    text = text.replace("@CLIENT@", str(world["client"]))
    text = text.replace("@PROJECTOR@", str(projector))
    text = text.replace("@EMPTY_AGENTS@", str(world["empty"]))
    return write_exec(world["tmp"] / "cortex-boot", text)


def call(world, wrapper, cwd=None, extra_env=None):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(world["tmp"]),
           "FAKE_CLIENT_OUT": str(world["out"])}
    env.update(extra_env or {})
    return subprocess.run([str(wrapper), "bob", "--flag"], capture_output=True, text=True,
                          cwd=cwd or world["tmp"], env=env, timeout=30)


def refusal(world, proc):
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    lines = proc.stderr.strip().splitlines()
    assert len(lines) == 1, proc.stderr
    body = json.loads(lines[0])
    assert body["unit"] == "ship/cli"
    assert not world["out"].exists(), "client ran after a refusal"
    return body["reason"]


HOSTILE = {"CORTEX_API_URL": "http://localhost:8501", "CORTEX_API": "http://localhost:8501",
           "CORTEX_ADMIN_TOKEN": "admin", "CORTEX_CTO_OVERRIDE": "H-D000",
           "CORTEX_API_WITH_ADMIN": "1", "CORTEX_PROJECT": "other", "CORTEX_PROJECT_ID": "x",
           "CORTEX_PROJECT_ROOT": "/", "CORTEX_WORKSPACE_PROJECT": "other",
           "CORTEX_CREDENTIAL_FILE": "/etc/passwd"}


def test_wrapper_overrides_hostile_env(world):
    proc = call(world, render(world), extra_env=HOSTILE)
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(world["out"].read_text())
    assert seen["argv"] == ["bob", "--flag"]
    assert seen["env"] == {"CORTEX_API_URL": "http://127.0.0.1:18601",
                           "CORTEX_PROJECT": "helix",
                           "CORTEX_CREDENTIAL_FILE": str(world["cred"]),
                           "CORTEX_AGENTS_DIR": str(world["empty"]),
                           "CORTEX_RUNTIME_CONFIG": "/dev/null",
                           "CORTEX_WORKSPACE_CONFIG": "/dev/null"}


def test_cwd_runtime_yaml_does_not_change_target(world):
    repo = world["tmp"] / "repo"
    (repo / ".agents" / "config").mkdir(parents=True)
    (repo / ".agents" / "config" / "runtime.yaml").write_text("project:\n  name: v1team\n")
    (repo / ".agents" / "config" / "workspace.json").write_text('{"project": "v1team"}')
    proc = call(world, render(world), cwd=repo)
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(world["out"].read_text())
    assert seen["env"]["CORTEX_API_URL"] == "http://127.0.0.1:18601"
    assert seen["env"]["CORTEX_PROJECT"] == "helix"
    assert seen["env"]["CORTEX_AGENTS_DIR"] == str(world["empty"])


def test_projector_failure_refuses(world):
    assert refusal(world, call(world, render(world, rc=3))) == "projection_failed:3"


@pytest.mark.parametrize("drop", [0, 1, 2])
def test_projection_field_missing(world, drop):
    lines = [line for i, line in enumerate(world["lines"]) if i != drop]
    key = world["lines"][drop].split("=", 1)[0]
    assert refusal(world, call(world, render(world, lines))) == f"projection_field_missing:{key}"


def test_projection_field_unknown(world):
    lines = world["lines"] + ["CORTEX_ADMIN_TOKEN=leak"]
    assert refusal(world, call(world, render(world, lines))) == "projection_field_unknown:CORTEX_ADMIN_TOKEN"


def test_projection_db_key_is_unknown(world):
    lines = world["lines"][:2] + ["DATABASE_PASSWORD=x", world["lines"][2]]
    assert refusal(world, call(world, render(world, lines))) == "projection_field_unknown:DATABASE_PASSWORD"


@pytest.mark.parametrize("url", ["http://localhost:8501", "http://127.0.0.1:8501",
                                 "http://localhost:18601", "https://127.0.0.1:18601",
                                 "http://127.0.0.1:18601/x", "http://10.0.0.5:18601",
                                 "http://127.0.0.1:"])
def test_url_not_v2_refuses(world, url):
    lines = [f"CORTEX_API_URL={url}"] + world["lines"][1:]
    assert refusal(world, call(world, render(world, lines))) == f"url_not_v2:{url}"


def test_credential_missing(world):
    world["cred"].unlink()
    assert refusal(world, call(world, render(world))) == f"credential_missing:{world['cred']}"


def test_credential_wrong_mode(world):
    world["cred"].chmod(0o644)
    assert refusal(world, call(world, render(world))) == f"credential_unsafe:{world['cred']}"


def test_credential_symlink(world):
    real = world["tmp"] / "real.token"
    real.write_text("x")
    real.chmod(0o600)
    world["cred"].unlink()
    os.symlink(real, world["cred"])
    assert refusal(world, call(world, render(world))) == f"credential_unsafe:{world['cred']}"


def test_template_has_no_admin_path():
    assert TEMPLATE.is_file(), f"missing {TEMPLATE}"
    text = TEMPLATE.read_text()
    assert "exec \"@CLIENT@\" \"$@\"" in text
    assert stat.S_ISREG(TEMPLATE.stat().st_mode)
