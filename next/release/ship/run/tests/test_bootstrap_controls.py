"""S4 controls: the fixed finite bootstrap refuses before effects, admits only the bound owner,
cleans up within the original deadline and never records completion early.

Every case runs the ACTUAL Core owner, Nix's provision_sequence and the actual install record
over the named NONSECRET doubles of _bootstrap_stage; the engine is the labeled double.
"""
import copy
import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

import _bootstrap_stage as S
from _lifecycle_doubles import EngineDouble, RecordDouble, unlock_pipe
from cortex_core.runtime.bootstrap_config import decode_config
from cortex_core.runtime.signed_member import Refusal
from test_lifecycle_deciding import load_render
from test_lifecycle_record_deciding import RELEASE, load_record
from test_render import inputs, rendered, write_config  # noqa: F401  (inputs is a fixture)

FINITE = ("cortex-v2-bootstrap", "cortex-v2-bootstrap-core")
TRACE = {("run", "custody.py"), ("record_object", "record.py")}


class VolumeEngine(S.BootstrapEngine):
    """The S4 engine double plus data-volume removal; it records the call, argv/input only."""

    def remove(self, kind, ident, *, deadline):
        if kind != "volume":
            return super().remove(kind, ident, deadline=deadline)
        if ident not in self.volumes or self.fail_remove == ident:
            raise OSError("engine remove failed (double)")
        self.volumes.discard(ident)
        self.log.append(("remove", kind, ident, ident))


@pytest.fixture
def boot(inputs, tmp_path):
    render, rec = load_render(), load_record()
    write_config(inputs, S.install_config(str(inputs["vault"])))
    _serve, plan, _inventory = rendered(inputs)
    config = decode_config(inputs["config"].read_bytes(), mode="install")
    prefix = tmp_path / "prefix"
    prefix.mkdir(mode=0o700)
    events = []
    with S.composed(render, config) as doubles, S.traced(events, TRACE):
        yield SimpleNamespace(render=render, rec=rec, plan=plan, inputs=inputs, prefix=prefix,
                              record=render.InstallRecord(prefix, RELEASE), events=events,
                              doubles=doubles, engine=VolumeEngine(events))


def bootstrap(b, **change):
    args = dict(plan=b.plan, unlock_fd=S.UNLOCK, key_id=S.KEY_ID, operation_key=S.OPERATION,
                record=b.record, engine=b.engine, deadline=S.DEADLINE)
    args.update(change)
    return b.render.finite_bootstrap(change.pop("config_path", b.inputs["config"]),
                                     **{k: v for k, v in args.items() if k != "config_path"})


def imported(owner):
    for purpose in S.PURPOSES:
        owner.import_credential(purpose, S.password(purpose))


def objects(b):
    doc = b.rec.read_record(str(b.prefix))
    return [] if doc is None else doc["objects"]


def effects(b):
    return [e for e in b.events if e[0] in ("run", "create", "stop", "remove")]


def owner_runs(b):
    return [e for e in b.events if e[:3] == ("call", "run", "custody.py")]


def refused(fn):
    with pytest.raises(Refusal) as error:
        fn()
    return error.value.reason


def completed(b, owner_hook=None):
    with bootstrap(b) as owner:
        imported(owner)
        b.render.receive_bootstrap(owner, S.password("bootstrap"))
        if owner_hook:
            owner_hook(owner)
    return owner


# ---- pre-checks: typed refusals before custody opens, with no engine or record effect ----
def _complete(b): b.rec.record_object(str(b.prefix), RELEASE, "bootstrap", "cortex-v2-bootstrap", "earlier")
def _recorded(b): b.rec.record_object(str(b.prefix), RELEASE, "pod", "cortex-v2-bootstrap", "pod-earlier")
def _collision(b): b.engine.objects[("container", "cortex-v2-core-init")] = {"id": "theirs", "state": "running"}
def _data(b): b.engine.volumes.add("cortex-v2-core")


def _no_pwfile(b):
    plan = copy.deepcopy(b.plan)
    step = next(s for s in plan["steps"] if s["name"] == "core-init")
    step["argv"] = [a for a in step["argv"] if not a.startswith("--pwfile=")]
    return {"plan": plan}


def _no_temporary_db(b):
    plan = copy.deepcopy(b.plan)
    next(s for s in plan["steps"] if s["name"] == "temporary-db")["podman"] = []
    return {"plan": plan}


