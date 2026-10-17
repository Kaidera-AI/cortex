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

SOURCE = "7a1078213ac2497d6219e7778c7866001f97f30b"
CORE_IMAGE = "sha256:" + "c" * 64
API_IMAGE = "sha256:" + "a" * 64
PY = "/opt/cortex/python/bin/python3"
CFG = "/run/cortex/config/cortex.json"
PURPOSES = ("api_request", "probe", "bootstrap", "migrator", "installer_enrollment")
STEPS = ["core-init", "temporary-db", "roles-pre", "migrate", "roles-post",
         "seed", "roles-close", "stop-temporary-db", "serve"]
FORBIDDEN = re.compile(r"embed|tei|qdrant|rerank|supervisor|conductor|observer|worker", re.I)


def base_config():
    return {
        "version": 1,
        "installation_id": "11111111-1111-4111-8111-111111111111",
        "ai_mode": "off",
        "bind": {"host": "0.0.0.0", "port": 8000},
        "publish": {"host": "127.0.0.1", "port": 18601},
        "database": {"host": "127.0.0.1", "port": 5432, "name": "cortex",
                     "tls": {"mode": "verify-full"}},
        "purposes": {p: {"login": "cortex_" + p, "secret_ref": "secrets/cortex/" + p}
                     for p in PURPOSES},
        "vault_root": "/vault/helix",
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
    paths = {"config": tmp_path / "cortex.json", "release": tmp_path / "release.json",
             "operations": tmp_path / "ops.json", "core_conf": conf, "out": tmp_path / "out"}
    paths["config"].write_text(json.dumps(base_config()))
    paths["release"].write_text(json.dumps(base_release()))
    paths["operations"].write_bytes(OPERATIONS.read_bytes())
    return paths


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
    fixture_root = str(inputs["config"].parent.resolve())
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


def test_serve_mounts_only_request_and_probe(inputs):
    pod, _plan, inventory = rendered(inputs)
    core, api = pod["spec"]["containers"]
    assert not [m for m in core["volumeMounts"] if m["name"].startswith("secret-")]
    api_secrets = {m["name"]: m["mountPath"] for m in api["volumeMounts"]
                   if m["name"].startswith("secret-")}
    assert api_secrets == {"secret-api-request": "/run/cortex/secrets/api_request",
                           "secret-probe": "/run/cortex/secrets/probe"}
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    assert volumes["secret-api-request"]["secret"]["secretName"] == "cortex-v2-api_request"
    assert volumes["secret-probe"]["secret"]["secretName"] == "cortex-v2-probe"
    host_paths = sorted((v["hostPath"]["path"], v["hostPath"]["type"])
                        for v in volumes.values() if "hostPath" in v)
    conf = inputs["core_conf"]
    assert host_paths == sorted([(str(inputs["config"]), "File"),
                                 (str(conf / "postgresql.conf"), "File"),
                                 (str(conf / "pg_hba.conf"), "File")])
    assert inventory["serve"]["secrets"] == ["api_request", "probe"]


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
                "seed": ["installer_enrollment"], "roles-close": ["bootstrap"],
                "stop-temporary-db": [], "serve": []}
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
    cfg = base_config()
    fn(cfg)
    inputs["config"].write_text(json.dumps(cfg))


@pytest.mark.parametrize("change,reason", [
    (lambda c: c.pop("ai_mode"), "ai_mode_missing"),
    (lambda c: c.__setitem__("ai_mode", "hosted"), "ai_mode_not_off:hosted"),
    (lambda c: c["database"]["tls"].__setitem__("mode", "disable"), "transport_unratified:disable"),
    (lambda c: c["publish"].__setitem__("host", "0.0.0.0"), "unsafe_publish:0.0.0.0"),
    (lambda c: c["purposes"].pop("probe"), "purpose_missing:probe"),
    (lambda c: c["purposes"].pop("migrator"), "purpose_missing:migrator"),
    (lambda c: c.pop("installation_id"), "input_missing:config.installation_id"),
    (lambda c: c["publish"].pop("port"), "input_missing:config.publish.port"),
])
def test_config_refusals(inputs, change, reason):
    mutate_config(inputs, change)
    assert refusal(run(inputs)) == reason
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
