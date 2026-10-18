"""S5 lifecycle controls: refusals before effects, ownership, cleanup, deadline, engine path.

Named doubles only (_lifecycle_doubles); the PodmanEngine case runs a fake `podman` script,
never the real engine. No unlock, secret, network or container is real.
"""
import copy
import json
import os
import subprocess
import sys
import time

import pytest

from _lifecycle_doubles import BOOTSTRAP, EngineDouble, RecordDouble, unlock_pipe
from test_lifecycle_deciding import load_render
from test_render import RENDER, inputs, rendered  # noqa: F401  (inputs is a fixture)
from cortex_core.runtime.signed_member import Refusal

SERVE = ("cortex-v2", "cortex-v2-core", "cortex-v2-api")


@pytest.fixture
def lifecycle(inputs):
    render = load_render()
    _serve, plan, _inventory = rendered(inputs)
    log = []

    def call(verb, record, engine, *, unlock=None, deadline=None, key_id="kid-fixture"):
        return render.serve_lifecycle(verb, plan=plan, record=record, engine=engine,
                                      deadline=time.monotonic() + 30 if deadline is None else deadline,
                                      key_id=key_id, unlock_fd=unlock)
    return render, plan, log, call


def started(call, log):
    record, engine = RecordDouble(log, objects=[BOOTSTRAP]), EngineDouble(log)
    with unlock_pipe() as unlock:
        call("start", record, engine, unlock=unlock)
    log.clear()
    return record, engine


def refused(fn):
    with pytest.raises(Refusal) as error:
        fn()
    return error.value.reason


@pytest.mark.parametrize("case", ["missing", "stdout", "closed", "regular-file", "write-end", "consumed"])
def test_start_refuses_a_wrong_unlock_descriptor_before_any_effect(lifecycle, tmp_path, case):
    _render, _plan, log, call = lifecycle
    record, engine = RecordDouble(log, objects=[BOOTSTRAP]), EngineDouble(log)
    if case in ("missing", "stdout"):
        fd, expected = (None if case == "missing" else 1), "unlock_required"
        assert refused(lambda: call("start", record, engine, unlock=fd)) == expected
    elif case == "closed":
        read_end, write_end = os.pipe()
        os.close(read_end)
        os.close(write_end)
        assert refused(lambda: call("start", record, engine, unlock=read_end)) == "unlock_invalid"
    elif case == "regular-file":
        path = tmp_path / "key"
        path.write_bytes(b"x" * 32)
        fd = os.open(path, os.O_RDONLY)
        try:
            assert refused(lambda: call("start", record, engine, unlock=fd)) == "unlock_invalid"
        finally:
            os.close(fd)
    elif case == "write-end":
        read_end, write_end = os.pipe()
        try:
            assert refused(lambda: call("start", record, engine, unlock=write_end)) == "unlock_invalid"
        finally:
            os.close(read_end)
            os.close(write_end)
    else:
        with unlock_pipe() as unlock:
            os.read(unlock, 64)
            assert refused(lambda: call("start", record, engine, unlock=unlock)) == "unlock_stale"
    assert log == [] and record.objects() == [BOOTSTRAP]


def test_start_needs_the_finite_bootstrap_completion_object(lifecycle):
    _render, _plan, log, call = lifecycle
    record, engine = RecordDouble(log), EngineDouble(log)
    with unlock_pipe() as unlock:
        assert refused(lambda: call("start", record, engine, unlock=unlock)) == "bootstrap_incomplete"
    assert log == []


def test_production_record_refuses_every_write_before_any_engine_effect(lifecycle, tmp_path):
    render, _plan, log, call = lifecycle
    prefix = tmp_path / "prefix"
    prefix.mkdir(mode=0o700)
    (prefix / "install-record.json").write_text(json.dumps({"release_id": "r", "objects": [BOOTSTRAP]}))
    record, engine = render.InstallRecord(prefix), EngineDouble(log)
    assert record.objects() == [BOOTSTRAP]
    for verb in ("start", "restart", "uninstall"):
        with unlock_pipe() as unlock:
            assert refused(lambda: call(verb, record, engine, unlock=unlock)) == "record_owner_unbound"
    assert refused(lambda: call("stop", record, engine)) == "serve_not_recorded"
    assert log == []


def test_an_unrecorded_object_with_our_name_is_a_collision_never_adopted(lifecycle):
    _render, _plan, log, call = lifecycle
    record = RecordDouble(log, objects=[BOOTSTRAP])
    engine = EngineDouble(log, baseline={("container", "cortex-v2-core"): {"id": "theirs", "state": "running"}})
    with unlock_pipe() as unlock:
        assert refused(lambda: call("start", record, engine, unlock=unlock)) == "serve_collision:cortex-v2-core"
    assert log == [] and engine.objects == {("container", "cortex-v2-core"): {"id": "theirs", "state": "running"}}


