"""S5 record join deciding behaviour: the serve lifecycle owns its objects through the production
InstallRecord port over the ONE actual installer record module (install/record.py, Nix).

Real record.py IO in an owned NONSECRET temporary prefix only. The engine is the named
EngineDouble and the unlock the synthetic anonymous pipe: no Podman, key, secret or install.
"""
import importlib.util
import time

from _lifecycle_doubles import EngineDouble, unlock_pipe
from test_lifecycle_deciding import load_render
from test_render import RENDER, inputs, rendered  # noqa: F401  (inputs is a fixture)

RECORD = RENDER.parents[1] / "install" / "record.py"
RELEASE = "rel-s5-fixture"
SERVE = ("cortex-v2", "cortex-v2-core", "cortex-v2-api")


def load_record():
    spec = importlib.util.spec_from_file_location("ship_install_record", RECORD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def serve_ids(rec, prefix):
    return {o["name"]: o["id"] for o in rec.read_record(str(prefix))["objects"] if o["name"] in SERVE}


def test_the_lifecycle_owns_its_objects_through_the_one_actual_install_record(inputs, tmp_path):
    render, rec = load_render(), load_record()
    _serve, plan, _inventory = rendered(inputs)
    prefix = tmp_path / "prefix"
    prefix.mkdir(mode=0o700)
    # What install.sh (an image) and the finite owner S4 (its completion object) already recorded.
    rec.record_object(str(prefix), RELEASE, "image", "localhost/cortex-v2-core:" + RELEASE, "sha256:" + "c" * 64)
    rec.record_object(str(prefix), RELEASE, "bootstrap", "cortex-v2-bootstrap", "s4-receipt-fixture")
    installed = (prefix / "install-record.json").read_bytes()
    seen = []

    class Engine(EngineDouble):
        def create(self, argv, **kwargs):
            # What the actual record holds at the moment of each engine create.
            seen.append(serve_ids(rec, prefix))
            return super().create(argv, **kwargs)

    baseline = {("container", "unrelated-app"): {"id": "baseline-0001", "state": "running"}}
    engine = Engine([], baseline=baseline)
    record = render.InstallRecord(prefix, RELEASE)
    deadline = time.monotonic() + 30

    with unlock_pipe() as unlock:
        started = render.serve_lifecycle("start", plan=plan, record=record, engine=engine,
                                         key_id="kid-fixture", unlock_fd=unlock, deadline=deadline)
    # Each create's exact returned ID is in the actual record before the next engine effect.
    assert seen == [{}, {"cortex-v2": "pod-0001"},
                    {"cortex-v2": "pod-0001", "cortex-v2-core": "container-0002"}]
    doc = rec.read_record(str(prefix))
    assert doc["release_id"] == RELEASE
    first = serve_ids(rec, prefix)
    assert first == {"cortex-v2": "pod-0001", "cortex-v2-core": "container-0002",
                     "cortex-v2-api": "container-0003"}
    assert {o["name"]: o["id"] for o in started["objects"]} == first

    render.serve_lifecycle("stop", plan=plan, record=record, engine=engine, deadline=deadline)
    assert rec.read_record(str(prefix)) == doc, "stop changes no ownership"
    with unlock_pipe() as fresh:
        render.serve_lifecycle("restart", plan=plan, record=record, engine=engine,
                               key_id="kid-fixture", unlock_fd=fresh, deadline=deadline)
    second = serve_ids(rec, prefix)
    assert set(second) == set(SERVE) and not set(second.values()) & set(first.values())

    # Uninstall removes exactly the recorded serve objects: the engine is back to its baseline
    # and the actual record is byte-identical to what the installer and S4 had recorded.
    render.serve_lifecycle("uninstall", plan=plan, record=record, engine=engine, deadline=deadline)
    assert set(engine.objects) == set(baseline)
    assert (prefix / "install-record.json").read_bytes() == installed
