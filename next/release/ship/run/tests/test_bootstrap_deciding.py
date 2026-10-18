"""S4 deciding behaviour: ONE bounded finite bootstrap through the ACTUAL Core owner (order A).

Config written first; finite_bootstrap opens the actual installer custody and yields the actual
owner; the five actual imports run (this test stands in for S1 make_secret, RED at this base);
the SAME bootstrap bytes reach the fixed receiver; the prelude runs on the engine double; the
actual owner.run executes Nix's fixed provision_sequence once; the finite objects are reverse
cleaned; custody exits; only then is the completion object in the actual install record.
Doubles: _bootstrap_stage (named, NONSECRET, below IO). Not proven: PostgreSQL, TLS, crypto,
Podman, native placement or the S4 prelude prerequisites U1-U4 of the published signature.
"""
import hashlib

import _bootstrap_stage as S
from cortex_core.release.codec import canonical_bytes
from cortex_core.runtime.bootstrap_config import decode_config
from test_lifecycle_deciding import load_render
from test_lifecycle_record_deciding import RELEASE, load_record
from test_render import inputs, rendered, write_config  # noqa: F401  (inputs is a fixture)

FINITE = ("cortex-v2-bootstrap", "cortex-v2-bootstrap-core")
TRACE = {("run", "custody.py"), ("close", "custody.py"), ("_reload_seed_receipt", "operator.py"),
         ("record_object", "record.py")}


def test_one_finite_bootstrap_runs_the_actual_owner_once_and_records_completion_last(inputs, tmp_path):
    render, rec = load_render(), load_record()
    write_config(inputs, S.install_config(str(inputs["vault"])))
    _serve, plan, _inventory = rendered(inputs)
    config = decode_config(inputs["config"].read_bytes(), mode="install")
    prefix = tmp_path / "prefix"
    prefix.mkdir(mode=0o700)
    record = render.InstallRecord(prefix, RELEASE)
    events = []

    with S.composed(render, config) as doubles, S.traced(events, TRACE):
        engine = S.BootstrapEngine(events)
        with render.finite_bootstrap(inputs["config"], plan=plan, unlock_fd=S.UNLOCK, key_id=S.KEY_ID,
                                     operation_key=S.OPERATION, record=record, engine=engine,
                                     deadline=S.DEADLINE) as owner:
            assert type(owner) is S.custody.InstallerCustody
            for purpose in S.PURPOSES:
                owner.import_credential(purpose, S.password(purpose))
            assert events == [], "no effect before the bootstrap bytes are received"
            render.receive_bootstrap(owner, S.password("bootstrap"))
            assert rec.read_record(str(prefix))["objects"][0]["kind"] != "bootstrap"

    # The SAME imported bootstrap bytes reached core-init once, on the inherited descriptor only.
    assert engine.inputs == [("cortex-v2-core-init", S.password("bootstrap"))]
    executed = [list(e[2] if e[0] == "run" else e[3]) for e in events if e[0] in ("run", "create")]
    core_init = next(s for s in plan["steps"] if s["name"] == "core-init")
    assert executed[0][-len(core_init["argv"]):] == core_init["argv"], "pinned vector bytes unchanged"
    assert not any(arg.startswith("--secret") or "PGPASSWORD" in arg for argv in executed for arg in argv)
    assert not any(S.password("bootstrap").decode() in arg for argv in executed for arg in argv)

    # Fixed order: prelude, the actual owner run once, reverse cleanup, custody exit, completion.
    def at(match):
        found = [i for i, e in enumerate(events) if match(e)]
        assert len(found) == 1, (found, match)
        return found[0]
    run_init = at(lambda e: e[0] == "run")
    pod, db = (at(lambda e, n=n: e[0] == "create" and e[2] == n) for n in FINITE)
    owner_run = at(lambda e: e[:3] == ("call", "run", "custody.py"))
    ack = at(lambda e: e[:3] == ("return", "_reload_seed_receipt", "operator.py"))
    gone_db, gone_pod = (at(lambda e, n=n: e[0] == "remove" and e[2] == n) for n in reversed(FINITE))
    closed = at(lambda e: e[:3] == ("call", "close", "custody.py"))
    done = at(lambda e: e[:3] == ("call", "record_object", "record.py") and e[3]["kind"] == "bootstrap")
    assert run_init < pod < db < owner_run < ack < gone_db < gone_pod < closed < done
    assert doubles.fixture.applied_sql == [sql for _, _, sql in doubles.fixture.prepared]

    # Only the completion object remains, identified by the digest of the actual private ACK.
    receipt = events[ack][3]
    assert receipt["operation_key"] == S.OPERATION
    doc = rec.read_record(str(prefix))
    assert doc == {"release_id": RELEASE, "objects": [
        {"kind": "bootstrap", "name": "cortex-v2-bootstrap",
         "id": hashlib.sha256(canonical_bytes(receipt)).hexdigest()}]}
    assert engine.objects == {} and owner._stage == "EXIT" and not owner._tainted
