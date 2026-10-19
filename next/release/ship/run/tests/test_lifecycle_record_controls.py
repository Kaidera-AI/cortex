"""S5 record join controls: the production InstallRecord port over the actual install/record.py.

Boundaries, each named in its test:
- LIFECYCLE: serve_lifecycle -> InstallRecord -> the actual record.py, with EngineDouble.
- SHELL: the ACTUAL install.sh `record_object` function text run under sh, plus record.py's
  own read entry (component level, NOT the signed full installer caller).
- CLI: `render.py lifecycle` -> InstallRecord -> record.py, with the existing fake `podman`.
Real record.py IO happens in owned NONSECRET temporary prefixes only; no Podman, key, unlock
secret or install effect exists here.
"""
import json
import subprocess
import sys
import time

import pytest

from _lifecycle_doubles import EngineDouble, unlock_pipe
from test_lifecycle_controls import FAKE_PODMAN
from test_lifecycle_deciding import load_render
from test_lifecycle_record_deciding import RECORD, RELEASE, SERVE, load_record, serve_ids
from test_render import RENDER, inputs, rendered  # noqa: F401  (inputs is a fixture)
from cortex_core.runtime.signed_member import Refusal

INSTALL_SH = RECORD.parent / "install.sh"
BASELINE = {("container", "unrelated-app"): {"id": "baseline-0001", "state": "running"}}


@pytest.fixture
def joined(inputs, tmp_path):
    render, rec = load_render(), load_record()
    _serve, plan, _inventory = rendered(inputs)
    prefix = tmp_path / "prefix"
    prefix.mkdir(mode=0o700)
    rec.record_object(str(prefix), RELEASE, "bootstrap", "cortex-v2-bootstrap", "s4-receipt-fixture")

    def call(verb, record, engine, *, unlock=None):
        return render.serve_lifecycle(verb, plan=plan, record=record, engine=engine,
                                      deadline=time.monotonic() + 30, key_id="kid-fixture",
                                      unlock_fd=unlock)
    return render, rec, prefix, call


def refused(fn):
    with pytest.raises(Refusal) as error:
        fn()
    return error.value.reason


def shell_record_object(prefix, release_id, *args):
    """SHELL: the ACTUAL install.sh `record_object` text under sh, against the actual record.py."""
    text = INSTALL_SH.read_text()
    start = text.index("record_object() {")
    function = text[start:text.index("\n}\n", start) + 3]
    script = 'HERE=$1; prefix=$2; release_id=$3; shift 3\n' + function + 'record_object "$@"\n'
    return subprocess.run(["sh", "-c", script, "sh", str(RECORD.parent), str(prefix), release_id, *args],
                          capture_output=True, text=True, timeout=60)


def test_a_record_of_another_release_is_refused_untouched_before_any_engine_effect(joined, tmp_path):
    render, rec, _prefix, call = joined
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o700)
    rec.record_object(str(foreign), "other-release", "bootstrap", "cortex-v2-bootstrap", "s4-receipt-fixture")
    rec.record_object(str(foreign), "other-release", "container", "cortex-v2-api", "container-9999")
    before = (foreign / "install-record.json").read_bytes()
    log = []
    record, engine = render.InstallRecord(foreign, RELEASE), EngineDouble(log)
    for verb in ("start", "stop", "restart", "uninstall"):
        with unlock_pipe() as unlock:
            assert refused(lambda: call(verb, record, engine, unlock=unlock)) == "record_foreign_release"
    assert log == [] and engine.objects == {}
    assert (foreign / "install-record.json").read_bytes() == before


def test_a_record_write_failure_after_a_create_removes_exactly_that_object(joined):
    render, _rec, prefix, call = joined
    before = (prefix / "install-record.json").read_bytes()
    log = []
    record, engine = render.InstallRecord(prefix, RELEASE), EngineDouble(log, baseline=BASELINE)
    prefix.chmod(0o500)  # the actual module can no longer publish: record_write_failed
    try:
        with unlock_pipe() as unlock:
            assert refused(lambda: call("start", record, engine, unlock=unlock)) == "record_write_failed"
    finally:
        prefix.chmod(0o700)
    # No next effect after the unrecorded create; its cleanup removes only that object.
    assert [entry[:3] for entry in log] == [("create", "pod", "cortex-v2"), ("remove", "pod", "cortex-v2")]
    assert engine.objects == BASELINE
    assert sorted(p.name for p in prefix.iterdir()) == ["install-record.json"]
    assert (prefix / "install-record.json").read_bytes() == before


def test_a_failed_removal_keeps_its_actual_record_entry_until_a_later_uninstall(joined):
    render, rec, prefix, call = joined
    installed = (prefix / "install-record.json").read_bytes()
    record, engine = render.InstallRecord(prefix, RELEASE), EngineDouble([])
    with unlock_pipe() as unlock:
        call("start", record, engine, unlock=unlock)
    ids = serve_ids(rec, prefix)
    engine.fail_remove = "cortex-v2-core"
    assert refused(lambda: call("uninstall", record, engine)) == "serve_cleanup_failed"
    # Forget follows only a successful removal: the object that survived stays recorded.
    assert serve_ids(rec, prefix) == {"cortex-v2-core": ids["cortex-v2-core"]}
    assert set(engine.objects) == {("container", "cortex-v2-core")}
    engine.fail_remove = None
    call("uninstall", record, engine)
    assert engine.objects == {} and (prefix / "install-record.json").read_bytes() == installed


