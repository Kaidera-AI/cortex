"""S5 deciding behaviour: the supported serve lifecycle owns exactly its recorded objects.

Drives start -> stop -> explicit restart -> scoped uninstall over the actual rendered serve
step with the named doubles in _lifecycle_doubles (no Podman, unlock, secret or network).
"""
import importlib.util
import os
import time

import pytest

from _lifecycle_doubles import BOOTSTRAP, EngineDouble, RecordDouble, unlock_pipe
from test_render import RENDER, inputs, rendered  # noqa: F401  (inputs is a fixture)
from cortex_core.runtime.signed_member import Refusal


def load_render():
    spec = importlib.util.spec_from_file_location("ship_run_render", RENDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_supported_serve_lifecycle_owns_exactly_its_recorded_objects(inputs):
    render = load_render()
    _serve, plan, _inventory = rendered(inputs)
    log = []
    baseline = {("container", "unrelated-app"): {"id": "baseline-0001", "state": "running"}}
    record = RecordDouble(log, objects=[BOOTSTRAP])
    engine = EngineDouble(log, baseline=baseline)
    deadline = time.monotonic() + 30

    with unlock_pipe() as unlock:
        started = render.serve_lifecycle("start", plan=plan, record=record, engine=engine,
                                         key_id="kid-fixture", unlock_fd=unlock, deadline=deadline)
    # Each rendered create is recorded with its exact returned ID before the next effect.
    assert [entry[:3] for entry in log] == [
        ("create", "pod", "cortex-v2"), ("record", "pod", "cortex-v2"),
        ("create", "container", "cortex-v2-core"), ("record", "container", "cortex-v2-core"),
        ("create", "container", "cortex-v2-api"), ("record", "container", "cortex-v2-api")]
    creates = [entry for entry in log if entry[0] == "create"]
    assert [entry[4] for entry in creates] == [False, False, True], "only the API gets the unlock"
    api = list(creates[2][3])
    assert api[api.index("--key-id") + 1] == "kid-fixture"
    assert not any(isinstance(arg, dict) for entry in creates for arg in entry[3])
    first = {o["name"]: o["id"] for o in record.objects() if o["kind"] != "bootstrap"}
    assert first == {"cortex-v2": "pod-0001", "cortex-v2-core": "container-0002",
                     "cortex-v2-api": "container-0003"}
    assert started["objects"] == [o for o in record.objects() if o["kind"] != "bootstrap"]

    log.clear()
    render.serve_lifecycle("stop", plan=plan, record=record, engine=engine, deadline=deadline)
    assert log == [("stop", "pod", "cortex-v2", "pod-0001")]
    assert first == {o["name"]: o["id"] for o in record.objects() if o["kind"] != "bootstrap"}

    # Restart needs a NEW caller descriptor; a consumed one is refused before any effect.
    log.clear()
    with unlock_pipe() as stale:
        os.read(stale, 64)
        with pytest.raises(Refusal) as refused:
            render.serve_lifecycle("restart", plan=plan, record=record, engine=engine,
                                   key_id="kid-fixture", unlock_fd=stale, deadline=deadline)
    assert refused.value.reason == "unlock_stale" and log == []
    with unlock_pipe() as fresh:
        render.serve_lifecycle("restart", plan=plan, record=record, engine=engine,
                               key_id="kid-fixture", unlock_fd=fresh, deadline=deadline)
    second = {o["name"]: o["id"] for o in record.objects() if o["kind"] != "bootstrap"}
    assert set(second) == set(first) and not set(second.values()) & set(first.values())
    assert {("remove",) + key for key in (("pod", "cortex-v2", "pod-0001"),
                                          ("container", "cortex-v2-core", "container-0002"),
                                          ("container", "cortex-v2-api", "container-0003"))} \
        <= {entry for entry in log if entry[0] == "remove"}

    # Uninstall removes exactly the recorded serve objects; the unrelated baseline and the
    # finite bootstrap record stay, and each forget follows its own successful removal.
    log.clear()
    render.serve_lifecycle("uninstall", plan=plan, record=record, engine=engine, deadline=deadline)
    assert set(engine.objects) == set(baseline)
    assert record.objects() == [BOOTSTRAP]
    removed = [entry for entry in log if entry[0] in ("remove", "forget")]
    assert [entry[0] for entry in removed] == ["remove", "forget"] * 3
    assert all(r[2] == f[1] for r, f in zip(removed[::2], removed[1::2]))
