"""Spec for ship/run/render.py: DB + API only serve pod and the finite bootstrap plan."""
import copy
import json
import pathlib
import re
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
RENDER = HERE.parent / "render.py"
REPO_NEXT = HERE.parents[3]
OPERATIONS = REPO_NEXT / "contracts" / "runtime" / "runtime-operations-v1.json"
sys.path.insert(0, str(REPO_NEXT / "src"))
from cortex_core.runtime.bootstrap_config import MODE_PURPOSES, decode_config  # noqa: E402
from cortex_core.runtime.signed_member import Refusal  # noqa: E402

SOURCE = "7a1078213ac2497d6219e7778c7866001f97f30b"
CORE_IMAGE = "sha256:" + "c" * 64
API_IMAGE = "sha256:" + "a" * 64
PY = "/opt/cortex/python/bin/python3"
CFG = "/run/cortex/config/cortex.json"
PURPOSES = ("api_request", "probe", "bootstrap", "migrator", "installer_enrollment")
# Refs deliberately differ from purpose names: targets must come from the Core refs.
REFS = {"bootstrap": "secrets/cortex/pg_boot", "migrator": "secrets/cortex/pg_mig",
        "api_request": "secrets/cortex/pg_req", "probe": "secrets/cortex/pg_probe",
        "installer_enrollment": "secrets/cortex/pg_enrol"}
CA_REF = "secrets/cortex/db_ca"
STEPS = ["core-init", "temporary-db", "roles-pre", "migrate", "roles-post",
         "seed", "roles-close", "stop-temporary-db", "serve"]
FORBIDDEN = re.compile(r"embed|tei|qdrant|rerank|supervisor|conductor|observer|worker", re.I)


def base_config(vault="/vault/helix"):
    return {
        "version": 1,
        "installation_id": "11111111-1111-4111-8111-111111111111",
        "tenant_id": "22222222-2222-4222-8222-222222222222",
        "project_id": "33333333-3333-4333-8333-333333333333",
        "project_names": {"helix": "33333333-3333-4333-8333-333333333333"},
        "ai_mode": "off",
        "vault_root": vault,
        "bind": {"host": "0.0.0.0", "port": 8000},
        "publish": {"host": "127.0.0.1", "port": 18601},
        "client": {"credential_ref": "secrets/cortex/client_bob"},
        "database": {"host": "127.0.0.1", "port": 5432, "name": "cortex",
                     "tls": {"mode": "verify-full", "root_cert_ref": CA_REF}},
        "roles": {p: {"login": "cortex_" + p, "secret_ref": REFS[p]} for p in PURPOSES},
        "limits": {"request_seconds": 10, "shutdown_seconds": 15},
    }


def base_release():
    return {
        "source_commit": SOURCE,
        "core": {"image": CORE_IMAGE, "initdb": "/usr/lib/postgresql/bin/initdb",
                 "postgres": "/usr/lib/postgresql/bin/postgres",
                 "psql": "/usr/lib/postgresql/bin/psql"},
        "api": {"image": API_IMAGE},
    }


@pytest.fixture
def inputs(tmp_path):
    conf = tmp_path / "core-conf"
    (conf / "sql").mkdir(parents=True)
    for name in ("postgresql.conf", "pg_hba.conf"):
        (conf / name).write_text("# fixture\n")
    for step in ("pre", "post", "close"):
        (conf / "sql" / f"roles-{step}.sql").write_text("-- fixture\n")
    vault = tmp_path.resolve() / "vault"
    (vault / "config").mkdir(parents=True)
    paths = {"config": vault / "config" / "cortex.json", "release": tmp_path / "release.json",
             "operations": tmp_path / "ops.json", "core_conf": conf, "out": tmp_path / "out",
             "vault": vault, "root": tmp_path.resolve()}
    write_config(paths, base_config(str(vault)))
    paths["release"].write_text(json.dumps(base_release()))
    paths["operations"].write_bytes(OPERATIONS.read_bytes())
    return paths


def write_config(paths, value, raw=None):
    paths["config"].write_bytes(raw if raw is not None else json.dumps(value).encode())
    paths["config"].chmod(0o600)


