#!/usr/bin/env python3
"""Render the option A serve pod (DB + API only) and the finite bootstrap plan.

stdlib only; run as `python3 -I render.py`. Contract: contract.md next to this file.
Every input is explicit: a missing one is a typed refusal, never a default.
"""
import hashlib
import json
import os
import pathlib
import shutil
import sys
import tempfile

UNIT = "ship/run"
PRODUCT = "cortex-v2"
OPERATIONS_SHA256 = "51de95ac63cd19e59b046a9db913e38aa790687da48a87b2174468c6873a873b"
FLAGS = ("--config", "--operations", "--release", "--core-conf", "--out")
PY = "/opt/cortex/python/bin/python3"
CONFIG_MOUNT = "/run/cortex/config/cortex.json"
SECRET_ROOT = "/run/cortex/secrets"
DATA = "/var/lib/cortex/core"
DATA_VOLUME = "cortex-v2-core"
BOOTSTRAP_POD = "cortex-v2-bootstrap"
POD_FILE = "cortex-v2.pod.yaml"
PURPOSES = ("api_request", "probe", "bootstrap", "migrator", "installer_enrollment")
SERVE_SECRETS = ("api_request", "probe")
ROLE_LOGIN_SECRETS = ("api_request", "installer_enrollment", "migrator", "probe")
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


class Refusal(Exception):
    """A typed refusal: reason is printed as JSON and nothing is written."""


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


def reject_duplicates(name):
    def pairs(items):
        seen = {}
        for key, value in items:
            if key in seen:
                raise Refusal(f"duplicate_key:{name}.{key}")
            seen[key] = value
        return seen
    return pairs


def read_bytes(path, name):
    try:
        return pathlib.Path(path).read_bytes()
    except OSError:
        raise Refusal(f"input_missing:{name}") from None


def decode(raw, name):
    try:
        return json.loads(raw, object_pairs_hook=reject_duplicates(name))
    except ValueError:
        raise Refusal(f"input_invalid:{name}") from None


def need(node, path, name):
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise Refusal(f"input_missing:{name}.{'.'.join(path)}")
        node = node[key]
    return node


def port(node, path):
    value = need(node, path, "config")
    if type(value) is not int or not 1 <= value <= 65535:
        raise Refusal(f"input_invalid:config.{'.'.join(path)}")
    return value


def check_config(config):
    if "ai_mode" not in config:
        raise Refusal("ai_mode_missing")
    if config["ai_mode"] != "off":
        raise Refusal(f"ai_mode_not_off:{config['ai_mode']}")
    need(config, ("installation_id",), "config")
    host = need(config, ("publish", "host"), "config")
    if host != "127.0.0.1":
        raise Refusal(f"unsafe_publish:{host}")
    tls = need(config, ("database", "tls", "mode"), "config")
    if tls != "verify-full":
        raise Refusal(f"transport_unratified:{tls}")
    port(config, ("publish", "port"))
    port(config, ("bind", "port"))
    port(config, ("database", "port"))
    purposes = need(config, ("purposes",), "config")
    for purpose in PURPOSES:
        entry = purposes.get(purpose) if isinstance(purposes, dict) else None
        if not (isinstance(entry, dict) and isinstance(entry.get("login"), str)
                and isinstance(entry.get("secret_ref"), str)):
            raise Refusal(f"purpose_missing:{purpose}")


def check_conf_dir(core_conf):
    root = pathlib.Path(core_conf)
    for rel in CONF_FILES:
        path = root / rel
        if path.is_symlink() or not path.is_file():
            raise Refusal(f"input_missing:core-conf/{rel}")
    return root.resolve()


def resolve_vector(ops, mode, config, release):
    fields = {"target.core.initdb.value.path": release["core"]["initdb"],
              "target.core.postgres.value.path": release["core"]["postgres"],
              "config.roles.bootstrap.binding.value.sql_name": config["purposes"]["bootstrap"]["login"]}
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


def secret_volume(purpose):
    return "secret-" + purpose.replace("_", "-")


def serve_pod(config, release, config_path, conf_root, core_server):
    labels = {"ai.kaidera.product": PRODUCT,
              "ai.kaidera.cortex.source": release["source_commit"],
              "ai.kaidera.cortex.installation": config["installation_id"]}
    locked = {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
              "capabilities": {"drop": ["ALL"]}}
    core = {"name": "core", "image": release["core"]["image"], "command": core_server,
            "securityContext": locked,
            "volumeMounts": [
                {"name": "core-data", "mountPath": DATA},
                {"name": "core-conf", "mountPath": "/run/cortex/postgresql.conf", "readOnly": True},
                {"name": "core-hba", "mountPath": "/run/cortex/pg_hba.conf", "readOnly": True},
                {"name": "core-run", "mountPath": "/run/postgresql"},
                {"name": "core-tmp", "mountPath": "/tmp"}]}
    api = {"name": "api", "image": release["api"]["image"], "command": cortex_argv("serve"),
           "ports": [{"containerPort": config["bind"]["port"], "hostIP": "127.0.0.1",
                      "hostPort": config["publish"]["port"]}],
           "securityContext": locked,
           "volumeMounts": [{"name": "config", "mountPath": CONFIG_MOUNT, "readOnly": True},
                            {"name": "api-tmp", "mountPath": "/tmp"}]
           + [{"name": secret_volume(p), "mountPath": f"{SECRET_ROOT}/{p}", "readOnly": True}
              for p in SERVE_SECRETS]}
    volumes = [
        {"name": "core-data", "persistentVolumeClaim": {"claimName": DATA_VOLUME}},
        {"name": "config", "hostPath": {"path": str(config_path), "type": "File"}},
        {"name": "core-conf", "hostPath": {"path": str(conf_root / "postgresql.conf"), "type": "File"}},
        {"name": "core-hba", "hostPath": {"path": str(conf_root / "pg_hba.conf"), "type": "File"}},
        {"name": "core-run", "emptyDir": {}},
        {"name": "core-tmp", "emptyDir": {}},
        {"name": "api-tmp", "emptyDir": {}},
    ] + [{"name": secret_volume(p),
          "secret": {"secretName": f"{PRODUCT}-{p}", "defaultMode": 0o400}}
         for p in SERVE_SECRETS]
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": PRODUCT, "labels": labels},
            "spec": {"hostNetwork": False, "restartPolicy": "Always",
                     "containers": [core, api], "volumes": volumes}}


