"""Native TEST installer; freeze with PyInstaller on macOS arm64.

No target Python/provider install, source mount, live endpoint or automatic
removal. Subprocess output is retained only for explicitly public projections.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

from prepare_sandbox import PACKAGE_TEST_INSTANCE, prepare

INSTANCE = PACKAGE_TEST_INSTANCE
LABEL = "com.kaidera.candidate"
ROLES = ("db", "api", "doc", "embed", "graph")
SUFFIXES = {
    "db-owner-password": "db-owner-password",
    "db-app-password": "db-app-password",
    "db-migrator-password": "db-migrator-password",
    "database-url-app": "database-url-app",
    "database-url-migrator": "database-url-migrator",
    "token-pepper": "token-pepper",
    "fixture.json": "fixture",
    "owner.token": "owner-token",
    "worker.token": "worker-token",
    "recovery.token": "recovery-token",
    "worker-principal-id": "worker-principal-id",
}


class Refusal(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refusal("TEST endpoint redirect refused")


HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def public_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
        raise Refusal("public metadata missing, linked or too large")
    try:
        value = json.loads(path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise Refusal("invalid public metadata") from exc
    if not isinstance(value, dict):
        raise Refusal("metadata must be an object")
    return value


def verify_package(package: Path) -> dict:
    if (not package.is_absolute() or ".." in package.parts or not package.is_dir()
            or any(p.is_symlink() for p in (package, *package.parents))):
        raise Refusal("package must be an absolute, unlinked directory")
    manifest = public_json(package / "release.json")
    if (manifest.get("schema") != "cortex.test-package.v1"
            or manifest.get("deployment_class") != "TEST"
            or manifest.get("instance") != INSTANCE
            or manifest.get("target") != "macos-arm64"
            or not re.fullmatch(r"0\.2\.001-test\.[0-9]{8}\.[1-9][0-9]*", manifest.get("version", ""))
            or not re.fullmatch(r"[0-9a-f]{40}", manifest.get("source_sha", ""))):
        raise Refusal("unsupported package identity/platform/class")
    files = manifest.get("files")
    mandatory = {"bin/cortex-test", "INSTALL-macos.md", "LICENSE", "compose-images.yaml", "rehearsal-receipt.json",
                 "sbom/host-inventory.json", "sbom/host-build-lock.txt", "sbom/host.spdx.json",
                 "sbom/host-archive-inventory.txt", "licenses/Python-3.12.14-LICENSE.txt"}
    mandatory.update(f"sbom/{role}.spdx.json" for role in ROLES)
    if not isinstance(files, dict) or not mandatory.issubset(files):
        raise Refusal("empty file inventory")
    actual = set()
    for path in package.rglob("*"):
        if path.is_symlink():
            raise Refusal("package contains a symlink")
        if path.is_file():
            actual.add(path.relative_to(package).as_posix())
    if actual != set(files) | {"release.json", "SHA256SUMS"}:
        raise Refusal("package file inventory differs")
    for name, digest in files.items():
        part = Path(name)
        if part.is_absolute() or ".." in part.parts:
            raise Refusal("unsafe inventory path")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise Refusal("invalid inventory digest")
        if sha256(package / part) != digest:
            raise Refusal("package file checksum mismatch")
    expected_sums = dict(files, **{"release.json": sha256(package / "release.json")})
    sums = (package / "SHA256SUMS").read_text().splitlines()
    if sums != [f"{digest}  {name}" for name, digest in sorted(expected_sums.items())]:
        raise Refusal("checksum list differs from manifest")
    images = manifest.get("images", {})
    if not isinstance(images, dict) or set(images) != set(ROLES):
        raise Refusal("package image role set differs")
    for role in ROLES:
        image = images[role]
        if (not isinstance(image, dict) or image.get("os") != "linux" or image.get("architecture") != "arm64"
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("config_id", ""))
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("manifest_digest", ""))
                or image.get("archive") != f"images/{role}.oci.tar"
                or image["archive"] not in files):
            raise Refusal("invalid platform/image identity")
    if not os.access(package / "bin/cortex-test", os.X_OK):
        raise Refusal("packaged installer is not executable")
    rehearsal = public_json(package / "rehearsal-receipt.json")
    if (rehearsal.get("status") != "PASS" or rehearsal.get("source_sha") != manifest["source_sha"]
            or rehearsal.get("version") != manifest["version"]
            or rehearsal.get("image_ids") != {r: i["config_id"] for r, i in images.items()}):
        raise Refusal("package rehearsal receipt missing or mismatched")
    return manifest


def private_root(root: Path, *, create: bool = False) -> None:
    if not root.is_absolute() or ".." in root.parts:
        raise Refusal("root must be an absolute path")
    home = Path.home().resolve()
    try:
        root.relative_to(home / ".cortex" / "test")
    except ValueError as exc:
        raise Refusal("TEST runtime must be under the user's private .cortex/test folder") from exc
    if root == home / ".cortex" / "test":
        raise Refusal("choose a candidate subfolder beneath .cortex/test")
    for parent in (root, *root.parents):
        if parent.is_symlink():
            raise Refusal("linked TEST root or ancestor")
    probe = root
    while not probe.exists():
        probe = probe.parent
    if platform.system() == "Darwin":
        # diskutil accepts a device/mount point, not an arbitrary directory.
        device = subprocess.run(["df", "-P", str(probe)], capture_output=True, text=True, timeout=20)
        lines = device.stdout.splitlines()
        filesystem = lines[-1].split()[0] if len(lines) == 2 else ""
        if device.returncode or not re.fullmatch(r"/dev/disk[0-9]+s[0-9]+(?:s[0-9]+)?", filesystem):
            raise Refusal("cannot establish private filesystem device")
        check = subprocess.run(["diskutil", "info", "-plist", filesystem], capture_output=True, timeout=20)
        import plistlib
        try:
            volume = plistlib.loads(check.stdout)
        except (ValueError, plistlib.InvalidFileException) as exc:
            raise Refusal("cannot establish private volume custody") from exc
        if (check.returncode or volume.get("GlobalPermissionsEnabled") is not True
                or volume.get("Internal") is not True):
            raise Refusal("private runtime requires an internal volume with ownership enabled")
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise Refusal("TEST root must be user-owned mode 0700")


def private_bytes(path: Path, *, limit: int = 65536) -> bytes:
    for parent in (path.parent, path.parent.parent):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise Refusal("private state directory ownership/mode differs")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise Refusal("private credential ownership/mode differs")
        value = stream.read(limit + 1)
        if not value or len(value) > limit:
            raise Refusal("private credential size invalid")
        return value


@contextmanager
def namespace_lock():
    # Serialize install/erase operations, including shared immutable image IDs.
    path = Path.home() / ".cortex-test-namespace.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        info = os.fstat(lock.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise Refusal("TEST operator lock ownership/mode differs")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refusal("another TEST lifecycle operation holds the namespace") from exc
        yield


def write_record(root: Path, record: dict) -> None:
    tmp = root / "install.json.new"
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, root / "install.json")


class Podman:
    def __init__(self, connection: str):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", connection):
            raise Refusal("explicit Podman connection name required")
        executable = shutil.which("podman")
        if not executable:
            raise Refusal("install the guide's Podman prerequisite first")
        self.prefix = [executable, "--connection", connection]

    def run(self, args: list[str], *, read: bool = False, input_data: bytes | None = None,
            timeout: int = 90, allowed: tuple[int, ...] = (0,)) -> str:
        try:
            result = subprocess.run(self.prefix + args, input=input_data,
                                    stdout=subprocess.PIPE if read else subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Refusal(f"Podman {args[0]} unavailable or timed out; no cleanup performed") from exc
        if result.returncode not in allowed:
            raise Refusal(f"Podman {args[0]} refused; no cleanup performed")
        return result.stdout.decode().strip() if read else str(result.returncode)

    def exists(self, kind: str, name: str) -> bool:
        return self.run([kind, "exists", name], allowed=(0, 1)) == "0"

    def preflight(self) -> None:
        versions = self.run(["version", "--format", "{{.Client.Version}} {{.Server.Version}}"], read=True)
        if len(versions.split()) != 2 or any(not re.fullmatch(r"6\.0\.[0-9]+", x) for x in versions.split()):
            raise Refusal("package requires Podman client/server 6.0.x; see guide")
        arch = self.run(["info", "--format", "{{.Host.Arch}}"], read=True)
        if arch not in ("arm64", "aarch64"):
            raise Refusal("Podman machine is not arm64")
        if self.run(["info", "--format", "{{.Host.Security.Rootless}}"], read=True) != "true":
            raise Refusal("TEST package requires a rootless Podman connection")

    def image(self, entry: dict) -> None:
        actual = self.run(["image", "inspect", "--format", "{{.Id}} {{.Os}} {{.Architecture}}", entry["config_id"]], read=True)
        if actual != f'{entry["config_id"]} linux arm64':
            raise Refusal("loaded image identity/platform differs")


def namespace(record: dict) -> str:
    installation = str(uuid.UUID(record["installation"]))
    if installation != record["installation"]:
        raise Refusal("invalid installation owner identity")
    return INSTANCE + "_" + uuid.UUID(installation).hex


def names(record: dict) -> list[tuple[str, str]]:
    prefix = namespace(record)
    return [("container", f"{prefix}_{r}") for r in (*ROLES, "migrate")] + [
        ("network", f"{prefix}_net"), ("volume", f"{prefix}_pgdata")
    ] + [("secret", f"{prefix}-{s}") for s in SUFFIXES.values()]


def assert_owner(engine: Podman, kind: str, name: str, installation: str) -> None:
    template = f'{{{{index .Labels "{LABEL}"}}}}'
    if kind == "container":
        template = f'{{{{index .Config.Labels "{LABEL}"}}}}'
    elif kind == "secret":
        template = f'{{{{index .Spec.Labels "{LABEL}"}}}}'
    actual = engine.run([kind, "inspect", "--format", template, name], read=True)
    if actual != installation:
        raise Refusal("candidate object ownership differs; left untouched")


def verify_port(port: int) -> None:
    if port in (8501, 5499, 5500) or not 1024 <= port <= 65535:
        raise Refusal("port is protected or outside the TEST range")
    with socket.socket() as stream:
        try:
            stream.bind(("127.0.0.1", port))
        except OSError as exc:
            raise Refusal("TEST loopback port already in use") from exc


def secret_args(record: dict, suffix: str, target: str, uid: int = 10001) -> list[str]:
    return ["--secret", f"{namespace(record)}-{suffix},target={target},uid={uid},gid={uid},mode=0400"]


def container_args(role: str, record: dict) -> list[str]:
    args = ["run", "--pull=never", "--name", f"{namespace(record)}_{role}", "--label",
            f'{LABEL}={record["installation"]}', "--label", "com.kaidera.deployment-class=TEST",
            "--network", f"{namespace(record)}_net", "--memory", "512m" if role in ("db", "api") else "256m",
            "--cpus", "1", "--restart=no"]
    if role != "db":
        args += ["--user", "10001:10001", "--read-only", "--init", "--cap-drop=ALL",
                 "--security-opt=no-new-privileges", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m,mode=1777",
                 "--env", f"CORTEX_V2_SANDBOX_INSTANCE={INSTANCE}"]
    return args


def ready(engine: Podman, record: dict) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with HTTP.open(f'http://127.0.0.1:{record["port"]}/health/ready', timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(1)
    raise Refusal("TEST API readiness timed out; candidate preserved")


def start_stack(engine: Podman, manifest: dict, record: dict, root: Path) -> None:
    verify_port(record["port"])
    for role in ROLES:
        engine.image(manifest["images"][role])
    for kind, name in names(record):
        if not engine.exists(kind, name):
            raise Refusal("candidate object missing; no repair/removal attempted")
        assert_owner(engine, kind, name, record["installation"])
    engine.run(["start", f"{namespace(record)}_db"])
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = engine.run(["exec", f"{namespace(record)}_db", "pg_isready", "-U", "cortex_v2_owner", "-d", "cortex_v2"], allowed=(0, 1, 2))
        if result == "0":
            break
        time.sleep(1)
    else:
        raise Refusal("TEST database did not become ready")
    # A finite container is retained as the migration receipt. Rerun only via
    # `start --attach`, which executes the immutable checksum-verifying migrator.
    output = engine.run(["start", "--attach", f"{namespace(record)}_migrate"], read=True, timeout=180)
    exit_code = engine.run(["container", "inspect", "--format", "{{.State.ExitCode}}", f"{namespace(record)}_migrate"], read=True)
    if exit_code != "0":
        raise Refusal("finite migration failed; API/workers not started")
    try:
        migration = json.loads(output)
    except ValueError as exc:
        raise Refusal("migration receipt is not JSON") from exc
    if migration.get("instance") != INSTANCE or migration.get("fixture") != "verified":
        raise Refusal("migration did not verify this TEST fixture")
    (root / "migration-receipt.json").write_text(json.dumps(migration, indent=2) + "\n")
    for role in ROLES[1:]:
        engine.run(["start", f"{namespace(record)}_{role}"])
    ready(engine, record)
    running(engine, record)


def running(engine: Podman, record: dict) -> None:
    for role in ROLES:
        state = engine.run(["container", "inspect", "--format", "{{.State.Status}}", f"{namespace(record)}_{role}"], read=True)
        if state != "running":
            raise Refusal("TEST role is not running; candidate preserved")


def install(args: argparse.Namespace, package: Path, manifest: dict, engine: Podman) -> None:
    root = args.root
    if root.exists() or root.is_symlink():
        raise Refusal("TEST root already exists; use its lifecycle command")
    record = {"schema": "cortex.test-install.v1", "version": manifest["version"],
              "source_sha": manifest["source_sha"], "installation": str(uuid.uuid4()),
              "connection": args.connection, "port": args.port, "stage": "staged",
              "loaded_images": []}
    record["namespace"] = namespace(record)
    record["service_label"] = "ai.kaidera.cortex.TEST-v2." + uuid.UUID(record["installation"]).hex
    verify_port(args.port)
    for kind, name in names(record):
        if engine.exists(kind, name):
            raise Refusal("TEST namespace already exists; existing objects retained")
    private_root(root, create=True)
    # Identity must survive a failed package copy; erase needs no intact package.
    write_record(root, record)
    shutil.copytree(package, root / "package")
    subprocess.run(["tmutil", "addexclusion", str(root)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    excluded = subprocess.run(["tmutil", "isexcluded", str(root)], capture_output=True, text=True, check=True)
    if not excluded.stdout.startswith("[Excluded]"):
        raise Refusal("private TEST root is not excluded from Time Machine")
    prepare(root / "state", INSTANCE)
    for role in ROLES:
        image_id = manifest["images"][role]["config_id"]
        if not engine.exists("image", image_id) and image_id not in record["loaded_images"]:
            record["loaded_images"].append(image_id)
            write_record(root, record)  # Record load intent before a possible timeout.
        engine.run(["load", "--input", str(package / manifest["images"][role]["archive"])], timeout=300)
        engine.image(manifest["images"][role])
    provision(engine, manifest, root, record)
    start_stack(engine, manifest, record, root)
    record["stage"] = "ready"
    write_record(root, record)
    print(json.dumps({"status": "TEST ready", "version": manifest["version"], "source_sha": manifest["source_sha"],
                      "root": str(root), "url": f'http://127.0.0.1:{record["port"]}', "service_label": record["service_label"]}))



def provision(engine: Podman, manifest: dict, root: Path, record: dict) -> None:
    label = ["--label", f'{LABEL}={record["installation"]}']
    engine.run(["network", "create", "--internal", *label, f"{namespace(record)}_net"])
    engine.run(["volume", "create", *label, f"{namespace(record)}_pgdata"])
    for source, suffix in SUFFIXES.items():
        path = root / "state" / "secrets" / source
        engine.run(["secret", "create", *label, f"{namespace(record)}-{suffix}", "-"], input_data=private_bytes(path))
    # Create first, then start DB -> finite migration -> API/workers. This also
    # records all intended objects before any service admits work.
    db = container_args("db", record)
    db[0] = "create"
    db += ["--network-alias=db", "--env", "POSTGRES_USER=cortex_v2_owner", "--env", "POSTGRES_DB=cortex_v2",
           "--env", "POSTGRES_PASSWORD_FILE=/run/secrets/db-owner-password", "--volume", f"{namespace(record)}_pgdata:/var/lib/postgresql"]
    for suffix in ("db-owner-password", "db-app-password", "db-migrator-password"):
        db += secret_args(record, suffix, suffix, 999)
    engine.run(db + [manifest["images"]["db"]["config_id"]])
    migrate = container_args("migrate", record)
    migrate[0] = "create"
    migrate += ["--entrypoint=python", "--env", "CORTEX_V2_MIGRATOR_DATABASE_URL_FILE=/run/secrets/database-url-migrator",
                "--env", "CORTEX_V2_FIXTURE_FILE=/run/secrets/fixture"]
    migrate += secret_args(record, "database-url-migrator", "database-url-migrator") + secret_args(record, "fixture", "fixture")
    engine.run(migrate + [manifest["images"]["api"]["config_id"], "-m", "cortex_v2.migrate"])
    for role in ROLES[1:]:
        command = container_args(role, record)
        command[0] = "create"
        command += ["--env", "CORTEX_V2_DATABASE_URL_FILE=/run/secrets/database-url-app",
                    "--env", "CORTEX_V2_TOKEN_PEPPER_FILE=/run/secrets/token-pepper"]
        command += secret_args(record, "database-url-app", "database-url-app") + secret_args(record, "token-pepper", "token-pepper")
        if role == "api":
            command += ["--network-alias=api", "--publish", f'127.0.0.1:{record["port"]}:8601']
        else:
            command += ["--env", "CORTEX_V2_WORKER_TOKEN_FILE=/run/secrets/worker-token",
                        "--env", "CORTEX_V2_WORKER_PRINCIPAL_ID_FILE=/run/secrets/worker-principal-id",
                        "--env", f"CORTEX_V2_WORKER_ID={namespace(record)}-{role}",
                        "--env", "CORTEX_V2_INFERENCE_ROUTING_POLICY=disabled"]
            if role == "doc":
                command += ["--env", "CORTEX_V2_WORKER_EXECUTOR_ROLES=core,doc"]
            elif role == "graph":
                command += ["--env", "CORTEX_V2_WORKER_HANDLER_MODULES=cortex_v2.retrieval.jobs"]
            command += secret_args(record, "worker-token", "worker-token") + secret_args(record, "worker-principal-id", "worker-principal-id")
        engine.run(command + [manifest["images"][role]["config_id"]])
    record["stage"] = "created"
    write_record(root, record)


def smoke(root: Path, record: dict) -> None:
    token = private_bytes(root / "state" / "secrets" / "worker.token", limit=4096).decode().strip()
    base = f'http://127.0.0.1:{record["port"]}'
    headers = {"Authorization": f"Bearer {token}", "X-Cortex-Scope": "sandbox-project", "Content-Type": "application/json"}
    def request(path: str, *, method: str = "GET", data: dict | None = None, auth: bool = True):
        request_headers = dict(headers) if auth else {}
        if data is not None:
            request_headers["Idempotency-Key"] = "package-smoke-" + str(uuid.uuid4())
        req = urllib.request.Request(base + path, method=method, headers=request_headers,
                                     data=json.dumps(data).encode() if data is not None else None)
        try:
            with HTTP.open(req, timeout=10) as response:
                return response.status, json.loads(response.read(1_000_000))
        except urllib.error.HTTPError as exc:
            return exc.code, {}
    status, _ = request("/projects", auth=False)
    if status != 401:
        raise Refusal("smoke: unauthenticated registry read was not refused")
    body = "Cortex package-only scratch round trip " + str(uuid.uuid4())
    status, value = request("/v1/memory/records", method="POST", data={"record_type": "knowledge", "body": body})
    if status != 201:
        raise Refusal("smoke: synthetic record create failed")
    record_id = value.get("data", {}).get("record_id")
    try:
        record_id = str(uuid.UUID(record_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise Refusal("smoke: record id missing") from exc
    status, value = request("/v1/memory/records/" + record_id)
    if status != 200 or value.get("data", {}).get("body") != body:
        raise Refusal("smoke: synthetic record read differed")
    receipt = {"status": "PASS", "scope": "TEST package smoke only", "version": record["version"],
               "source_sha": record["source_sha"], "checks": ["readiness", "unauthenticated refusal", "synthetic write/read"],
               "record_id": record_id, "epoch": int(time.time())}
    (root / "smoke-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


def uninstall(engine: Podman, root: Path, record: dict) -> None:
    # Default retention: pgdata, credentials, image bytes and installed package.
    containers = [f"{namespace(record)}_{role}" for role in (*ROLES, "migrate")]
    for name in containers:
        if engine.exists("container", name):
            assert_owner(engine, "container", name, record["installation"])
            state = engine.run(["container", "inspect", "--format", "{{.State.Status}}", name], read=True)
            if state not in ("exited", "stopped", "created", "configured"):
                raise Refusal("stop all TEST containers before uninstalling")
    for name in containers:
        if engine.exists("container", name):
            assert_owner(engine, "container", name, record["installation"])
            engine.run(["container", "rm", name])
    network = f"{namespace(record)}_net"
    if engine.exists("network", network):
        assert_owner(engine, "network", network, record["installation"])
        engine.run(["network", "rm", network])  # No force; any foreign attachment refuses.
    record["stage"] = "uninstalled"
    write_record(root, record)
    print("TEST containers/network removed; data, credentials, images and package retained")


def validate_record(record: dict) -> None:
    if (record.get("schema") != "cortex.test-install.v1"
            or record.get("stage") not in ("staged", "created", "ready", "uninstalled", "erasing")
            or not re.fullmatch(r"[0-9a-f]{40}", record.get("source_sha", ""))
            or not re.fullmatch(r"0\.2\.001-test\.[0-9]{8}\.[1-9][0-9]*", record.get("version", ""))
            or record.get("namespace") != namespace(record)):
        raise Refusal("unsupported installation record")
    port = record.get("port")
    if type(port) is not int or port in (8501, 5499, 5500) or not 1024 <= port <= 65535:
        raise Refusal("installed port invalid or protected")
    image_ids = record.get("loaded_images")
    if (not isinstance(image_ids, list) or len(image_ids) > len(ROLES)
            or len(set(image_ids)) != len(image_ids)
            or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", x) for x in image_ids)):
        raise Refusal("invalid loaded-image inventory")


def image_owner(engine: Podman, image_id: str, record: dict) -> None:
    template = '{{index .Labels "com.kaidera.deployment-class"}} {{index .Labels "org.opencontainers.image.revision"}} {{index .Labels "org.opencontainers.image.version"}}'
    if engine.run(["image", "inspect", "--format", template, image_id], read=True) != f'TEST {record["source_sha"]} {record["version"]}':
        raise Refusal("loaded image ownership differs; erasure refused")


def erase(engine: Podman, root: Path, record: dict, confirmation: str) -> dict:
    private_root(root)
    validate_record(record)
    if confirmation != record["installation"]:
        raise Refusal("erase requires --confirm with the exact installation ID")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise Refusal("safe TEST-root erasure is unavailable on this platform")
    # Check the complete existing set before stopping/removing any object.
    for kind, name in names(record):
        if engine.exists(kind, name):
            assert_owner(engine, kind, name, record["installation"])
    for image_id in record["loaded_images"]:
        if engine.exists("image", image_id):
            image_owner(engine, image_id, record)
    record["stage"] = "erasing"
    write_record(root, record)
    removed, retained_images = [], []
    for kind, name in names(record):
        if not engine.exists(kind, name):
            continue
        assert_owner(engine, kind, name, record["installation"])
        if kind == "container":
            state = engine.run(["container", "inspect", "--format", "{{.State.Status}}", name], read=True)
            if state in ("running", "paused", "restarting", "stopping"):
                if state == "paused":
                    engine.run(["unpause", name])
                engine.run(["stop", "--time=30", name])
        engine.run([kind, "rm", name])  # No force, including volume/network/image.
        removed.append({"kind": kind, "name": name})
    for image_id in record["loaded_images"]:
        if not engine.exists("image", image_id):
            continue
        image_owner(engine, image_id, record)
        references = engine.run(["ps", "--all", "--filter", "ancestor=" + image_id, "--format", "{{.ID}}"], read=True)
        if references:
            retained_images.append(image_id)
            continue
        engine.run(["image", "rm", image_id])
        removed.append({"kind": "image", "name": image_id})
    private_root(root)
    receipt = {"status": "erased", "installation": record["installation"], "namespace": namespace(record),
               "version": record["version"], "source_sha": record["source_sha"], "removed": removed,
               "retained_shared_images": retained_images, "root": str(root)}
    shutil.rmtree(root)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Cortex v2 package-only TEST installer/lifecycle")
    parser.add_argument("command", choices=("verify", "install", "start", "stop", "status", "smoke", "uninstall", "erase"))
    parser.add_argument("--package", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--connection")
    parser.add_argument("--port", type=int, default=18601)
    parser.add_argument("--confirm", help="exact installation ID; required for erase")
    args = parser.parse_args()
    try:
        if platform.system() != "Darwin" or platform.machine() != "arm64" or os.getuid() == 0:
            raise Refusal("this TEST installer requires a non-root macOS arm64 user")
        if int(platform.mac_ver()[0].split(".")[0]) < 14:
            raise Refusal("this TEST installer requires macOS 14 or newer")
        if args.command in ("verify", "install"):
            if args.package is None or not args.package.is_absolute():
                raise Refusal("absolute extracted --package directory required")
            manifest = verify_package(args.package)
            if args.command == "verify":
                print(json.dumps({"status": "verified", "version": manifest["version"], "source_sha": manifest["source_sha"]}))
                return 0
            if not args.connection:
                raise Refusal("--connection required for installation")
            engine = Podman(args.connection)
            engine.preflight()
            with namespace_lock():
                install(args, args.package, manifest, engine)
            return 0
        private_root(args.root)
        with namespace_lock():
            install_info = (args.root / "install.json").lstat()
            if (not stat.S_ISREG(install_info.st_mode) or install_info.st_uid != os.getuid()
                    or stat.S_IMODE(install_info.st_mode) != 0o600 or install_info.st_nlink != 1):
                raise Refusal("installation record ownership/mode differs")
            record = public_json(args.root / "install.json")
            validate_record(record)
            engine = Podman(record["connection"])
            engine.preflight()
            if args.command == "erase":
                print(json.dumps(erase(engine, args.root, record, args.confirm)))
                return 0
            if args.command in ("start", "smoke"):
                manifest = verify_package(args.root / "package")
                if record.get("source_sha") != manifest["source_sha"] or record.get("version") != manifest["version"]:
                    raise Refusal("installed record/package differs")
            for kind, name in names(record):
                if engine.exists(kind, name):
                    assert_owner(engine, kind, name, record["installation"])
            if args.command == "start":
                start_stack(engine, manifest, record, args.root)
                record["stage"] = "ready"
                write_record(args.root, record)
                print("TEST package started; existing state preserved")
            elif args.command == "stop":
                for role in (*reversed(ROLES[1:]), "migrate", "db"):
                    if engine.exists("container", f"{namespace(record)}_{role}"):
                        engine.run(["stop", "--time=30", f"{namespace(record)}_{role}"])
                print("TEST containers stopped; data, credentials and images retained")
            elif args.command == "smoke":
                ready(engine, record)
                running(engine, record)
                smoke(args.root, record)
            elif args.command == "uninstall":
                uninstall(engine, args.root, record)
            else:
                result = {}
                for role in (*ROLES, "migrate"):
                    name = f"{namespace(record)}_{role}"
                    result[role] = engine.run(["container", "inspect", "--format", "{{.State.Status}}", name], read=True) if engine.exists("container", name) else "absent"
                print(json.dumps({"version": record["version"], "source_sha": record["source_sha"],
                                  "installation": record["installation"], "namespace": namespace(record),
                                  "stage": record["stage"], "loaded_images": record["loaded_images"], "TEST": result}))
        return 0
    except (RuntimeError, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        # OS/provider details may include private bytes; preserve only safe class.
        message = str(exc) if isinstance(exc, Refusal) else type(exc).__name__
        print("Cortex TEST refused: " + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