def run(paths, drop=None):
    assert RENDER.is_file(), f"missing {RENDER}"
    flags = {"--config": paths["config"], "--operations": paths["operations"],
             "--release": paths["release"], "--core-conf": paths["core_conf"], "--out": paths["out"]}
    argv = [sys.executable, "-I", str(RENDER)]
    for flag, value in flags.items():
        if flag != drop:
            argv += [flag, str(value)]
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


def rendered(paths):
    proc = run(paths)
    assert proc.returncode == 0, proc.stderr
    out = paths["out"]
    return (json.loads((out / "cortex-v2.pod.yaml").read_text()),
            json.loads((out / "bootstrap.plan.json").read_text()),
            json.loads((out / "inventory.json").read_text()))


def refusal(proc):
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    lines = proc.stderr.strip().splitlines()
    assert len(lines) == 1, proc.stderr
    body = json.loads(lines[0])
    assert body["unit"] == "ship/run"
    return body["reason"]


def resolved_vector(mode):
    ops = json.loads(OPERATIONS.read_text())
    release = base_release()
    fields = {"target.core.initdb.value.path": release["core"]["initdb"],
              "target.core.postgres.value.path": release["core"]["postgres"],
              "config.roles.bootstrap.binding.value.sql_name": "cortex_bootstrap"}
    return [fields[a["field"]] if isinstance(a, dict) else a
            for a in ops["operation_vectors"][mode]["argv"]]


def test_rung_feature_db_and_api_only(inputs):
    pod, plan, inventory = rendered(inputs)
    containers = pod["spec"]["containers"]
    assert [c["name"] for c in containers] == ["core", "api"]
    assert {c["image"] for c in containers} == {CORE_IMAGE, API_IMAGE}
    assert pod["spec"].get("initContainers", []) == []
    assert inventory["serve"]["containers"] == ["core", "api"]
    images = {step["image"] for step in plan["steps"] if "image" in step}
    assert images <= {CORE_IMAGE, API_IMAGE}
    # Host paths are installer inputs, not run inventory: mask the fixture root before scanning.
    fixture_root = str(inputs["root"])
    for name in ("cortex-v2.pod.yaml", "bootstrap.plan.json", "inventory.json"):
        text = (inputs["out"] / name).read_text().replace(fixture_root, "<fixture>")
        assert fixture_root not in text
        assert FORBIDDEN.search(text) is None, (name, FORBIDDEN.search(text))


def test_ready_identity_on_api_listener(inputs):
    pod, _plan, inventory = rendered(inputs)
    assert inventory["ready"] == {"url": "http://127.0.0.1:18601/health",
                                  "fields": ["component", "status", "readiness"],
                                  "source_commit": SOURCE}
    labels = pod["metadata"]["labels"]
    assert labels["ai.kaidera.product"] == "cortex-v2"
    assert labels["ai.kaidera.cortex.source"] == SOURCE
    assert labels["ai.kaidera.cortex.installation"] == base_config()["installation_id"]
    api = pod["spec"]["containers"][1]
    assert api["command"] == [PY, "-I", "-B", "-m", "cortex_core.cli", "serve", "--config", CFG]
    assert api["ports"] == [{"containerPort": 8000, "hostIP": "127.0.0.1", "hostPort": 18601}]
    assert inventory["status_argv"] == [PY, "-I", "-B", "-m", "cortex_core.cli", "status",
                                        "--config", CFG, "--json"]
    for name in ("cortex-v2.pod.yaml", "bootstrap.plan.json", "inventory.json"):
        assert "9309" not in (inputs["out"] / name).read_text()


def test_db_argv_comes_from_operations(inputs):
    pod, plan, _inventory = rendered(inputs)
    assert pod["spec"]["containers"][0]["command"] == resolved_vector("core-server")
    steps = {s["name"]: s for s in plan["steps"]}
    assert steps["core-init"]["argv"] == resolved_vector("core-init")
    assert steps["core-init"].get("when") == "absent:/var/lib/cortex/core/PG_VERSION"
    assert steps["temporary-db"]["argv"] == resolved_vector("core-server")


def target(inputs, ref):
    return str(inputs["vault"] / ref)


