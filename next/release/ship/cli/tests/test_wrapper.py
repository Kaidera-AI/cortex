"""Spec for ship/cli/launcher.py.in: the sole client launcher over Core's private projection.

SOURCE ONLY: the projector is the actual Core `runtime.operator config client` over actual
encrypted custody, staged with public fixture bytes and no SQL enrollment; the client is a fake
that reads the inherited pipe. Nothing here grants installed, native or Podman credit.
"""
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import venv

import pytest

HERE = pathlib.Path(__file__).resolve().parent
TEMPLATE = HERE.parent / "launcher.py.in"
REPO_NEXT = HERE.parents[3]
sys.path.insert(0, str(REPO_NEXT / "src"))
from cortex_core.runtime.bootstrap_config import decode_config  # noqa: E402
from cortex_core.runtime.custody import open_custody  # noqa: E402
from cortex_core.runtime.tests.test_bootstrap_config import sample  # noqa: E402
from cortex_core.runtime.tests.test_custody import KEY, SECRET, intent, receipt  # noqa: E402

KEY_ID = "public-fixture-key"
# bash/python report their own failures as "<script>: line N: ..." or a traceback.
CRASH = re.compile(r": line \d+: |Traceback \(most recent call last\)")

FAKE_CLIENT = """#!{python}
import hashlib, json, os, sys
fd = int(os.environ["CORTEX_CLIENT_PROJECTION_FD"])
raw = b""
while True:
    chunk = os.read(fd, 8192)
    if not chunk:
        break
    raw += chunk
fields = raw.split(b"\\0")
unlock = os.environ.get("FAKE_UNLOCK_FD")
try:
    os.fstat(int(unlock))
    unlock_open = True
except (OSError, TypeError, ValueError):
    unlock_open = False
report = {{"env": {{k: v for k, v in os.environ.items() if k.startswith("CORTEX_")}},
          "argv": sys.argv[1:], "fields": [f.decode() for f in fields[:5]] + [
              hashlib.sha256(fields[5]).hexdigest() if len(fields) > 5 else None] + [
              f.decode() for f in fields[6:]], "unlock_open": unlock_open}}
json.dump(report, open(os.environ["FAKE_CLIENT_OUT"], "w"))
"""


class ChildError(Exception):
    """The launcher or fake client crashed: never a behavioral assertion."""


@pytest.fixture(scope="module")
def core_python(tmp_path_factory):
    # An interpreter where Core is importable under -I, like the installed Core closure.
    root = tmp_path_factory.mktemp("core-python")
    venv.create(root, with_pip=False, symlinks=True)
    site = next(root.glob("lib/python*/site-packages"))
    paths = [str(REPO_NEXT / "src")] + [p for p in sys.path if p.endswith("site-packages")]
    (site / "_cortex_core_source.pth").write_text("\n".join(paths) + "\n")
    return root / "bin" / "python3"


def stage(root, *, committed=True):
    root.chmod(0o700)
    for sub in ("config", "secrets", "secrets/cortex"):
        (root / sub).mkdir(mode=0o700)
    value = sample(root)
    path = root / "config" / "cortex.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    config = decode_config(json.dumps(value).encode(), mode="seed")
    unlock, writer = os.pipe()
    os.write(writer, KEY)
    os.close(writer)
    try:
        with open_custody(config, unlock_fd=unlock, key_id=KEY_ID) as vault:
            with vault.seed_record() as record:
                expected = intent(value)
                record.create(intent=expected, credential=SECRET)
                if committed:
                    record.commit(receipt=receipt(expected))
    finally:
        os.close(unlock)
    return value, path


@pytest.fixture
def world(tmp_path, core_python):
    vault = tmp_path.resolve() / "vault"
    vault.mkdir()
    value, config = stage(vault)
    empty = tmp_path / "empty-agents"
    empty.mkdir()
    client = tmp_path / "client"
    client.write_text(FAKE_CLIENT.format(python=sys.executable))
    client.chmod(0o755)
    return {"tmp": tmp_path, "vault": vault, "value": value, "config": config, "empty": empty,
            "client": client, "core_python": core_python, "out": tmp_path / "client-out.json"}


def render(world, config=None):
    assert TEMPLATE.is_file(), f"missing {TEMPLATE}"
    text = TEMPLATE.read_text()
    for key, value in (("@CLIENT@", world["client"]), ("@CORE_PYTHON@", world["core_python"]),
                       ("@CONFIG@", config or world["config"]), ("@EMPTY_AGENTS@", world["empty"])):
        text = text.replace(key, str(value))
    launcher = world["tmp"] / "cortex-boot"
    launcher.write_text(text)
    launcher.chmod(0o755)
    return launcher


def call(world, launcher, *, key=KEY, unlock=True, key_id=KEY_ID, extra_env=None, cwd=None):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(world["tmp"]), "FAKE_CLIENT_OUT": str(world["out"])}
    fds = ()
    reader = None
    if unlock:
        reader, writer = os.pipe()
        os.write(writer, key)
        os.close(writer)
        fds = (reader,)
        env.update(CORTEX_UNLOCK_FD=str(reader), FAKE_UNLOCK_FD=str(reader))
    if key_id is not None:
        env["CORTEX_KEY_ID"] = key_id
    env.update(extra_env or {})
    try:
        proc = subprocess.run([str(launcher), "bob", "--flag"], capture_output=True, text=True,
                              cwd=cwd or world["tmp"], env=env, pass_fds=fds, timeout=30)
    finally:
        if reader is not None:
            os.close(reader)
    # The launcher declares exit 2 (one typed refusal); the fake client exits 0.
    if proc.returncode not in (0, 2) or CRASH.search(proc.stderr):
        raise ChildError(proc.returncode, proc.stderr[-2000:])
    assert SECRET.decode() not in proc.stdout + proc.stderr, "credential reached launcher output"
    return proc


