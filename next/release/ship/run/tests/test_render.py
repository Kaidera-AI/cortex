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
from cortex_core.runtime.bootstrap_config import (MODE_PURPOSES, decode_config,  # noqa: E402
                                                  read_config_file)
from cortex_core.runtime.signed_member import Refusal  # noqa: E402

SOURCE = "7a1078213ac2497d6219e7778c7866001f97f30b"
CORE_IMAGE = "sha256:" + "c" * 64
API_IMAGE = "sha256:" + "a" * 64
PY = "/opt/cortex/python/bin/python3"
CFG = "/run/cortex/config/cortex.json"
OPERATOR = "cortex_core.runtime.operator"
OUTPUTS = ("bootstrap.plan.json", "inventory.json")
PURPOSES = ("api_request", "probe", "bootstrap", "migrator", "installer_enrollment")
# Refs deliberately differ from purpose names: targets must come from the Core refs.
REFS = {"bootstrap": "secrets/cortex/pg_boot", "migrator": "secrets/cortex/pg_mig",
        "api_request": "secrets/cortex/pg_req", "probe": "secrets/cortex/pg_probe",
        "installer_enrollment": "secrets/cortex/pg_enrol"}
CA_REF = "secrets/cortex/db_ca"
STEPS = ["core-init", "temporary-db", "roles-pre", "migrate", "roles-post",
         "seed", "roles-close", "stop-temporary-db", "serve"]
FORBIDDEN = re.compile(r"embed|tei|qdrant|rerank|supervisor|conductor|observer|worker", re.I)


class ChildError(Exception):
    """The renderer crashed (traceback or undeclared exit): never a behavioral assertion."""


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


def flags(paths, drop=None, config=None):
    values = {"--config": paths["config"] if config is None else config,
              "--operations": paths["operations"], "--release": paths["release"],
              "--core-conf": paths["core_conf"], "--out": paths["out"]}
    return [arg for flag, value in values.items() if flag != drop for arg in (flag, str(value))]


def run(paths, drop=None, config=None, cwd=None):
    assert RENDER.is_file(), f"missing {RENDER}"
    argv = [sys.executable, "-I", str(RENDER), *flags(paths, drop, config)]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, cwd=cwd)
    # render declares exit 0 (written) and 2 (one typed refusal); anything else is a crash.
    if proc.returncode not in (0, 2) or "Traceback (most recent call last)" in proc.stderr:
        raise ChildError(proc.returncode, proc.stderr[-2000:])
    return proc


def rendered(paths):
    proc = run(paths)
    assert proc.returncode == 0, proc.stderr
    out = paths["out"]
    plan = json.loads((out / "bootstrap.plan.json").read_text())
    serve = next(s for s in plan["steps"] if s["name"] == "serve")
    # Serve is three explicit Podman invocations: pod create, then the DB and API containers.
    assert len(serve["podman"]) == 3, serve["podman"]
    return serve, plan, json.loads((out / "inventory.json").read_text())


def operator(verb, *extra):
    return [PY, "-I", "-B", "-m", OPERATOR, verb, "--config", CFG,
            "--unlock-fd", "3", "--key-id", {"input": "key_id"}, *extra]


def named(argv):
    return argv[argv.index("--name") + 1]


def option(argv, flag):
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == flag]


def command(argv, image):
    assert image in argv, argv
    return argv[argv.index(image) + 1:]


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
    serve, plan, inventory = rendered(inputs)
    pod_create, core, api = serve["podman"]
    assert pod_create[:3] == ["podman", "pod", "create"]
    assert [named(core), named(api)] == ["cortex-v2-core", "cortex-v2-api"]
    assert {core[core.index(CORE_IMAGE)], api[api.index(API_IMAGE)]} == {CORE_IMAGE, API_IMAGE}
    assert inventory["serve"]["containers"] == ["core", "api"]
    images = {step["image"] for step in plan["steps"] if "image" in step}
    assert images <= {CORE_IMAGE, API_IMAGE}
    # Host paths are installer inputs, not run inventory: mask the fixture root before scanning.
    fixture_root = str(inputs["root"])
    for name in OUTPUTS:
        text = (inputs["out"] / name).read_text().replace(fixture_root, "<fixture>")
        assert fixture_root not in text
        assert FORBIDDEN.search(text) is None, (name, FORBIDDEN.search(text))
    assert sorted(p.name for p in inputs["out"].iterdir()) == sorted(OUTPUTS)