def test_serve_mounts_only_request_and_probe(inputs):
    pod, _plan, inventory = rendered(inputs)
    core, api = pod["spec"]["containers"]
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    assert not [m for m in core["volumeMounts"] if "secret" in volumes[m["name"]]]
    projected = {m["mountPath"]: (volumes[m["name"]]["secret"]["secretName"], m.get("subPath"),
                                  m.get("readOnly"))
                 for m in api["volumeMounts"] if "secret" in volumes[m["name"]]}
    assert projected == {
        target(inputs, REFS["api_request"]): ("cortex-v2-pg_req", "pg_req", True),
        target(inputs, REFS["probe"]): ("cortex-v2-pg_probe", "pg_probe", True),
        target(inputs, CA_REF): ("cortex-v2-db_ca", "db_ca", True)}
    vault = inputs["vault"]
    mounts = [m["mountPath"] for c in (core, api) for m in c["volumeMounts"]]
    assert not {str(vault), str(vault / "secrets"), str(vault / "secrets" / "cortex")} & set(mounts)
    host_paths = sorted((v["hostPath"]["path"], v["hostPath"]["type"])
                        for v in volumes.values() if "hostPath" in v)
    conf = inputs["core_conf"]
    assert host_paths == sorted([(str(inputs["config"]), "File"),
                                 (str(conf / "postgresql.conf"), "File"),
                                 (str(conf / "pg_hba.conf"), "File")])
    assert inventory["serve"]["secrets"] == sorted(MODE_PURPOSES["serve"]) == ["api_request", "probe"]


def test_only_loopback_publish(inputs):
    pod, _plan, _inventory = rendered(inputs)
    ports = [p for c in pod["spec"]["containers"] for p in c.get("ports", [])]
    assert len(ports) == 1
    assert ports[0]["hostIP"] == "127.0.0.1"
    assert all(p["containerPort"] != 5432 for p in ports)
    assert pod["spec"]["hostNetwork"] is False


def test_bootstrap_is_finite_and_ordered(inputs):
    _pod, plan, _inventory = rendered(inputs)
    assert [s["name"] for s in plan["steps"]] == STEPS
    expected = {"core-init": ["bootstrap"], "temporary-db": [],
                "roles-pre": ["api_request", "bootstrap", "installer_enrollment", "migrator", "probe"],
                "migrate": ["migrator"], "roles-post": ["bootstrap"],
                "seed": ["api_request", "bootstrap", "installer_enrollment"], "roles-close": ["bootstrap"],
                "stop-temporary-db": [], "serve": []}
    assert expected["migrate"] == sorted(MODE_PURPOSES["migrate"])
    assert expected["seed"] == sorted(MODE_PURPOSES["seed"])
    for step in plan["steps"]:
        assert step["secrets"] == expected[step["name"]], step["name"]
        assert isinstance(step["deadline_ms"], int) and step["deadline_ms"] > 0
        assert step.get("retries", 0) == 0
    steps = {s["name"]: s for s in plan["steps"]}
    assert steps["migrate"]["argv"] == [PY, "-I", "-B", "-m", "cortex_core.cli", "migrate", "--config", CFG]
    assert steps["seed"]["argv"] == [PY, "-I", "-B", "-m", "cortex_core.cli", "seed", "--config", CFG]
    assert steps["roles-pre"]["argv"][-2:] == ["-f", "/run/cortex/sql/roles-pre.sql"]
    assert steps["serve"]["pod_file"] == "cortex-v2.pod.yaml"
    assert plan["restart"] == "never"


def secret_flags(step):
    return sorted(arg for argv in step["podman"] for arg in argv if arg.startswith("--secret="))


def test_bootstrap_projections_follow_core_refs(inputs):
    _pod, plan, _inventory = rendered(inputs)
    steps = {s["name"]: s for s in plan["steps"]}
    ca = f"--secret=cortex-v2-db_ca,type=mount,target={target(inputs, CA_REF)},mode=0400"

    def flags(*purposes):
        return sorted([f"--secret=cortex-v2-{REFS[p].rsplit('/', 1)[1]},type=mount,"
                       f"target={target(inputs, REFS[p])},mode=0400" for p in purposes] + [ca])
    assert secret_flags(steps["migrate"]) == flags("migrator")
    assert secret_flags(steps["seed"]) == flags("bootstrap", "installer_enrollment", "api_request")
    assert secret_flags(steps["core-init"]) == [
        "--secret=cortex-v2-pg_boot,type=mount,target=/run/cortex/bootstrap/initdb-password,mode=0400"]
    for name in ("migrate", "seed"):
        assert f"{inputs['config']}:/run/cortex/config/cortex.json:ro" in steps[name]["podman"][0]
    for name in ("cortex-v2.pod.yaml", "bootstrap.plan.json", "inventory.json"):
        assert "/run/cortex/secrets" not in (inputs["out"] / name).read_text()