def _other_config(b):
    other = b.inputs["vault"] / "config" / "other.json"
    shutil.copyfile(b.inputs["config"], other)
    other.chmod(0o600)
    return {"config_path": other}


PRECHECKS = {
    "completed-before": (_complete, None, "bootstrap_complete"),
    "finite-object-recorded": (_recorded, None, "bootstrap_recorded"),
    "engine-collision": (_collision, None, "bootstrap_collision:cortex-v2-core-init"),
    "data-volume-present": (_data, None, "bootstrap_data_present"),
    "plan-config-mismatch": (None, _other_config, "plan_config_mismatch"),
    "foreign-release": (_complete, lambda b: {"record": b.render.InstallRecord(b.prefix, "other-release")},
                        "record_foreign_release"),
    "read-only-record": (None, lambda b: {"record": RecordDouble([], writable=False)}, "record_owner_unbound"),
    "no-operation-key": (None, lambda b: {"operation_key": ""}, "input_missing:operation_key"),
    "deadline-not-a-number": (None, lambda b: {"deadline": "200"}, "input_missing:deadline"),
    "deadline-expired": (None, lambda b: {"deadline": 100.0}, "deadline"),
    "core-init-without-pwfile": (None, _no_pwfile, "plan_invalid:core-init"),
    "no-temporary-db": (None, _no_temporary_db, "plan_invalid:temporary-db"),
}


@pytest.mark.parametrize("case", PRECHECKS, ids=list(PRECHECKS))
def test_pre_checks_refuse_typed_before_custody_engine_or_record_effect(boot, case):
    prepare, change, reason = PRECHECKS[case]
    if prepare:
        prepare(boot)
    before = objects(boot)
    assert refused(lambda: bootstrap(boot, **(change(boot) if change else {})).__enter__()) == reason
    assert "unlock-double" not in boot.doubles.adapters.events, "custody never opened"
    assert effects(boot) == [] and objects(boot) == before


# ---- the fixed receiver: bound owner only, after all five imports, once ----
def test_the_receiver_outside_a_finite_bootstrap_is_unbound_and_has_no_effect(boot):
    assert refused(lambda: boot.render.receive_bootstrap(object(), S.password("bootstrap"))) == \
        "bootstrap_receiver_unbound"
    assert effects(boot) == []


@pytest.mark.parametrize("case", ["before-all-imports", "foreign-owner", "twice", "not-bytes", "empty", "too-long"])
def test_the_receiver_admits_only_the_bound_owner_after_five_imports_once(boot, case):
    with pytest.raises(Refusal) as error:
        with bootstrap(boot) as owner:
            if case == "before-all-imports":
                owner.import_credential("bootstrap", S.password("bootstrap"))
                boot.render.receive_bootstrap(owner, S.password("bootstrap"))
            imported(owner)
            if case == "foreign-owner":
                boot.render.receive_bootstrap(SimpleNamespace(), S.password("bootstrap"))
            data = {"not-bytes": S.password("bootstrap").decode(), "empty": b"", "too-long": b"x" * 4097}
            boot.render.receive_bootstrap(owner, data.get(case, S.password("bootstrap")))
            if case == "twice":
                boot.render.receive_bootstrap(owner, S.password("bootstrap"))
    assert error.value.reason == {"before-all-imports": "bootstrap_owner", "not-bytes": "bootstrap_input",
                                  "empty": "bootstrap_input", "too-long": "bootstrap_input"}.get(
        case, "bootstrap_receiver_unbound")
    assert owner._tainted and owner_runs(boot) == [], "a refused receiver never reaches owner.run"
    # Whatever the prelude made is gone again; nothing is recorded or kept.
    assert boot.engine.objects == {} and boot.engine.volumes == set() and objects(boot) == []
    if case != "twice":
        assert effects(boot) == []


def test_a_swallowed_receiver_refusal_leaves_no_way_to_run_without_the_prelude(boot):
    with pytest.raises(Refusal) as error:
        with bootstrap(boot) as owner:
            imported(owner)
            with pytest.raises(Refusal):
                boot.render.receive_bootstrap(owner, b"")
            # The caller ignored the refusal: the owner is already tainted, so the actual run
            # refuses before any phase, connector or credential.
            assert refused(lambda: owner.run(S.OPERATION)) == "installer_context"
    assert error.value.reason in ("bootstrap_owner", "installer_context")
    assert effects(boot) == [] and objects(boot) == []
    assert boot.doubles.fixture.connections == []