def test_ready_identity_on_api_listener(inputs):
    serve, _plan, inventory = rendered(inputs)
    assert inventory["ready"] == {"url": "http://127.0.0.1:18601/health",
                                  "fields": ["component", "status", "readiness"],
                                  "source_commit": SOURCE}
    pod_create, _core, api = serve["podman"]
    labels = {pod_create[i + 1] for i, arg in enumerate(pod_create) if arg == "--label"}
    assert {"ai.kaidera.product=cortex-v2", f"ai.kaidera.cortex.source={SOURCE}",
            "ai.kaidera.cortex.installation=" + base_config()["installation_id"]} <= labels
    assert command(api, API_IMAGE) == operator("serve") == serve["argv"]
    assert inventory["status_argv"] == [PY, "-I", "-B", "-m", OPERATOR, "status", "--config", CFG]
    for name in OUTPUTS:
        text = (inputs["out"] / name).read_text()
        assert "9309" not in text and "cortex_core.cli" not in text


def test_db_argv_comes_from_operations(inputs):
    serve, plan, _inventory = rendered(inputs)
    assert command(serve["podman"][1], CORE_IMAGE) == resolved_vector("core-server")
    steps = {s["name"]: s for s in plan["steps"]}
    assert steps["core-init"]["argv"] == resolved_vector("core-init")
    assert steps["core-init"].get("when") == "absent:/var/lib/cortex/core/PG_VERSION"
    assert steps["temporary-db"]["argv"] == resolved_vector("core-server")


def target(inputs, ref):
    return str(inputs["vault"] / ref)


def private_parents(inputs, *, records=True):
    dirs = [inputs["vault"], inputs["vault"] / "secrets"]
    if records:
        dirs.append(inputs["vault"] / "secrets" / "cortex")
    return [f"{d}:rw,mode=0700,size=64k" for d in dirs]


def test_serve_mounts_only_request_and_probe(inputs):
    serve, _plan, inventory = rendered(inputs)
    _pod, core, api = serve["podman"]
    assert option(core, "-v") == [f"cortex-v2-core:/var/lib/cortex/core",
                                  f"{inputs['core_conf'] / 'postgresql.conf'}:/run/cortex/postgresql.conf:ro",
                                  f"{inputs['core_conf'] / 'pg_hba.conf'}:/run/cortex/pg_hba.conf:ro"]
    # The actual encrypted adapter reads the two role records and the CA in place; no plaintext copy.
    assert sorted(option(api, "-v")) == sorted([
        f"{inputs['config']}:{CFG}:ro",
        f"{target(inputs, REFS['api_request'])}:{target(inputs, REFS['api_request'])}:ro",
        f"{target(inputs, REFS['probe'])}:{target(inputs, REFS['probe'])}:ro",
        f"{target(inputs, CA_REF)}:{target(inputs, CA_REF)}:ro"])
    assert option(api, "--tmpfs") == ["/tmp"] + private_parents(inputs)
    assert not [a for step in serve["podman"] for a in step if a.startswith("--secret")]
    assert serve["secrets"] == inventory["serve"]["secrets"] == sorted(MODE_PURPOSES["serve"])
    assert inventory["serve"]["secrets"] == ["api_request", "probe"]


@pytest.mark.parametrize("request_name,probe_name", [
    ("Case_A", "case-a"),
    ("x" * 95 + "a", "x" * 95 + "b"),
    ("y" * 50 + "_1", "y" * 50 + "-1"),
], ids=["case-underscore", "max-length-last-char", "long-prefix-underscore"])
def test_serve_record_mounts_keep_each_ref(inputs, request_name, probe_name):
    def change(cfg):
        cfg["roles"]["api_request"]["secret_ref"] = "secrets/cortex/" + request_name
        cfg["roles"]["probe"]["secret_ref"] = "secrets/cortex/" + probe_name
    mutate_config(inputs, change)
    serve, _plan, _inventory = rendered(inputs)
    records = [m for m in option(serve["podman"][2], "-v") if "/secrets/cortex/" in m]
    assert sorted(records) == sorted(
        f"{target(inputs, 'secrets/cortex/' + n)}:{target(inputs, 'secrets/cortex/' + n)}:ro"
        for n in (request_name, probe_name, CA_REF.rsplit("/", 1)[1]))


def test_only_loopback_publish(inputs):
    serve, plan, _inventory = rendered(inputs)
    publish = [a for step in plan["steps"] for argv in step["podman"] for a in option(argv, "--publish")]
    assert publish == ["127.0.0.1:18601:8000"]
    assert option(serve["podman"][0], "--publish") == publish
    for argv in serve["podman"]:
        assert not [a for a in argv if a.startswith("--network") and "host" in a]
        assert "-p" not in argv