def test_start_twice_refuses_and_restart_is_the_explicit_path(lifecycle):
    _render, _plan, log, call = lifecycle
    record, engine = started(call, log)
    with unlock_pipe() as unlock:
        assert refused(lambda: call("start", record, engine, unlock=unlock)) == "serve_already_recorded"
    assert log == []


@pytest.mark.parametrize("verb", ["stop", "restart", "uninstall"])
def test_a_recorded_name_now_holding_another_object_is_foreign_and_untouched(lifecycle, verb):
    _render, _plan, log, call = lifecycle
    record, engine = started(call, log)
    engine.objects[("pod", "cortex-v2")]["id"] = "replaced-by-someone-else"
    before = copy.deepcopy(engine.objects)
    with unlock_pipe() as unlock:
        assert refused(lambda: call(verb, record, engine, unlock=unlock)) == "serve_foreign:cortex-v2"
    assert log == [] and engine.objects == before
    assert {o["name"] for o in record.objects()} == {BOOTSTRAP["name"], *SERVE}


def test_a_partial_start_removes_exactly_what_it_created_and_never_retries(lifecycle):
    _render, _plan, log, call = lifecycle
    record = RecordDouble(log, objects=[BOOTSTRAP])
    engine = EngineDouble(log, fail_on="cortex-v2-core")
    with unlock_pipe() as unlock:
        assert refused(lambda: call("start", record, engine, unlock=unlock)) == "serve_start_failed"
    assert [entry[:3] for entry in log] == [
        ("create", "pod", "cortex-v2"), ("record", "pod", "cortex-v2"),
        ("create", "container", "cortex-v2-core"),
        ("remove", "pod", "cortex-v2"), ("forget", "cortex-v2")]
    assert engine.objects == {} and record.objects() == [BOOTSTRAP]


def test_an_interrupt_during_start_still_cleans_up_and_propagates(lifecycle):
    _render, _plan, log, call = lifecycle
    record = RecordDouble(log, objects=[BOOTSTRAP])

    class Interrupting(EngineDouble):
        def create(self, argv, *, unlock_fd=None, deadline):
            if "cortex-v2-api" in argv:
                raise KeyboardInterrupt
            return super().create(argv, unlock_fd=unlock_fd, deadline=deadline)
    engine = Interrupting(log)
    with unlock_pipe() as unlock, pytest.raises(KeyboardInterrupt):
        call("start", record, engine, unlock=unlock)
    assert engine.objects == {} and record.objects() == [BOOTSTRAP]


def test_a_failed_cleanup_keeps_its_record_and_is_not_success(lifecycle):
    _render, _plan, log, call = lifecycle
    record = RecordDouble(log, objects=[BOOTSTRAP])
    engine = EngineDouble(log, fail_on="cortex-v2-api", fail_remove="cortex-v2-core")
    with unlock_pipe() as unlock:
        assert refused(lambda: call("start", record, engine, unlock=unlock)) == "serve_cleanup_failed"
    assert set(engine.objects) == {("container", "cortex-v2-core")}
    assert {o["name"] for o in record.objects()} == {BOOTSTRAP["name"], "cortex-v2-core"}
    # The surviving object stays visible to a later explicit uninstall.
    engine.fail_remove = None
    call("uninstall", record, engine)
    assert engine.objects == {} and record.objects() == [BOOTSTRAP]


def test_uninstall_forgets_an_already_gone_object_and_removes_the_rest(lifecycle):
    _render, _plan, log, call = lifecycle
    record, engine = started(call, log)
    del engine.objects[("container", "cortex-v2-api")]
    call("uninstall", record, engine)
    assert engine.objects == {} and record.objects() == [BOOTSTRAP]


def test_an_expired_original_deadline_refuses_before_any_effect(lifecycle):
    _render, _plan, log, call = lifecycle
    record, engine = RecordDouble(log, objects=[BOOTSTRAP]), EngineDouble(log)
    with unlock_pipe() as unlock:
        assert refused(lambda: call("start", record, engine, unlock=unlock,
                                    deadline=time.monotonic() - 1)) == "deadline"
    assert log == []