def test_an_early_owner_run_inside_the_block_is_never_completed_by_s4(boot):
    with pytest.raises(Refusal):
        with bootstrap(boot) as owner:
            imported(owner)
            owner.run(S.OPERATION)  # no prelude: S4 cannot stop a public Core call, only refuse it
    assert owner._tainted and objects(boot) == []
    assert not any(e[3]["kind"] == "bootstrap" for e in boot.events if e[:3] == ("call", "record_object", "record.py"))


def test_a_block_that_never_delivers_the_bootstrap_bytes_runs_nothing(boot):
    with pytest.raises(Refusal) as error:
        with bootstrap(boot) as owner:
            imported(owner)
    assert error.value.reason == "bootstrap_not_delivered"
    assert owner._tainted and effects(boot) == [] and owner_runs(boot) == [] and objects(boot) == []


# ---- failure at each finite stage: taint, reverse cleanup, never a completion ----
def test_a_failed_core_init_creates_nothing_further(boot):
    boot.engine.run_exit = 1
    with pytest.raises(Refusal) as error:
        completed(boot)
    assert error.value.reason == "bootstrap_init_failed"
    assert [e[0] for e in effects(boot)] == ["run"] and owner_runs(boot) == [] and objects(boot) == []


def test_a_failed_temporary_db_create_removes_the_pod_and_the_new_data(boot):
    boot.engine.fail_on = "cortex-v2-bootstrap-core"
    with pytest.raises(Refusal) as error:
        completed(boot)
    assert error.value.reason == "bootstrap_start_failed"
    assert boot.engine.objects == {} and boot.engine.volumes == set() and objects(boot) == []
    assert owner_runs(boot) == []


def test_a_failed_owner_run_is_unwound_and_records_nothing(boot):
    boot.doubles.fixture.bad_flags = True  # the actual catalog policy refuses inside SEED
    with pytest.raises(Refusal):
        owner = completed(boot)
    assert len(owner_runs(boot)) == 1
    assert boot.engine.objects == {} and boot.engine.volumes == set() and objects(boot) == []
    assert not any(e[3]["kind"] == "bootstrap" for e in boot.events if e[:3] == ("call", "record_object", "record.py"))


def test_an_interrupt_after_delivery_still_unwinds_and_propagates(boot):
    with pytest.raises(KeyboardInterrupt):
        with bootstrap(boot) as owner:
            imported(owner)
            boot.render.receive_bootstrap(owner, S.password("bootstrap"))
            raise KeyboardInterrupt
    assert owner._tainted and owner_runs(boot) == []
    assert boot.engine.objects == {} and boot.engine.volumes == set() and objects(boot) == []


def test_the_original_deadline_bounds_the_run_and_its_cleanup(boot):
    def expire(stage, _connection, _sql):
        if stage == "PRE":
            boot.doubles.adapters.clock = S.DEADLINE
    boot.doubles.fixture.hook = expire
    with pytest.raises(Refusal) as error:
        completed(boot)
    # No renewed budget: cleanup cannot outlive the original deadline, so the finite objects
    # stay recorded by exact ID for a later explicit cleanup, and nothing is completed.
    assert error.value.reason == "bootstrap_cleanup_failed"
    assert {o["name"] for o in objects(boot)} == set(FINITE)


def test_a_failed_finite_cleanup_after_the_run_is_not_success(boot):
    boot.engine.fail_remove = "cortex-v2-bootstrap"
    with pytest.raises(Refusal) as error:
        completed(boot)
    assert error.value.reason == "bootstrap_cleanup_failed"
    assert [o["name"] for o in objects(boot)] == ["cortex-v2-bootstrap"], "the pod stays recorded"
    assert "cortex-v2-core" in boot.engine.volumes, "seeded data is never removed after the run"


def test_completion_waits_for_the_custody_exit(boot):
    boot.doubles.adapters.close_error = OSError("NONSECRET-close-fault")
    with pytest.raises(Refusal) as error:
        completed(boot)
    assert error.value.reason == "installer_cleanup"
    assert len(owner_runs(boot)) == 1 and objects(boot) == [] and boot.engine.objects == {}


