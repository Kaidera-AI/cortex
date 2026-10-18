#!/usr/bin/env python3
"""Render the option A finite bootstrap plan and its DB + API only serve step.

Run as `python3 -I render.py`; stdlib host Python plus the release's own Core source.
The config is read only by Core's private-file reader and closed decoder, once per mode
view. Core containers run the actual `cortex_core.runtime.operator` with one explicitly
inherited unlock descriptor and only their own encrypted records. Contract: contract.md.
"""
import fcntl
import json
import os
import pathlib
import re
import select
import shutil
import signal
import sys
import tempfile
import termios
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "src"))
from cortex_core.release.codec import sha256  # noqa: E402
from cortex_core.runtime.bootstrap_config import MODE_PURPOSES, decode_config  # noqa: E402
from cortex_core.runtime.contracts import (MAX_CONFIG_BYTES, MAX_LOCK_BYTES,  # noqa: E402
                                           RC0_SHA256, decode)
from cortex_core.runtime.credentials import read_private_file  # noqa: E402
from cortex_core.runtime.custody_fs import anonymous_pipe  # noqa: E402
from cortex_core.runtime.signed_member import Refusal  # noqa: E402

UNIT = "ship/run"
PRODUCT = "cortex-v2"
FLAGS = ("--config", "--operations", "--release", "--core-conf", "--out")
PY = "/opt/cortex/python/bin/python3"
CONFIG_MOUNT = "/run/cortex/config/cortex.json"
DATA = "/var/lib/cortex/core"
DATA_VOLUME = "cortex-v2-core"
BOOTSTRAP_POD = "cortex-v2-bootstrap"
OPERATOR = [PY, "-I", "-B", "-m", "cortex_core.runtime.operator"]
# --preserve-fds=1 hands each Core container the runner's one unlock descriptor as fd 3.
UNLOCK_FD = "3"
# Explicit operator inputs the runner supplies at execution; never rendered or defaulted.
INPUTS = ("unlock_descriptor", "key_id", "seed_operation_key")
KEY_ID = {"input": "key_id"}
OPERATION_KEY = {"input": "seed_operation_key"}
PRIVATE = "rw,mode=0700,size=64k"
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


class Projection:
    """One Core ref as a disposable podman secret; only the installer's (nix) SQL steps use these."""

    def __init__(self, vault_root, ref):
        self.name = ref.rsplit("/", 1)[1]
        self.source = f"{PRODUCT}-{self.name}"
        self.target = str(pathlib.PurePosixPath(vault_root) / ref)

    def flag(self):
        return f"--secret={self.source},type=mount,target={self.target},mode=0400"


def vault_mounts(vault, refs=(), *, records_writable=False):
    """Core's no-follow custody walk needs vault_root, secrets and secrets/cortex owned and 0700:
    private tmpfs parents, then each encrypted record read-only at its own Core path. Seed alone
    binds its one record directory writable (atomic replace and lock); never the whole vault."""
    root = pathlib.PurePosixPath(vault)
    records = root / "secrets" / "cortex"
    parents = [root, root / "secrets"] + ([] if records_writable else [records])
    argv = [arg for path in parents for arg in ("--tmpfs", f"{path}:{PRIVATE}")]
    if records_writable:
        return argv + ["-v", f"{records}:{records}:rw"]
    return argv + [arg for ref in refs for arg in ("-v", f"{root / ref}:{root / ref}:ro")]


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


def operator_argv(verb, *extra):
    argv = [*OPERATOR, verb, "--config", CONFIG_MOUNT]
    if verb != "status":
        argv += ["--unlock-fd", UNLOCK_FD, "--key-id", KEY_ID]
    return argv + list(extra)


def serve_step(serve, release, config_path, conf_root, core_server, ca_ref):
    """DB + API only; the API gets its unlock descriptor explicitly and is never auto-restarted."""
    labels = [arg for label in (f"ai.kaidera.product={PRODUCT}",
                                f"ai.kaidera.cortex.source={release['source_commit']}",
                                f"ai.kaidera.cortex.installation={serve.installation_id}")
              for arg in ("--label", label)]
    in_pod = ["podman", "run", "-d", "--pod", PRODUCT, "--label", f"ai.kaidera.product={PRODUCT}",
              *LOCKED, "--restart=no"]
    refs = [serve.roles[p].secret_ref for p in MODE_PURPOSES["serve"]] + [ca_ref]
    argv = operator_argv("serve")
    return {"name": "serve", "image": release["api"]["image"], "argv": argv,
            "secrets": sorted(MODE_PURPOSES["serve"]),
            "podman": [
                ["podman", "pod", "create", "--name", PRODUCT, *labels,
                 "--publish", f"127.0.0.1:{serve.publish['port']}:{serve.bind['port']}"],
                [*in_pod, "--name", f"{PRODUCT}-core", "-v", f"{DATA_VOLUME}:{DATA}",
                 "-v", f"{conf_root / 'postgresql.conf'}:/run/cortex/postgresql.conf:ro",
                 "-v", f"{conf_root / 'pg_hba.conf'}:/run/cortex/pg_hba.conf:ro",
                 "--tmpfs", "/run/postgresql", "--tmpfs", "/tmp", release["core"]["image"], *core_server],
                [*in_pod, "--name", f"{PRODUCT}-api", "--preserve-fds=1", "--tmpfs", "/tmp",
                 "-v", f"{config_path}:{CONFIG_MOUNT}:ro", *vault_mounts(serve.vault_root, refs),
                 release["api"]["image"], *argv]]}