def test_the_executed_creates_are_the_rendered_ones_with_only_the_key_input_filled(lifecycle):
    _render, plan, log, call = lifecycle
    record, engine = RecordDouble(log, objects=[BOOTSTRAP]), EngineDouble(log)
    with unlock_pipe() as unlock:
        call("start", record, engine, unlock=unlock)
    rendered_argv = next(s for s in plan["steps"] if s["name"] == "serve")["podman"]
    executed = [list(entry[3]) for entry in log if entry[0] == "create"]
    expected = [[("kid-fixture" if arg == {"input": "key_id"} else arg) for arg in argv]
                for argv in rendered_argv]
    assert executed == expected
    api = executed[2]
    assert "--restart=no" in api and "--preserve-fds=1" in api
    assert api.count("--publish") == 0 and executed[0].count("--publish") == 1


def test_missing_key_input_refuses_and_cleans_up(lifecycle):
    _render, _plan, log, call = lifecycle
    record, engine = RecordDouble(log, objects=[BOOTSTRAP]), EngineDouble(log)
    with unlock_pipe() as unlock:
        assert refused(lambda: call("start", record, engine, unlock=unlock, key_id="")) == "input_missing:key_id"
    assert engine.objects == {} and record.objects() == [BOOTSTRAP]


FAKE_PODMAN = r'''
import json, os, sys
state_path = os.environ["FAKE_PODMAN_STATE"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {"objects": {}, "calls": [], "serial": 0}
args = sys.argv[1:]
state["calls"].append(args)
def save():
    json.dump(state, open(state_path, "w"))
def key(kind, name):
    return kind + ":" + name
code = 0
if args[1:2] == ["exists"]:
    code = 0 if key(args[0], args[2]) in state["objects"] else 1
elif args[1:2] == ["inspect"]:
    print(state["objects"][key(args[0], args[-1])])
elif args[:2] == ["pod", "create"] or args[:1] == ["run"]:
    kind = "pod" if args[0] == "pod" else "container"
    name = args[args.index("--name") + 1]
    if "--preserve-fds=1" in args:
        state.setdefault("unlock", []).append([name, os.read(3, 64).decode()])
    state["serial"] += 1
    ident = format(state["serial"], "064x")
    state["objects"][key(kind, name)] = ident
    print(ident)
elif args[:2] in (["pod", "stop"], ["stop"]) or args[:1] == ["stop"]:
    pass
elif args[:2] == ["pod", "rm"] or args[:1] == ["rm"]:
    ident = args[-1]
    state["objects"] = {k: v for k, v in state["objects"].items() if v != ident}
else:
    code = 125
save()
sys.exit(code)
'''


def test_podman_engine_drives_the_supported_command_path_with_unlock_as_fd3(lifecycle, tmp_path, monkeypatch):
    render, _plan, log, call = lifecycle
    fake = tmp_path / "bin" / "podman"
    fake.parent.mkdir()
    fake.write_text(f"#!{sys.executable}\n" + FAKE_PODMAN)
    fake.chmod(0o755)
    state = tmp_path / "podman-state.json"
    monkeypatch.setenv("FAKE_PODMAN_STATE", str(state))
    engine = render.PodmanEngine(program=str(fake))
    record = RecordDouble(log, objects=[BOOTSTRAP])
    with unlock_pipe() as unlock:
        call("start", record, engine, unlock=unlock)
    doc = json.loads(state.read_text())
    assert doc["unlock"] == [["cortex-v2-api", "S5-SYNTHETIC-NONSECRET-UNLOCK-32"]]
    assert {o["name"]: o["id"] for o in record.objects() if o["kind"] != "bootstrap"} == {
        "cortex-v2": format(1, "064x"), "cortex-v2-core": format(2, "064x"), "cortex-v2-api": format(3, "064x")}
    call("stop", record, engine)
    with unlock_pipe() as unlock:
        call("restart", record, engine, unlock=unlock)
    call("uninstall", record, engine)
    doc = json.loads(state.read_text())
    assert doc["objects"] == {} and record.objects() == [BOOTSTRAP]
    verbs = [tuple(c[:2]) for c in doc["calls"]]
    assert ("pod", "stop") in verbs and ("rm", "--force") in verbs and ("pod", "rm") in verbs


def test_cli_lifecycle_refuses_typed_while_the_record_writer_is_unbound(inputs, tmp_path):
    _serve, _plan, _inventory = rendered(inputs)
    prefix = tmp_path / "prefix"
    prefix.mkdir(mode=0o700)
    proc = subprocess.run([sys.executable, "-I", str(RENDER), "lifecycle", "start",
                           "--plan", str(inputs["out"] / "bootstrap.plan.json"), "--prefix", str(prefix),
                           "--seconds", "30", "--key-id", "kid-fixture", "--unlock-fd", "9"],
                          capture_output=True, text=True, timeout=60, env={"PATH": "/nonexistent"})
    assert proc.returncode == 2, proc.stderr
    assert json.loads(proc.stderr.strip()) == {"reason": "record_owner_unbound", "unit": "ship/run"}