def test_operations_mismatch_refuses(inputs):
    data = bytearray(inputs["operations"].read_bytes())
    data[-2] = ord(" ") if data[-2] != ord(" ") else ord("\n")
    inputs["operations"].write_bytes(bytes(data))
    proc = run(inputs)
    assert refusal(proc) == "operations_mismatch"
    assert not inputs["out"].exists()


@pytest.mark.parametrize("flag,reason", [
    ("--config", "input_missing:--config"), ("--operations", "input_missing:--operations"),
    ("--release", "input_missing:--release"), ("--core-conf", "input_missing:--core-conf"),
    ("--out", "input_missing:--out")])
def test_missing_flag_is_typed_gap(inputs, flag, reason):
    proc = run(inputs, drop=flag)
    assert refusal(proc) == reason
    assert not inputs["out"].exists()


def mutate_config(inputs, fn):
    cfg = base_config(str(inputs["vault"]))
    fn(cfg)
    write_config(inputs, cfg)


@pytest.mark.parametrize("change,reason", [
    (lambda c: c.pop("ai_mode"), "config_shape"),
    (lambda c: c.__setitem__("ai_mode", "hosted"), "unshipped_provider_refusal"),
    (lambda c: c["database"]["tls"].__setitem__("mode", "disable"), "config_transport_unratified"),
    (lambda c: c["database"]["tls"].pop("root_cert_ref"), "config_shape"),
    (lambda c: c["publish"].__setitem__("host", "0.0.0.0"), "config_binding"),
    (lambda c: c["roles"].pop("probe"), "config_shape"),
    (lambda c: c.__setitem__("purposes", c.pop("roles")), "config_shape"),
    (lambda c: c.pop("installation_id"), "config_shape"),
    (lambda c: c["roles"].__setitem__("observer", {"login": "o", "secret_ref": "secrets/cortex/o"}),
     "config_shape"),
])
def test_config_refusals(inputs, change, reason):
    mutate_config(inputs, change)
    assert refusal(run(inputs)) == reason
    assert not inputs["out"].exists()


@pytest.mark.parametrize("raw", [b'{"version": 1, "version": 1}', b"[]", b"\xff", b"{"])
def test_config_refusal_is_cores_own(inputs, raw):
    write_config(inputs, None, raw=raw)
    with pytest.raises(Refusal) as core:
        decode_config(raw, mode="serve")
    assert refusal(run(inputs)) == core.value.reason
    assert not inputs["out"].exists()


def test_config_file_must_be_private(inputs):
    inputs["config"].chmod(0o644)
    assert refusal(run(inputs)) == "private_file_required"
    assert not inputs["out"].exists()


@pytest.mark.parametrize("path,reason", [
    (("core", "image"), "input_missing:release.core.image"),
    (("api", "image"), "input_missing:release.api.image"),
    (("source_commit",), "input_missing:release.source_commit"),
])
def test_release_refusals(inputs, path, reason):
    release = copy.deepcopy(base_release())
    node = release
    for key in path[:-1]:
        node = node[key]
    node.pop(path[-1])
    inputs["release"].write_text(json.dumps(release))
    assert refusal(run(inputs)) == reason
    assert not inputs["out"].exists()


def test_core_conf_file_missing_is_typed_gap(inputs):
    (inputs["core_conf"] / "sql" / "roles-close.sql").unlink()
    assert refusal(run(inputs)) == "input_missing:core-conf/sql/roles-close.sql"
    assert not inputs["out"].exists()


def test_out_not_empty_refuses(inputs):
    inputs["out"].mkdir()
    (inputs["out"] / "keep").write_text("x")
    assert refusal(run(inputs)) == "out_not_empty"
    assert sorted(p.name for p in inputs["out"].iterdir()) == ["keep"]