def secret_flags(purposes):
    return [f"--secret={PRODUCT}-{p},type=mount,target={SECRET_ROOT}/{p},mode=0400"
            for p in purposes]


def plan(ops, config, release, config_path, conf_root, core_init, core_server):
    budgets = ops["budgets"]
    label = ["--label", f"ai.kaidera.product={PRODUCT}"]
    core_img, api_img = release["core"]["image"], release["api"]["image"]
    data = ["-v", f"{DATA_VOLUME}:{DATA}"]
    conf = ["-v", f"{conf_root / 'postgresql.conf'}:/run/cortex/postgresql.conf:ro",
            "-v", f"{conf_root / 'pg_hba.conf'}:/run/cortex/pg_hba.conf:ro"]
    cfg = ["-v", f"{config_path}:{CONFIG_MOUNT}:ro"]
    in_pod = ["podman", "run", "--rm", "--pod", BOOTSTRAP_POD, *label, *LOCKED]

    def sql(step):
        argv = [release["core"]["psql"], "--no-psqlrc", "-v", "ON_ERROR_STOP=1",
                "-h", "127.0.0.1", "-p", str(config["database"]["port"]),
                "-U", config["purposes"]["bootstrap"]["login"], "-d", "postgres",
                "-f", f"/run/cortex/sql/{step}.sql"]
        logins = ROLE_LOGIN_SECRETS if step == "roles-pre" else ()
        podman = [*in_pod, "--name", f"{BOOTSTRAP_POD}-{step}",
                  f"--secret={PRODUCT}-bootstrap,type=env,target=PGPASSWORD",
                  *secret_flags(logins),
                  "-v", f"{conf_root / 'sql' / (step + '.sql')}:/run/cortex/sql/{step}.sql:ro",
                  core_img, *argv]
        return {"name": step, "image": core_img, "argv": argv,
                "secrets": sorted(("bootstrap", *logins)), "podman": [podman]}

    def cortex(step, verb, purpose):
        argv = cortex_argv(verb)
        podman = [*in_pod, "--name", f"{BOOTSTRAP_POD}-{step}", *cfg,
                  *secret_flags([purpose]), api_img, *argv]
        return {"name": step, "image": api_img, "argv": argv, "secrets": [purpose],
                "podman": [podman]}

    steps = [
        {"name": "core-init", "image": core_img, "argv": core_init, "secrets": ["bootstrap"],
         "when": f"absent:{DATA}/PG_VERSION",
         "podman": [["podman", "run", "--rm", "--name", f"{PRODUCT}-core-init", *label, *LOCKED,
                     "--network=none", *data,
                     f"--secret={PRODUCT}-bootstrap,type=mount,target=/run/cortex/bootstrap/initdb-password,mode=0400",
                     core_img, *core_init]]},
        {"name": "temporary-db", "image": core_img, "argv": core_server, "secrets": [],
         "podman": [["podman", "pod", "create", "--name", BOOTSTRAP_POD, *label, "--network=none"],
                    ["podman", "run", "-d", "--pod", BOOTSTRAP_POD, "--name", f"{BOOTSTRAP_POD}-core",
                     *label, *LOCKED, *data, *conf, "--tmpfs", "/run/postgresql", "--tmpfs", "/tmp",
                     core_img, *core_server]]},
        sql("roles-pre"),
        cortex("migrate", "migrate", "migrator"),
        sql("roles-post"),
        cortex("seed", "seed", "installer_enrollment"),
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
    if hashlib.sha256(ops_raw).hexdigest() != OPERATIONS_SHA256:
        raise Refusal("operations_mismatch")
    ops = decode(ops_raw, "operations")
    config = decode(read_bytes(values["--config"], "config"), "config")
    check_config(config)
    release = decode(read_bytes(values["--release"], "release"), "release")
    for path in RELEASE_FIELDS:
        need(release, path, "release")
    conf_root = check_conf_dir(values["--core-conf"])
    config_path = pathlib.Path(values["--config"]).resolve()
    core_init = resolve_vector(ops, "core-init", config, release)
    core_server = resolve_vector(ops, "core-server", config, release)
    pod = serve_pod(config, release, config_path, conf_root, core_server)
    bootstrap = plan(ops, config, release, config_path, conf_root, core_init, core_server)
    inventory = {
        "operations_sha256": OPERATIONS_SHA256,
        "source_commit": release["source_commit"],
        "serve": {"pod": PRODUCT, "containers": [c["name"] for c in pod["spec"]["containers"]],
                  "images": {c["name"]: c["image"] for c in pod["spec"]["containers"]},
                  "secrets": list(SERVE_SECRETS),
                  "published": [{"host": "127.0.0.1", "port": config["publish"]["port"]}]},
        "bootstrap": {"pod": BOOTSTRAP_POD, "steps": [s["name"] for s in bootstrap["steps"]]},
        "ready": {"url": f"http://127.0.0.1:{config['publish']['port']}/health",
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
        print(json.dumps({"reason": str(refusal), "unit": UNIT}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