def plan(ops, views, roles, release, config_path, conf_root, core_init, core_server, ca_ref):
    budgets = ops["budgets"]
    serve = views["serve"]
    vault = serve.vault_root
    database = serve.database
    label = ["--label", f"ai.kaidera.product={PRODUCT}"]
    core_img, api_img = release["core"]["image"], release["api"]["image"]
    data = ["-v", f"{DATA_VOLUME}:{DATA}"]
    conf = ["-v", f"{conf_root / 'postgresql.conf'}:/run/cortex/postgresql.conf:ro",
            "-v", f"{conf_root / 'pg_hba.conf'}:/run/cortex/pg_hba.conf:ro"]
    cfg = ["-v", f"{config_path}:{CONFIG_MOUNT}:ro"]
    in_pod = ["podman", "run", "--rm", "--pod", BOOTSTRAP_POD, *label, *LOCKED]
    boot = Projection(vault, roles["bootstrap"].secret_ref)
    ca = Projection(vault, ca_ref)

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

    def core(step, *extra):
        # The actual Core operator, unlocked by the one explicitly inherited descriptor.
        argv = operator_argv(step, *extra)
        purposes = MODE_PURPOSES[step]
        if step == "seed":
            mounts = vault_mounts(vault, records_writable=True)
        else:
            mounts = vault_mounts(vault, [views[step].roles[p].secret_ref for p in purposes] + [ca_ref])
        podman = [*in_pod, "--name", f"{BOOTSTRAP_POD}-{step}", "--preserve-fds=1", *cfg, *mounts,
                  api_img, *argv]
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
        core("migrate"),
        sql("roles-post"),
        core("seed", "--operation-key", OPERATION_KEY),
        sql("roles-close"),
        {"name": "stop-temporary-db", "secrets": [],
         "podman": [["podman", "pod", "stop", "--time", str(budgets["shutdown_ms"] // 1000), BOOTSTRAP_POD],
                    ["podman", "pod", "rm", BOOTSTRAP_POD]]},
        serve_step(serve, release, config_path, conf_root, core_server, ca_ref),
    ]
    for step in steps:
        key = STEP_BUDGET[step["name"]]
        if not isinstance(budgets.get(key), int):
            raise Refusal(f"input_missing:operations.budgets.{key}")
        step["deadline_ms"] = budgets[key]
        step["retries"] = 0
    return {"pod": BOOTSTRAP_POD, "restart": "never", "inputs": list(INPUTS), "steps": steps}


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
    ca_ref = serve.database["tls"]["root_cert_ref"]
    logins = {p: role.login for p, role in roles.items()}
    core_init = resolve_vector(ops, "core-init", logins, release)
    core_server = resolve_vector(ops, "core-server", logins, release)
    bootstrap = plan(ops, views, roles, release, config_path, conf_root, core_init, core_server, ca_ref)
    inventory = {
        "operations_sha256": RC0_SHA256["runtime-operations-v1.json"],
        "source_commit": release["source_commit"],
        "serve": {"pod": PRODUCT, "containers": ["core", "api"],
                  "images": {"core": release["core"]["image"], "api": release["api"]["image"]},
                  "secrets": sorted(MODE_PURPOSES["serve"]),
                  "published": [{"host": "127.0.0.1", "port": serve.publish["port"]}]},
        "bootstrap": {"pod": BOOTSTRAP_POD, "steps": [s["name"] for s in bootstrap["steps"]]},
        "ready": {"url": f"http://127.0.0.1:{serve.publish['port']}/health",
                  "fields": list(ops["health"]["c11_fields"]),
                  "source_commit": release["source_commit"]},
        "status_argv": operator_argv("status"),
    }
    return {"bootstrap.plan.json": bootstrap, "inventory.json": inventory}


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


# ---------------------------------------------------------------------------
# S5: the supported ordinary serve lifecycle - start, stop, explicit restart, uninstall.
# Ownership authority is the ONE installer record (install.sh `record_object`, Nix): every
# serve object is recorded with the exact ID its create returned, immediately after that
# create. A name, label, tag or PID never authenticates an object the record does not hold.
# ---------------------------------------------------------------------------
LIFECYCLE_VERBS = ("start", "stop", "restart", "uninstall")
SERVE_NAMES = (PRODUCT, f"{PRODUCT}-core", f"{PRODUCT}-api")
# The finite owner's (S4) completion object in the same record: start/restart need it.
BOOTSTRAP_DONE = {"kind": "bootstrap", "name": BOOTSTRAP_POD}
ENGINE_ID = re.compile(r"[0-9a-f]{64}")


def _serve_creates(plan):
    """The rendered serve step's three creates, exactly as rendered: pod, DB, API."""
    steps = plan.get("steps") if isinstance(plan, dict) else None
    step = next((s for s in steps or () if isinstance(s, dict) and s.get("name") == "serve"), None)
    creates = []
    for argv in (step or {}).get("podman") or ():
        kind = ("pod" if argv[:3] == ["podman", "pod", "create"]
                else "container" if argv[:2] == ["podman", "run"] else None)
        if kind is None or "--name" not in argv:
            raise Refusal("plan_invalid:serve")
        creates.append((kind, argv[argv.index("--name") + 1], argv))
    if [name for _kind, name, _argv in creates] != list(SERVE_NAMES):
        raise Refusal("plan_invalid:serve")
    return creates


def _with_inputs(argv, key_id):
    """Substitute the one explicit operator input; nothing else may be left unrendered."""
    resolved = []
    for arg in argv:
        if arg == KEY_ID:
            if type(key_id) is not str or not key_id:
                raise Refusal("input_missing:key_id")
            arg = key_id
        elif type(arg) is not str:
            raise Refusal("plan_invalid:serve")
        resolved.append(arg)
    return resolved


def _fresh_unlock(fd):
    """A caller-owned anonymous pipe read end that still holds its unread unlock bytes.

    The lifecycle never keeps, duplicates or reopens it: a restart needs a new one."""
    if type(fd) is not int or fd in (0, 1, 2):
        raise Refusal("unlock_required")
    if not anonymous_pipe(fd):
        raise Refusal("unlock_invalid")
    try:
        waiting = int.from_bytes(fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"), sys.byteorder)
    except OSError:
        raise Refusal("unlock_invalid") from None
    if waiting <= 0:
        raise Refusal("unlock_stale")


def _writable(record):
    if getattr(record, "writable", False) is not True:
        raise Refusal("record_owner_unbound")


def _recorded(record):
    """Our serve objects in the record, by name; any other kind or name is not ours to touch."""
    found = {}
    for entry in record.objects():
        if (isinstance(entry, dict) and entry.get("name") in SERVE_NAMES
                and entry.get("kind") in ("pod", "container") and type(entry.get("id")) is str):
            found[entry["name"]] = entry
    return found


def _check_owned(engine, recorded, *, missing_ok):
    """Every recorded object must still be exactly the one we created, else refuse untouched."""
    present = {}
    for name, entry in recorded.items():
        actual = engine.lookup(entry["kind"], name)
        if actual is None and missing_ok:
            continue
        if actual != entry["id"]:
            raise Refusal(f"serve_foreign:{name}" if actual is not None else f"serve_missing:{name}")
        present[name] = entry
    return present


def _remaining(deadline, reserve):
    left = deadline - reserve - time.monotonic()
    if left <= 0:
        raise Refusal("deadline")
    return deadline - reserve


def _inverse(owned, record, engine, deadline):
    """Remove owned objects API -> DB -> pod; forget each only after its removal succeeded.

    Uses what remains of the ORIGINAL deadline (the cleanup reserve), never a new budget. A
    failed removal keeps its record so stop/uninstall can see it; any failure is non-success."""
    failed = False
    for kind, name, ident, recorded in reversed(owned):
        try:
            if time.monotonic() >= deadline:
                raise OSError("deadline")
            engine.remove(kind, ident, deadline=deadline)
        except Exception:
            failed = True
            continue
        if recorded:
            record.forget(name)
    if failed:
        raise Refusal("serve_cleanup_failed")


def _start(creates, record, engine, *, key_id, unlock_fd, deadline, reserve):
    for kind, name, _argv in creates:
        if engine.lookup(kind, name) is not None:
            raise Refusal(f"serve_collision:{name}")
    owned = []
    try:
        for kind, name, argv in creates:
            ident = engine.create(_with_inputs(argv, key_id),
                                  unlock_fd=unlock_fd if name == SERVE_NAMES[-1] else None,
                                  deadline=_remaining(deadline, reserve))
            if type(ident) is not str or not ident:
                raise Refusal("serve_create_unrecognised")
            owned.append([kind, name, ident, False])
            record.record(kind, name, ident)
            owned[-1][3] = True
    except BaseException as error:
        # Cleanup runs on refusal, failure AND interrupt; an interrupt still propagates.
        _inverse(owned, record, engine, deadline)
        if isinstance(error, Refusal) or not isinstance(error, Exception):
            raise
        raise Refusal("serve_start_failed") from None
    return owned


def serve_lifecycle(verb, *, plan, record, engine, deadline, key_id=None, unlock_fd=None):
    """Start, stop, restart or uninstall the rendered DB + API serve pod.

    `record` is the one installer record port (objects/record/forget, `writable`), `engine`
    the container engine port (lookup/create/stop/remove). `deadline` is one absolute
    monotonic deadline; a bounded cleanup reserve of it is kept for failure cleanup. start
    and restart need the finite bootstrap's completion object and a fresh caller unlock
    descriptor; there is no automatic restart and no saved or re-opened key."""
    if verb not in LIFECYCLE_VERBS:
        raise Refusal(f"bad_argument:{verb}")
    if type(deadline) not in (int, float):
        raise Refusal("input_missing:deadline")
    reserve = min(1.0, max(0.0, deadline - time.monotonic()) / 4)
    creates = _serve_creates(plan)
    if verb in ("start", "restart", "uninstall"):
        _writable(record)
    if verb in ("start", "restart"):
        done = [e for e in record.objects() if isinstance(e, dict)
                and {k: e.get(k) for k in BOOTSTRAP_DONE} == BOOTSTRAP_DONE and e.get("id")]
        if not done:
            raise Refusal("bootstrap_incomplete")
        _fresh_unlock(unlock_fd)
    _remaining(deadline, reserve)
    recorded = _recorded(record)
    if verb == "start":
        if recorded:
            raise Refusal("serve_already_recorded")
        owned = _start(creates, record, engine, key_id=key_id, unlock_fd=unlock_fd,
                       deadline=deadline, reserve=reserve)
    elif verb in ("stop", "restart"):
        if set(recorded) != set(SERVE_NAMES):
            raise Refusal("serve_not_recorded")
        present = _check_owned(engine, recorded, missing_ok=False)
        if verb == "stop":
            pod = present[PRODUCT]
            engine.stop("pod", pod["id"], deadline=_remaining(deadline, reserve))
            owned = [[e["kind"], n, e["id"], True] for n, e in present.items()]
        else:
            # Explicit restart: remove what we own, then start fresh; a failed removal stops here.
            _inverse([(present[n]["kind"], n, present[n]["id"], True) for n in SERVE_NAMES],
                     record, engine, deadline)
            owned = _start(creates, record, engine, key_id=key_id, unlock_fd=unlock_fd,
                           deadline=deadline, reserve=reserve)
    else:
        present = _check_owned(engine, recorded, missing_ok=True)
        for name in set(recorded) - set(present):
            record.forget(name)  # already gone: nothing to remove, the stale entry goes
        _inverse([(present[n]["kind"], n, present[n]["id"], True) for n in SERVE_NAMES if n in present],
                 record, engine, deadline)
        owned = []
    objects = [{"kind": kind, "name": name, "id": ident} for kind, name, ident, _r in owned]
    return {"verb": verb, "objects": objects}


class InstallRecord:
    """The one installer record, read-only here: install.sh `record_object` (Nix) is its only
    writer, and it has no supported entry this lifecycle can call yet, so every write refuses
    before any engine effect (`record_owner_unbound`)."""

    writable = False

    def __init__(self, prefix):
        self.path = pathlib.Path(prefix) / "install-record.json"

    def objects(self):
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError:
            raise Refusal("record_unreadable") from None
        doc = decode(raw, limit=MAX_CONFIG_BYTES)
        if not isinstance(doc, dict) or not isinstance(doc.get("objects"), list):
            raise Refusal("record_invalid")
        return doc["objects"]

    def record(self, kind, name, ident):
        raise Refusal("record_owner_unbound")

    def forget(self, name):
        raise Refusal("record_owner_unbound")


class PodmanEngine:
    """The container engine port over the `podman` CLI. Each call is one bounded child; the
    API create receives the caller's unlock descriptor as fd 3 (the rendered --preserve-fds=1)."""

    def __init__(self, program="podman"):
        self.program = program

    def _run(self, args, *, deadline, unlock_fd=None):
        out_read, out_write = os.pipe()
        actions = [(os.POSIX_SPAWN_DUP2, out_write, 1),
                   (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0)]
        if unlock_fd is not None:
            actions.append((os.POSIX_SPAWN_DUP2, unlock_fd, 3))
        try:
            pid = os.posix_spawnp(self.program, [self.program, *args], dict(os.environ),
                                  file_actions=actions)
        finally:
            os.close(out_write)
        output = bytearray()
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([out_read], [], [], left)[0]:
                    os.kill(pid, signal.SIGKILL)
                    raise OSError("engine deadline")
                part = os.read(out_read, 65536)
                if not part:
                    break
                output.extend(part)
                if len(output) > 65536:
                    os.kill(pid, signal.SIGKILL)
                    raise OSError("engine output too large")
        finally:
            os.close(out_read)
            _pid, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status), output.decode("utf-8", "replace")

    def lookup(self, kind, name):
        noun = ["pod"] if kind == "pod" else ["container"]
        code, _out = self._run([*noun, "exists", name], deadline=time.monotonic() + 30)
        if code == 1:
            return None
        if code != 0:
            raise Refusal("engine_unavailable")
        code, out = self._run([*noun, "inspect", "--format", "{{.Id}}", name],
                              deadline=time.monotonic() + 30)
        ident = out.strip()
        if code != 0 or ENGINE_ID.fullmatch(ident) is None:
            raise Refusal("engine_unavailable")
        return ident

    def create(self, argv, *, unlock_fd=None, deadline):
        if argv[:1] != ["podman"]:
            raise Refusal("plan_invalid:serve")
        code, out = self._run(argv[1:], deadline=deadline, unlock_fd=unlock_fd)
        lines = out.strip().splitlines()
        if code != 0 or not lines or ENGINE_ID.fullmatch(lines[-1].strip()) is None:
            raise OSError("engine create failed")
        return lines[-1].strip()

    def stop(self, kind, ident, *, deadline):
        args = ["pod", "stop", ident] if kind == "pod" else ["stop", ident]
        if self._run(args, deadline=deadline)[0] != 0:
            raise OSError("engine stop failed")

    def remove(self, kind, ident, *, deadline):
        args = ["pod", "rm", "--force", ident] if kind == "pod" else ["rm", "--force", ident]
        if self._run(args, deadline=deadline)[0] != 0:
            raise OSError("engine remove failed")


LIFECYCLE_FLAGS = ("--plan", "--prefix", "--unlock-fd", "--key-id", "--seconds")


def lifecycle_main(argv):
    """render.py lifecycle VERB --plan PLAN --prefix DIR --seconds N [--unlock-fd FD --key-id ID]"""
    if not argv or argv[0] not in LIFECYCLE_VERBS:
        raise Refusal(f"bad_argument:{argv[0] if argv else 'lifecycle'}")
    verb, values, pending = argv[0], {}, list(argv[1:])
    while pending:
        flag = pending.pop(0)
        if flag not in LIFECYCLE_FLAGS or flag in values or not pending:
            raise Refusal(f"bad_argument:{flag}")
        values[flag] = pending.pop(0)
    for flag in ("--plan", "--prefix", "--seconds"):
        if flag not in values:
            raise Refusal(f"input_missing:{flag}")
    if not re.fullmatch(r"[1-9][0-9]{0,4}", values["--seconds"]):
        raise Refusal("bad_argument:--seconds")
    unlock = values.get("--unlock-fd")
    if unlock is not None and not re.fullmatch(r"[3-9]|[1-9][0-9]{1,5}", unlock):
        raise Refusal("bad_argument:--unlock-fd")
    plan_doc = decode(read_bytes(values["--plan"], "plan"), limit=MAX_LOCK_BYTES)
    result = serve_lifecycle(verb, plan=plan_doc, record=InstallRecord(values["--prefix"]),
                             engine=PodmanEngine(),
                             deadline=time.monotonic() + int(values["--seconds"]),
                             key_id=values.get("--key-id"),
                             unlock_fd=int(unlock) if unlock is not None else None)
    print(json.dumps(result, sort_keys=True))


def main(argv):
    try:
        if argv[:1] == ["lifecycle"]:
            lifecycle_main(argv[1:])
            return 0
        values = parse_args(argv)
        publish(values["--out"], render(values))
    except Refusal as refusal:
        print(json.dumps({"reason": refusal.reason, "unit": UNIT}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