@pytest.mark.parametrize("body", [
    None,
    "raise ImportError('broken')\n",
    "def read_record(*a):\n    pass\n",
    "RecordRefusal = 1\ndef read_record(*a):\n    pass\ndef record_object(*a):\n    pass\n"
    "def forget_object(*a):\n    pass\n",
], ids=["absent", "failing", "incomplete", "no-refusal-type"])
def test_an_absent_or_invalid_record_module_refuses_before_any_effect(joined, inputs, tmp_path, monkeypatch,
                                                                     capsys, body):
    render, _rec, prefix, _call = joined
    module = tmp_path / "selected" / "record.py"
    if body is not None:
        module.parent.mkdir()
        module.write_text(body)
    monkeypatch.setattr(render, "RECORD_MODULE", module)
    before = (prefix / "install-record.json").read_bytes()
    assert refused(lambda: render.InstallRecord(prefix, RELEASE)) == "record_module_unavailable"
    # The CLI has nothing to fall back on: one typed refusal, no engine is ever constructed.
    assert render.main(["lifecycle", "start", "--plan", str(inputs["out"] / "bootstrap.plan.json"),
                        "--prefix", str(prefix), "--release-id", RELEASE, "--seconds", "30"]) == 2
    assert json.loads(capsys.readouterr().err.strip()) == {"reason": "record_module_unavailable", "unit": "ship/run"}
    assert (prefix / "install-record.json").read_bytes() == before


@pytest.mark.parametrize("release_id", [None, "", 7])
def test_the_record_port_has_no_default_release_identity(joined, release_id):
    render, _rec, prefix, _call = joined
    assert refused(lambda: render.InstallRecord(prefix, release_id)) == "input_missing:release_id"


def test_the_shell_writer_and_the_lifecycle_share_one_record(joined):
    render, rec, prefix, call = joined
    image = "localhost/cortex-v2-api:" + RELEASE
    assert shell_record_object(prefix, RELEASE, "image", image, "sha256:" + "a" * 64).returncode == 0
    installed = (prefix / "install-record.json").read_bytes()
    record, engine = render.InstallRecord(prefix, RELEASE), EngineDouble([])
    with unlock_pipe() as unlock:
        call("start", record, engine, unlock=unlock)
    # SHELL: record.py's own read entry sees exactly what the lifecycle recorded.
    read = subprocess.run([sys.executable, "-I", "-B", str(RECORD), "read", "--prefix", str(prefix)],
                          capture_output=True, text=True, timeout=60)
    assert read.returncode == 0 and json.loads(read.stdout) == rec.read_record(str(prefix))
    assert {o["name"] for o in json.loads(read.stdout)["objects"]} == {image, "cortex-v2-bootstrap", *SERVE}
    # A shell write for another release is still refused against the lifecycle's record.
    foreign = shell_record_object(prefix, "other-release", "image", image, "sha256:" + "b" * 64)
    assert foreign.returncode == 2
    assert json.loads(foreign.stderr.strip().splitlines()[-1]) == {"reason": "record_foreign_release", "unit": "record"}
    call("uninstall", record, engine)
    assert (prefix / "install-record.json").read_bytes() == installed


def test_the_cli_lifecycle_records_through_the_actual_module_on_the_engine_command_path(joined, inputs, tmp_path):
    _render, rec, prefix, _call = joined
    fake = tmp_path / "bin" / "podman"
    fake.parent.mkdir()
    fake.write_text(f"#!{sys.executable}\n" + FAKE_PODMAN)
    fake.chmod(0o755)
    state = tmp_path / "podman-state.json"
    env = {"PATH": f"{fake.parent}:/usr/bin:/bin", "FAKE_PODMAN_STATE": str(state)}
    installed = (prefix / "install-record.json").read_bytes()
    cli = [sys.executable, "-I", "-B", str(RENDER), "lifecycle"]
    common = ["--plan", str(inputs["out"] / "bootstrap.plan.json"), "--prefix", str(prefix),
              "--release-id", RELEASE, "--seconds", "30"]
    with unlock_pipe() as unlock:
        started = subprocess.run(cli + ["start", *common, "--key-id", "kid-fixture", "--unlock-fd", str(unlock)],
                                 capture_output=True, text=True, timeout=60, env=env, pass_fds=(unlock,))
    assert started.returncode == 0, started.stderr
    assert {o["name"]: o["id"] for o in json.loads(started.stdout)["objects"]} == serve_ids(rec, prefix) == {
        "cortex-v2": format(1, "064x"), "cortex-v2-core": format(2, "064x"), "cortex-v2-api": format(3, "064x")}
    removed = subprocess.run(cli + ["uninstall", *common], capture_output=True, text=True, timeout=60, env=env)
    assert removed.returncode == 0, removed.stderr
    assert json.loads(state.read_text())["objects"] == {}
    assert (prefix / "install-record.json").read_bytes() == installed