# ---- the exact effects ----
def test_core_init_is_the_pinned_vector_network_less_with_its_pwfile_on_the_descriptor(boot):
    completed(boot)
    step = next(s for s in boot.plan["steps"] if s["name"] == "core-init")
    run = next(e for e in boot.events if e[0] == "run")
    assert list(run[2]) == ["podman", "run", "--rm", "--name", "cortex-v2-core-init",
                            "--label", "ai.kaidera.product=cortex-v2", "--read-only", "--cap-drop=ALL",
                            "--security-opt", "no-new-privileges", "--network=none",
                            "-v", "cortex-v2-core:/var/lib/cortex/core",
                            "-v", "/dev/fd/3:/run/cortex/bootstrap/initdb-password:ro",
                            step["image"], *step["argv"]]
    temporary = next(s for s in boot.plan["steps"] if s["name"] == "temporary-db")["podman"]
    assert [list(e[3]) for e in effects(boot) if e[0] == "create"] == temporary


def test_the_legacy_sql_migrate_and_seed_steps_are_never_executed(boot):
    completed(boot)
    legacy = {s["name"] for s in boot.plan["steps"]} & {"roles-pre", "migrate", "roles-post", "seed", "roles-close"}
    assert legacy, "the legacy plan still carries them (removal waits for the parity ruling)"
    names = {(e[2] if e[0] != "run" else e[1]) for e in effects(boot)}
    assert names == {"cortex-v2-core-init", *FINITE}
    assert not any(n.startswith("cortex-v2-bootstrap-") and n != FINITE[1] for n in names)


def test_s5_serve_start_accepts_exactly_the_s4_completion(boot):
    completed(boot)
    engine = EngineDouble([])
    with unlock_pipe() as unlock:
        started = boot.render.serve_lifecycle("start", plan=boot.plan, record=boot.record, engine=engine,
                                              key_id="kid-fixture", unlock_fd=unlock, deadline=S.DEADLINE)
    assert [o["name"] for o in started["objects"]] == ["cortex-v2", "cortex-v2-core", "cortex-v2-api"]


# ---- the engine command path (fake `podman`, records argv/input only) ----
FAKE_PODMAN = r'''
import json, os, sys
path = os.environ["FAKE_PODMAN_STATE"]
state = json.load(open(path)) if os.path.exists(path) else {"calls": [], "volumes": [], "input": None}
args = sys.argv[1:]
state["calls"].append(args)
code = 0
if args[:2] == ["volume", "exists"]:
    code = 0 if args[2] in state["volumes"] else 1
elif args[:2] == ["volume", "rm"]:
    state["volumes"].remove(args[2])
elif args[:2] == ["run", "--rm"]:
    state["input"] = os.read(3, 8192).decode()
    state["volumes"].append(args[args.index("-v") + 1].split(":")[0])
else:
    code = 125
json.dump(state, open(path, "w"))
sys.exit(code)
'''


def test_podman_engine_runs_core_init_with_the_bytes_as_fd3_and_handles_the_data_volume(boot, tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "podman"
    fake.parent.mkdir()
    fake.write_text(f"#!{sys.executable}\n" + FAKE_PODMAN)
    fake.chmod(0o755)
    state = tmp_path / "podman-state.json"
    monkeypatch.setenv("FAKE_PODMAN_STATE", str(state))
    engine = boot.render.PodmanEngine(program=str(fake))
    assert engine.lookup("volume", "cortex-v2-core") is None
    read_end, write_end = os.pipe()
    os.write(write_end, S.password("bootstrap"))
    os.close(write_end)
    try:
        argv = boot.render._core_init_argv(boot.plan)
        assert engine.run(argv, input_fd=read_end, deadline=S.DEADLINE) == 0
    finally:
        os.close(read_end)
    assert engine.lookup("volume", "cortex-v2-core") == "cortex-v2-core"
    engine.remove("volume", "cortex-v2-core", deadline=S.DEADLINE)
    doc = json.loads(state.read_text())
    assert doc["input"] == S.password("bootstrap").decode() and doc["volumes"] == []
    assert doc["calls"][1] == argv[1:]
    with pytest.raises(Refusal):
        engine.run(["podman", "pod", "create"], input_fd=None, deadline=S.DEADLINE)
