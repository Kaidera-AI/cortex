#!/usr/bin/env python3
"""Render the option A serve pod (DB + API only) and the finite bootstrap plan.

Run as `python3 -I render.py`; stdlib host Python plus the release's own Core source.
The config is read only by Core's private-file reader and closed decoder, once per mode
view; every secret projection target is Core's vault_root/secret_ref. Contract: contract.md.
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "src"))
from cortex_core.release.codec import sha256  # noqa: E402
from cortex_core.runtime.bootstrap_config import MODE_PURPOSES, decode_config  # noqa: E402
from cortex_core.runtime.contracts import (MAX_CONFIG_BYTES, MAX_LOCK_BYTES,  # noqa: E402
                                           RC0_SHA256, decode)
from cortex_core.runtime.credentials import read_private_file  # noqa: E402
from cortex_core.runtime.signed_member import Refusal  # noqa: E402

UNIT = "ship/run"
PRODUCT = "cortex-v2"
FLAGS = ("--config", "--operations", "--release", "--core-conf", "--out")
PY = "/opt/cortex/python/bin/python3"
CONFIG_MOUNT = "/run/cortex/config/cortex.json"
DATA = "/var/lib/cortex/core"
DATA_VOLUME = "cortex-v2-core"
BOOTSTRAP_POD = "cortex-v2-bootstrap"
POD_FILE = "cortex-v2.pod.yaml"
VIEWS = ("serve", "migrate", "seed")
# Installer (nix) SQL steps are not Core modes: they log in as bootstrap; roles-pre also
# reads the login secrets it sets.
ROLE_LOGINS = ("api_request", "installer_enrollment", "migrator", "probe")
CONF_FILES = ("postgresql.conf", "pg_hba.conf",
              "sql/roles-pre.sql", "sql/roles-post.sql", "sql/roles-close.sql")
RELEASE_FIELDS = (("source_commit",), ("core", "image"), ("core", "initdb"),
                  ("core", "postgres"), ("core", "psql"), ("api", "image"))
# Each step's deadline is one named budget of the pinned operations contract.
STEP_BUDGET = {"core-init": "initdb_ms", "temporary-db": "initdb_ms",
               "roles-pre": "migration_ms", "migrate": "migration_ms",
               "roles-post": "migration_ms", "seed": "migration_ms",
               "roles-close": "migration_ms", "stop-temporary-db": "shutdown_ms",
               "serve": "initdb_ms"}
LOCKED = ["--read-only", "--cap-drop=ALL", "--security-opt", "no-new-privileges"]


def parse_args(argv):
    values = {}
    pending = list(argv)
    while pending:
        flag = pending.pop(0)
        if flag not in FLAGS or flag in values:
            raise Refusal(f"bad_argument:{flag}")
        if not pending:
            raise Refusal(f"input_missing:{flag}")
        values[flag] = pending.pop(0)
    for flag in FLAGS:
        if flag not in values:
            raise Refusal(f"input_missing:{flag}")
    return values


def read_bytes(path, name):
    try:
        return pathlib.Path(path).read_bytes()
    except OSError:
        raise Refusal(f"input_missing:{name}") from None


def need(node, path, name):
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise Refusal(f"input_missing:{name}.{'.'.join(path)}")
        node = node[key]
    return node


def check_conf_dir(core_conf):
    root = pathlib.Path(core_conf)
    for rel in CONF_FILES:
        path = root / rel
        if path.is_symlink() or not path.is_file():
            raise Refusal(f"input_missing:core-conf/{rel}")
    return root.resolve()


def volume_id(ref):
    # A DNS-1123 label from the whole case-sensitive ref; serve_pod refuses any collision.
    return "proj-" + sha256(ref.encode())[:16]


class Projection:
    """One Core ref: vault custody copy `cortex-v2-<name>` at vault_root/secret_ref."""

    def __init__(self, vault_root, ref):
        self.name = ref.rsplit("/", 1)[1]
        self.source = f"{PRODUCT}-{self.name}"
        self.target = str(pathlib.PurePosixPath(vault_root) / ref)
        self.volume = volume_id(ref)

    def flag(self):
        return f"--secret={self.source},type=mount,target={self.target},mode=0400"


def resolve_vector(ops, mode, logins, release):
    fields = {"target.core.initdb.value.path": release["core"]["initdb"],
              "target.core.postgres.value.path": release["core"]["postgres"],
              "config.roles.bootstrap.binding.value.sql_name": logins["bootstrap"]}
    argv = []
    for item in ops["operation_vectors"][mode]["argv"]:
        if isinstance(item, dict):
            if item.get("field") not in fields:
                raise Refusal(f"vector_field_unknown:{item.get('field')}")
            item = fields[item["field"]]
        argv.append(item)
    return argv


def cortex_argv(verb, *extra):
    return [PY, "-I", "-B", "-m", "cortex_core.cli", verb, "--config", CONFIG_MOUNT, *extra]


def serve_pod(serve, release, config_path, conf_root, core_server, ca):
    labels = {"ai.kaidera.product": PRODUCT,
              "ai.kaidera.cortex.source": release["source_commit"],
              "ai.kaidera.cortex.installation": serve.installation_id}
    locked = {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
              "capabilities": {"drop": ["ALL"]}}
    projections = [Projection(serve.vault_root, serve.roles[p].secret_ref)
                   for p in MODE_PURPOSES["serve"]] + [ca]
    core = {"name": "core", "image": release["core"]["image"], "command": core_server,
            "securityContext": locked,
            "volumeMounts": [
                {"name": "core-data", "mountPath": DATA},
                {"name": "core-conf", "mountPath": "/run/cortex/postgresql.conf", "readOnly": True},
                {"name": "core-hba", "mountPath": "/run/cortex/pg_hba.conf", "readOnly": True},
                {"name": "core-run", "mountPath": "/run/postgresql"},
                {"name": "core-tmp", "mountPath": "/tmp"}]}
    api = {"name": "api", "image": release["api"]["image"], "command": cortex_argv("serve"),
           "ports": [{"containerPort": serve.bind["port"], "hostIP": "127.0.0.1",
                      "hostPort": serve.publish["port"]}],
           "securityContext": locked,
           "volumeMounts": [{"name": "config", "mountPath": CONFIG_MOUNT, "readOnly": True},
                            {"name": "api-tmp", "mountPath": "/tmp"}]
           + [{"name": p.volume, "mountPath": p.target, "subPath": p.name, "readOnly": True}
              for p in projections]}
    volumes = [
        {"name": "core-data", "persistentVolumeClaim": {"claimName": DATA_VOLUME}},
        {"name": "config", "hostPath": {"path": str(config_path), "type": "File"}},
        {"name": "core-conf", "hostPath": {"path": str(conf_root / "postgresql.conf"), "type": "File"}},
        {"name": "core-hba", "hostPath": {"path": str(conf_root / "pg_hba.conf"), "type": "File"}},
        {"name": "core-run", "emptyDir": {}},
        {"name": "core-tmp", "emptyDir": {}},
        {"name": "api-tmp", "emptyDir": {}},
    ] + [{"name": p.volume, "secret": {"secretName": p.source, "defaultMode": 0o400}}
         for p in projections]
    names = [v["name"] for v in volumes]
    if len(set(names)) != len(names):
        raise Refusal("projection_collision")
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": PRODUCT, "labels": labels},
            "spec": {"hostNetwork": False, "restartPolicy": "Always",
                     "containers": [core, api], "volumes": volumes}}


def plan(ops, views, roles, release, config_path, conf_root, core_init, core_server, ca):
    budgets = ops["budgets"]
    vault = views["serve"].vault_root
    database = views["serve"].database
    label = ["--label", f"ai.kaidera.product={PRODUCT}"]
    core_img, api_img = release["core"]["image"], release["api"]["image"]
    data = ["-v", f"{DATA_VOLUME}:{DATA}"]
    conf = ["-v", f"{conf_root / 'postgresql.conf'}:/run/cortex/postgresql.conf:ro",
            "-v", f"{conf_root / 'pg_hba.conf'}:/run/cortex/pg_hba.conf:ro"]
    cfg = ["-v", f"{config_path}:{CONFIG_MOUNT}:ro"]
    in_pod = ["podman", "run", "--rm", "--pod", BOOTSTRAP_POD, *label, *LOCKED]
    boot = Projection(vault, roles["bootstrap"].secret_ref)

    def sql(step):
        argv = [release["core"]["psql"], "--no-psqlrc", "-v", "ON_ERROR_STOP=1",
                "-h", database["host"], "-p", str(database["port"]),
                "-U", roles["bootstrap"].login, "-d", "postgres",
                "-f", f"/run/cortex/sql/{step}.sql"]
        logins = ROLE_LOGINS if step == "roles-pre" else ()
        mounted = [Projection(vault, roles[p].secret_ref) for p in logins] + [ca]
        podman = [*in_pod, "--name", f"{BOOTSTRAP_POD}-{step}",
                  f"--secret={boot.source},type=env,target=PGPASSWORD",
                  "-e", "PGSSLMODE=verify-full", "-e", f"PGSSLROOTCERT={ca.target}",
                  *[p.flag() for p in mounted],
                  "-v", f"{conf_root / 'sql' / (step + '.sql')}:/run/cortex/sql/{step}.sql:ro",
                  core_img, *argv]
        return {"name": step, "image": core_img, "argv": argv,
                "secrets": sorted(("bootstrap", *logins)), "podman": [podman]}

    def cortex(step):
        argv = cortex_argv(step)
        purposes = MODE_PURPOSES[step]
        mounted = [Projection(vault, views[step].roles[p].secret_ref) for p in purposes] + [ca]
        podman = [*in_pod, "--name", f"{BOOTSTRAP_POD}-{step}", *cfg,
                  *[p.flag() for p in mounted], api_img, *argv]
        return {"name": step, "image": api_img, "argv": argv, "secrets": sorted(purposes),
                "podman": [podman]}

    steps = [
        {"name": "core-init", "image": core_img, "argv": core_init, "secrets": ["bootstrap"],
         "when": f"absent:{DATA}/PG_VERSION",
         "podman": [["podman", "run", "--rm", "--name", f"{PRODUCT}-core-init", *label, *LOCKED,
                     "--network=none", *data,
                     f"--secret={boot.source},type=mount,target=/run/cortex/bootstrap/initdb-password,mode=0400",
                     core_img, *core_init]]},
        {"name": "temporary-db", "image": core_img, "argv": core_server, "secrets": [],
         "podman": [["podman", "pod", "create", "--name", BOOTSTRAP_POD, *label, "--network=none"],
                    ["podman", "run", "-d", "--pod", BOOTSTRAP_POD, "--name", f"{BOOTSTRAP_POD}-core",
                     *label, *LOCKED, *data, *conf, "--tmpfs", "/run/postgresql", "--tmpfs", "/tmp",
                     core_img, *core_server]]},
        sql("roles-pre"),
        cortex("migrate"),
        sql("roles-post"),
        cortex("seed"),
        sql("roles-close"),
        {"name": "stop-temporary-db", "secrets": [],
         "podman": [["podman", "pod", "stop", "--time", str(budgets["shutdown_ms"] // 1000), BOOTSTRAP_POD],
                    ["podman", "pod", "rm", BOOTSTRAP_POD]]},
        {"name": "serve", "secrets": [], "pod_file": POD_FILE,
         "podman": [["podman", "kube", "play", POD_FILE]]},
    ]
    for step in steps:
        key = STEP_BUDGET[step["name"]]
        if not isinstance(budgets.get(key), int):
            raise Refusal(f"input_missing:operations.budgets.{key}")
        step["deadline_ms"] = budgets[key]
        step["retries"] = 0
    return {"pod": BOOTSTRAP_POD, "restart": "never", "steps": steps}


def render(values):
    ops_raw = read_bytes(values["--operations"], "operations")
    if sha256(ops_raw) != RC0_SHA256["runtime-operations-v1.json"]:
        raise Refusal("operations_mismatch")
    ops = decode(ops_raw, limit=MAX_LOCK_BYTES)
    # Core's one reader and closed decoder; each mode view admits only its own purposes. The
    # caller's spelling goes to Core unresolved, so its no-follow walk sees every symlink.
    config_path = pathlib.Path(values["--config"])
    raw = read_private_file(config_path, limit=MAX_CONFIG_BYTES)
    views = {mode: decode_config(raw, mode=mode) for mode in VIEWS}
    roles = {p: role for view in views.values() for p, role in view.roles.items()}
    release = decode(read_bytes(values["--release"], "release"), limit=MAX_CONFIG_BYTES)
    for path in RELEASE_FIELDS:
        need(release, path, "release")
    conf_root = check_conf_dir(values["--core-conf"])
    serve = views["serve"]
    ca = Projection(serve.vault_root, serve.database["tls"]["root_cert_ref"])
    logins = {p: role.login for p, role in roles.items()}
    core_init = resolve_vector(ops, "core-init", logins, release)
    core_server = resolve_vector(ops, "core-server", logins, release)
    pod = serve_pod(serve, release, config_path, conf_root, core_server, ca)
    bootstrap = plan(ops, views, roles, release, config_path, conf_root, core_init, core_server, ca)
    inventory = {
        "operations_sha256": RC0_SHA256["runtime-operations-v1.json"],
        "source_commit": release["source_commit"],
        "serve": {"pod": PRODUCT, "containers": [c["name"] for c in pod["spec"]["containers"]],
                  "images": {c["name"]: c["image"] for c in pod["spec"]["containers"]},
                  "secrets": sorted(MODE_PURPOSES["serve"]),
                  "published": [{"host": "127.0.0.1", "port": serve.publish["port"]}]},
        "bootstrap": {"pod": BOOTSTRAP_POD, "steps": [s["name"] for s in bootstrap["steps"]]},
        "ready": {"url": f"http://127.0.0.1:{serve.publish['port']}/health",
                  "fields": list(ops["health"]["c11_fields"]),
                  "source_commit": release["source_commit"]},
        "status_argv": cortex_argv("status", "--json"),
    }
    return {POD_FILE: pod, "bootstrap.plan.json": bootstrap, "inventory.json": inventory}


def publish(out, files):
    out = pathlib.Path(out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise Refusal("out_not_empty")
    if not out.parent.is_dir():
        raise Refusal("input_missing:--out parent")
    staging = pathlib.Path(tempfile.mkdtemp(prefix=".render-", dir=out.parent))
    try:
        for name, body in files.items():
            (staging / name).write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
        if out.exists():
            out.rmdir()
        os.rename(staging, out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv):
    try:
        values = parse_args(argv)
        publish(values["--out"], render(values))
    except Refusal as refusal:
        print(json.dumps({"reason": refusal.reason, "unit": UNIT}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