def refusal(world, proc):
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    lines = proc.stderr.strip().splitlines()
    assert len(lines) == 1, proc.stderr
    body = json.loads(lines[0])
    assert body["unit"] == "ship/cli"
    assert not world["out"].exists(), "client ran after a refusal"
    return body["reason"]


def core_refusal(world, key):
    # The actual operator's own typed reason for the same input, run directly.
    reader, writer = os.pipe()
    unlock, key_writer = os.pipe()
    os.write(key_writer, key)
    os.close(key_writer)
    try:
        proc = subprocess.run([str(world["core_python"]), "-I", "-B", "-m", "cortex_core.runtime.operator",
                               "config", "client", "--config", str(world["config"]),
                               "--unlock-fd", str(unlock), "--key-id", KEY_ID,
                               "--projection-fd", str(writer)],
                              pass_fds=(unlock, writer), capture_output=True, text=True, timeout=30)
    finally:
        for fd in (reader, writer, unlock):
            try:
                os.close(fd)
            except OSError:
                pass
    assert proc.returncode == 2, proc.stderr
    return json.loads(proc.stderr)["error"]["reason"]


HOSTILE = {"CORTEX_API_URL": "http://localhost:8501", "CORTEX_API": "http://localhost:8501",
           "CORTEX_ADMIN_TOKEN": "admin", "CORTEX_CTO_OVERRIDE": "H-D000",
           "CORTEX_API_WITH_ADMIN": "1", "CORTEX_PROJECT": "other", "CORTEX_PROJECT_ID": "x",
           "CORTEX_PROJECT_ROOT": "/", "CORTEX_WORKSPACE_PROJECT": "other",
           "CORTEX_CREDENTIAL_FILE": "/etc/passwd", "CORTEX_CLIENT_PROJECTION_FD": "0"}


def test_launcher_projects_through_actual_core_and_scrubs_env(world):
    proc = call(world, render(world), extra_env=HOSTILE)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    seen = json.loads(world["out"].read_text())
    assert seen["argv"] == ["bob", "--flag"]
    fd = seen["env"]["CORTEX_CLIENT_PROJECTION_FD"]
    assert seen["env"] == {"CORTEX_CLIENT_PROJECTION_FD": fd,
                           "CORTEX_AGENTS_DIR": str(world["empty"]),
                           "CORTEX_RUNTIME_CONFIG": "/dev/null",
                           "CORTEX_WORKSPACE_CONFIG": "/dev/null"}
    assert fd not in ("0", "1", "2")
    value = world["value"]
    assert seen["fields"] == ["cortex.client.v1", "http://127.0.0.1:8501", value["installation_id"],
                              value["project_id"], "sample", hashlib.sha256(SECRET).hexdigest(),
                              "10", ""]
    assert seen["unlock_open"] is False, "the unlock descriptor reached the client"


def test_cwd_runtime_yaml_does_not_change_target(world):
    repo = world["tmp"] / "repo"
    (repo / ".agents" / "config").mkdir(parents=True)
    (repo / ".agents" / "config" / "runtime.yaml").write_text("project:\n  name: v1team\n")
    (repo / ".agents" / "config" / "workspace.json").write_text('{"project": "v1team"}')
    proc = call(world, render(world), cwd=repo)
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(world["out"].read_text())
    assert seen["fields"][1] == "http://127.0.0.1:8501"
    assert seen["fields"][4] == "sample"
    assert seen["env"]["CORTEX_AGENTS_DIR"] == str(world["empty"])


@pytest.mark.parametrize("unlock,key_id,extra", [
    (False, KEY_ID, {}),
    (True, None, {}),
    (False, KEY_ID, {"CORTEX_UNLOCK_FD": "3x"}),
    (False, KEY_ID, {"CORTEX_UNLOCK_FD": "987"}),
], ids=["no-unlock-fd", "no-key-id", "fd-not-decimal", "fd-not-open"])
def test_unlock_input_is_explicit(world, unlock, key_id, extra):
    proc = call(world, render(world), unlock=unlock, key_id=key_id, extra_env=extra)
    assert refusal(world, proc) == "unlock_unconfigured"


def test_wrong_key_is_cores_one_refusal(world):
    wrong = bytes(reversed(KEY))
    proc = call(world, render(world), key=wrong)
    assert refusal(world, proc) == f"projection_failed:{core_refusal(world, wrong)}"
    assert "cortex.operator.v1" not in proc.stderr, "raw projector stderr was forwarded"


def test_uncommitted_record_refuses_before_client(world):
    vault = world["tmp"].resolve() / "vault-prepared"
    vault.mkdir()
    _value, config = stage(vault, committed=False)
    proc = call(world, render(world, config=config))
    reason = refusal(world, proc)
    assert reason.startswith("projection_failed:") and reason != "projection_failed:2"


def test_missing_config_is_cores_refusal(world):
    proc = call(world, render(world, config=world["tmp"] / "absent.json"))
    assert refusal(world, proc) == "projection_failed:private_file_required"


def test_template_has_no_admin_path():
    assert TEMPLATE.is_file(), f"missing {TEMPLATE}"
    text = TEMPLATE.read_text()
    assert text.startswith("#!@CORE_PYTHON@ -IS\n")
    assert "ADMIN" not in text and "CTO_OVERRIDE" not in text
    assert '"cortex_core.runtime.operator", "config", "client"' in text
    assert "os.execve(CLIENT" in text
    assert stat.S_ISREG(TEMPLATE.stat().st_mode)