def test_serve_unlock_is_explicit_and_never_auto_restarted(inputs):
    serve, plan, _inventory = rendered(inputs)
    _pod, core, api = serve["podman"]
    assert plan["inputs"] == ["unlock_descriptor", "key_id", "seed_operation_key"]
    assert "--preserve-fds=1" in api and "--preserve-fds=1" not in core
    assert "--restart=no" in core and "--restart=no" in api
    assert command(api, API_IMAGE)[-4:] == ["--unlock-fd", "3", "--key-id", {"input": "key_id"}]
    for name in OUTPUTS:
        text = (inputs["out"] / name).read_text()
        assert "restartPolicy" not in text and "Always" not in text and "kube" not in text


def test_bootstrap_is_finite_and_ordered(inputs):
    _serve, plan, _inventory = rendered(inputs)
    assert [s["name"] for s in plan["steps"]] == STEPS
    expected = {"core-init": ["bootstrap"], "temporary-db": [],
                "roles-pre": ["api_request", "bootstrap", "installer_enrollment", "migrator", "probe"],
                "migrate": ["migrator"], "roles-post": ["bootstrap"],
                "seed": ["api_request", "bootstrap", "installer_enrollment"], "roles-close": ["bootstrap"],
                "stop-temporary-db": [], "serve": ["api_request", "probe"]}
    assert expected["migrate"] == sorted(MODE_PURPOSES["migrate"])
    assert expected["seed"] == sorted(MODE_PURPOSES["seed"])
    for step in plan["steps"]:
        assert step["secrets"] == expected[step["name"]], step["name"]
        assert isinstance(step["deadline_ms"], int) and step["deadline_ms"] > 0
        assert step.get("retries", 0) == 0
    steps = {s["name"]: s for s in plan["steps"]}
    assert steps["migrate"]["argv"] == operator("migrate")
    assert steps["seed"]["argv"] == operator("seed", "--operation-key", {"input": "seed_operation_key"})
    assert steps["roles-pre"]["argv"][-2:] == ["-f", "/run/cortex/sql/roles-pre.sql"]
    assert plan["restart"] == "never"


def secret_flags(step):
    return sorted(arg for argv in step["podman"] for arg in argv if arg.startswith("--secret="))


def test_bootstrap_projections_follow_core_refs(inputs):
    _serve, plan, _inventory = rendered(inputs)
    steps = {s["name"]: s for s in plan["steps"]}
    migrate, seed = steps["migrate"]["podman"][0], steps["seed"]["podman"][0]
    assert command(migrate, API_IMAGE) == operator("migrate")
    assert sorted(option(migrate, "-v")) == sorted([
        f"{inputs['config']}:{CFG}:ro",
        f"{target(inputs, REFS['migrator'])}:{target(inputs, REFS['migrator'])}:ro",
        f"{target(inputs, CA_REF)}:{target(inputs, CA_REF)}:ro"])
    assert option(migrate, "--tmpfs") == private_parents(inputs)
    # Seed alone gets its one encrypted-record directory writable (atomic replace + lock).
    records = inputs["vault"] / "secrets" / "cortex"
    assert sorted(option(seed, "-v")) == sorted([f"{inputs['config']}:{CFG}:ro", f"{records}:{records}:rw"])
    assert option(seed, "--tmpfs") == private_parents(inputs, records=False)
    for argv in (migrate, seed):
        assert "--preserve-fds=1" in argv
        assert not [a for a in argv if a.startswith("--secret")]
    assert secret_flags(steps["core-init"]) == [
        "--secret=cortex-v2-pg_boot,type=mount,target=/run/cortex/bootstrap/initdb-password,mode=0400"]
    for name in OUTPUTS:
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


def core_reason(path):
    with pytest.raises(Refusal) as core:
        read_config_file(path, mode="serve")
    return core.value.reason


def test_config_leaf_symlink_refuses_like_core(inputs):
    real = inputs["config"].with_name("real.json")
    inputs["config"].rename(real)
    inputs["config"].symlink_to(real)
    assert core_reason(inputs["config"]) == "private_file_required"
    assert refusal(run(inputs)) == "private_file_required"
    assert not inputs["out"].exists()


def test_config_parent_symlink_refuses_like_core(inputs):
    link = inputs["root"] / "linked-config"
    link.symlink_to(inputs["config"].parent, target_is_directory=True)
    path = link / inputs["config"].name
    assert core_reason(path) == "private_file_required"
    assert refusal(run(inputs, config=path)) == "private_file_required"
    assert not inputs["out"].exists()


def test_config_relative_path_refuses_like_core(inputs):
    relative = pathlib.Path(inputs["config"].name)
    assert core_reason(relative) == "private_file_required"
    assert refusal(run(inputs, config=relative, cwd=inputs["config"].parent)) == "private_file_required"
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
