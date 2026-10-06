"""Build-once TEST artifacts. Run only on an admitted native CI builder."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
INSTANCE = "cortex_v2_package_test"
TARGETS = {"api": "api", "doc": "worker-doc", "embed": "worker-embed", "graph": "worker-graph", "db": None}


def run(args: list[str], *, read: bool = False) -> str:
    result = subprocess.run(args, check=True, stdout=subprocess.PIPE if read else None, text=True)
    return result.stdout.strip() if read else ""


def stream_digest(stream) -> str:
    checksum = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        checksum.update(chunk)
    return checksum.hexdigest()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return stream_digest(stream)


def source_identity(source_sha: str) -> None:
    # A failed-job rerun can reuse the identity job's version output. Every
    # actual stage checks its own attempt before it can create new bytes.
    if os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("GITHUB_RUN_ATTEMPT") != "1":
        raise RuntimeError("rerun refused; create a new candidate identity")
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise RuntimeError("exact source SHA required")
    if run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], read=True) != source_sha:
        raise RuntimeError("builder HEAD differs from admitted source")
    if run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=normal"], read=True):
        raise RuntimeError("source checkout is dirty; output must be outside it")


def oci_identity(archive: Path, architecture: str = "arm64") -> dict:
    if architecture not in ("arm64", "amd64"):
        raise RuntimeError("closed native image architecture required")
    with tarfile.open(archive, "r") as stream:
        def read(name: str) -> bytes:
            member = stream.getmember(name)
            if not member.isfile() or member.size > 16_000_000:
                raise RuntimeError("invalid OCI metadata member")
            handle = stream.extractfile(member)
            if handle is None:
                raise RuntimeError("missing OCI metadata")
            return handle.read()
        index = json.loads(read("index.json"))
        if len(index.get("manifests", [])) != 1:
            raise RuntimeError("one native image per role archive required")
        manifest_digest = index["manifests"][0]["digest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_digest):
            raise RuntimeError("invalid OCI manifest digest")
        raw = read("blobs/sha256/" + manifest_digest.removeprefix("sha256:"))
        if "sha256:" + hashlib.sha256(raw).hexdigest() != manifest_digest:
            raise RuntimeError("OCI manifest checksum differs")
        manifest = json.loads(raw)
        config_id = manifest["config"]["digest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", config_id):
            raise RuntimeError("invalid OCI config digest")
        raw_config = read("blobs/sha256/" + config_id.removeprefix("sha256:"))
        if "sha256:" + hashlib.sha256(raw_config).hexdigest() != config_id:
            raise RuntimeError("OCI config checksum differs")
        config = json.loads(raw_config)
        if config.get("os") != "linux" or config.get("architecture") != architecture:
            raise RuntimeError("image is not native linux/" + architecture)
        for layer in manifest["layers"]:
            layer_digest = layer["digest"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", layer_digest):
                raise RuntimeError("invalid OCI layer digest")
            member = stream.getmember("blobs/sha256/" + layer_digest.removeprefix("sha256:"))
            if not member.isfile() or member.size != layer["size"]:
                raise RuntimeError("OCI layer length/type differs")
            with stream.extractfile(member) as handle:
                if "sha256:" + stream_digest(handle) != layer_digest:
                    raise RuntimeError("OCI layer checksum differs")
        return {"manifest_digest": manifest_digest, "config_id": config_id,
                "os": "linux", "architecture": architecture,
                "layers": [x["digest"] for x in manifest["layers"]]}


def spdx_document(name: str, identity: str, packages: list[dict]) -> dict:
    for package in packages:
        package.update(licenseConcluded="NOASSERTION", licenseDeclared="NOASSERTION", copyrightText="NOASSERTION")
    return {"spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT",
            "name": name, "documentNamespace": "https://github.com/Kaidera-AI/cortex/spdx/" + identity,
            "creationInfo": {"creators": ["Tool: cortex-build-candidate"],
                             "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
            "packages": packages,
            "relationships": [{"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES",
                               "relatedSpdxElement": p["SPDXID"]} for p in packages],
            "comment": "Installed-package inventory; not a vulnerability or license-acceptance verdict."}


def sbom(image: str, role: str, identity: str, target: str = "macos-arm64") -> dict:
    packages = []
    prefix = ["podman", "--remote=false"] if native_target(target) == "amd64" else ["podman"]
    system = run(prefix + ["run", "--rm", "--pull=never", "--network=none", "--entrypoint=dpkg-query",
                  image, "-W", "-f=${Package}\t${Version}\t${Architecture}\n"], read=True)
    for index, line in enumerate(system.splitlines()):
        name, version, arch = line.split("\t")
        packages.append({"SPDXID": f"SPDXRef-deb-{index}", "name": name, "versionInfo": version,
                         "downloadLocation": "NOASSERTION", "filesAnalyzed": False,
                         "externalRefs": [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl",
                                           "referenceLocator": f"pkg:deb/debian/{quote(name)}@{quote(version)}?arch={quote(arch)}"}]})
    if role != "db":
        code = "import importlib.metadata,json; print(json.dumps(sorted((d.metadata['Name'],d.version) for d in importlib.metadata.distributions())))"
        python_packages = json.loads(run(prefix + ["run", "--rm", "--pull=never", "--network=none",
                                          "--entrypoint=python", image, "-c", code], read=True))
        for index, (name, version) in enumerate(python_packages):
            packages.append({"SPDXID": f"SPDXRef-python-{index}", "name": name, "versionInfo": version,
                             "downloadLocation": "NOASSERTION", "filesAnalyzed": False})
    return spdx_document(f"Cortex TEST {role} installed-package inventory", identity, packages)


def native_target(target: str) -> str:
    if target not in ("macos-arm64", "linux-x86_64"):
        raise RuntimeError("closed native target required")
    return "amd64" if target == "linux-x86_64" else "arm64"


def images(out: Path, source_sha: str, version: str, target: str = "macos-arm64") -> None:
    architecture = native_target(target)
    machines = ("x86_64",) if architecture == "amd64" else ("aarch64", "arm64")
    if platform.system() != "Linux" or platform.machine() not in machines:
        raise RuntimeError("native Linux " + architecture + " builder required; no emulation")
    source_identity(source_sha)
    prefix = ["podman", "--remote=false"] if architecture == "amd64" else ["podman"]
    bases, policy, provider = None, None, None
    if architecture == "amd64":
        from linux_recipe_parity import verify_recipe_parity
        from podman_policy import validate_linuxbrew_provider, validate_local_version
        bases = verify_recipe_parity(ROOT)
        policy = validate_local_version(run(prefix + ["version", "--format", "{{.Client.Version}}"], read=True))
        metadata = json.loads(run(["brew", "info", "--json=v2", "podman"], read=True))
        provider = dict(validate_linuxbrew_provider(metadata, policy['engine_version']), metadata=metadata)
    if out.exists():
        raise RuntimeError("build output already exists; candidate bytes are immutable")
    out.mkdir(parents=True)
    (out / "images").mkdir()
    (out / "sbom").mkdir()
    entries = {}
    for role, role_target in TARGETS.items():
        tag = f"localhost/cortex-v2-test-{role}:{version}"
        command = prefix + ["build", "--format=oci", "--layers", "--label", f"org.opencontainers.image.revision={source_sha}",
                   "--label", f"org.opencontainers.image.version={version}", "--label", "com.kaidera.deployment-class=TEST", "--tag", tag]
        if role_target is None:
            command += ["--file", str(ROOT / ("deploy/release/Containerfile.db.linux-amd64" if architecture == "amd64" else "deploy/release/Containerfile.db"))]
        else:
            command += ["--target", role_target, "--file", str(ROOT / ("deploy/release/Dockerfile.linux-amd64" if architecture == "amd64" else "Dockerfile"))]
        run(command + [str(ROOT)])
        archive = out / "images" / f"{role}.oci.tar"
        run(prefix + ["save", "--format=oci-archive", "--output", str(archive), tag])
        identity = oci_identity(archive, architecture=architecture)
        identity["archive"] = f"images/{role}.oci.tar"
        entries[role] = identity
        (out / "sbom" / f"{role}.spdx.json").write_text(json.dumps(sbom(tag, role, f"{version}-{role}-{identity['manifest_digest'][7:]}", target=target), indent=2) + "\n")
    selected_target = "linux-x86_64" if architecture == "amd64" else "macos-arm64"
    (out / "image-inventory.json").write_text(json.dumps({"source_sha": source_sha, "version": version, "target": selected_target, "images": entries,
            "builder": {"podman": run(prefix + ["version", "--format", "{{.Client.Version}}"], read=True),
                        "system": platform.platform(), "architecture": platform.machine(),
                        "podman_minimum_receipt": policy, "podman_provider": provider,
                        "recipe_parity": bases}}, indent=2) + "\n")

    sys.path.insert(0, str(ROOT / "scripts"))
    from package_rehearsal import rehearse
    receipt = rehearse(entries, source_sha, version, target=selected_target)
    (out / "rehearsal-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


def inspect_host_binary(binary: Path) -> tuple[str, str]:
    """Read architecture and the sole macOS build version independently."""
    if run(["lipo", "-archs", str(binary)], read=True) != "arm64":
        raise RuntimeError("host executable is not the admitted native architecture")
    output = run(["otool", "-l", str(binary)], read=True)
    commands = re.findall(r"^\s*cmd\s+(\S+)\s*$", output, re.MULTILINE)
    if commands.count("LC_BUILD_VERSION") != 1 or any(c.startswith("LC_VERSION_MIN_") for c in commands):
        raise RuntimeError("host executable must have exactly one build version and no legacy minimum")
    blocks = re.split(r"^Load command \d+\s*$", output, flags=re.MULTILINE)
    block = next(b for b in blocks if re.search(r"^\s*cmd\s+LC_BUILD_VERSION\s*$", b, re.MULTILINE))
    fields = {}
    for name in ("platform", "minos", "sdk"):
        values = re.findall(rf"^\s*{name}\s+(\S+)\s*$", block, re.MULTILINE)
        if len(values) != 1:
            raise RuntimeError("host executable has an invalid build version field")
        fields[name] = values[0]
    if fields["platform"] not in ("1", "MACOS", "macos"):
        raise RuntimeError("host executable build version is not macOS")
    if any(not re.fullmatch(r"\d+(?:\.\d+){1,2}", fields[name]) for name in ("minos", "sdk")):
        raise RuntimeError("host executable has an invalid build version number")
    return fields["minos"], fields["sdk"]


def stamp_host_binary_floor(binary: Path) -> None:
    """Narrow the frozen bootloader's floor, restore its signature, prove it."""
    minimum, sdk = inspect_host_binary(binary)
    version = tuple(int(v) for v in minimum.split("."))
    if version + (0,) * (3 - len(version)) > (14, 0, 0):
        raise RuntimeError("host executable minimum cannot be lowered to macOS 14.0")
    stamped = binary.with_name(binary.name + ".floor-stamped")
    if stamped.exists():
        raise RuntimeError("host floor output already exists")
    mode = binary.stat().st_mode & 0o777
    try:
        run(["vtool", "-set-build-version", "macos", "14.0", sdk, "-output", str(stamped), str(binary)])
        stamped.chmod(mode)
        stamped.replace(binary)
    finally:
        stamped.unlink(missing_ok=True)
    run(["codesign", "--force", "--sign", "-", str(binary)])
    run(["codesign", "--verify", "--strict", str(binary)])
    actual_minimum, actual_sdk = inspect_host_binary(binary)
    if actual_minimum != "14.0" or actual_sdk != sdk:
        raise RuntimeError("host executable floor or preserved SDK differs after stamping")
    run([str(binary), "--help"])


def freeze_host_programs(out: Path) -> dict:
    """Freeze both host entrypoints and retain their separate byte inventories."""
    programs = {}
    for name, entrypoint, paths, inventory_name in (
        ("cortex-test", "install_candidate.py", ROOT / "scripts", "host-archive-inventory.txt"),
        ("cortex-agent", "agent_request.py", ROOT / "src", "agent-archive-inventory.txt"),
    ):
        run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--target-arch=arm64", f"--name={name}",
             "--paths", str(paths), "--distpath", str(out / "bin"), "--workpath", str(out / "work" / name),
             "--specpath", str(out / "spec"), str(ROOT / "scripts/release" / entrypoint)])
        binary = out / "bin" / name
        stamp_host_binary_floor(binary)
    # Both entrypoints must survive stamping and signing before any final inventory.
    for name, inventory_name in (("cortex-test", "host-archive-inventory.txt"),
                                 ("cortex-agent", "agent-archive-inventory.txt")):
        binary = out / "bin" / name
        dependencies = run(["otool", "-L", str(binary)], read=True)
        archive = run([sys.executable, "-m", "PyInstaller.utils.cliutils.archive_viewer", "--recursive", "--brief",
                       str(binary)], read=True)
        (out / inventory_name).write_text(archive + "\n")
        programs[name] = {"sha256": digest(binary), "dependencies": dependencies.splitlines(),
                          "archive_inventory": inventory_name}
    for name in ("cortex-projects", "_cortex_api.sh"):
        source = ROOT / "scripts/agent-shims" / name
        shutil.copy2(source, out / "bin" / name)
        (out / "bin" / name).chmod(0o755 if name == "cortex-projects" else 0o644)
    return programs


def run_linux_qualification(args: list[str], *, read: bool = False) -> str:
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith("LD_") and k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    environment.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin", LC_ALL="C")
    result = subprocess.run(args, check=True, stdout=subprocess.PIPE if read else None,
                            text=True, env=environment)
    return result.stdout.strip() if read else ""


def freeze_linux_programs(out: Path) -> dict:
    from linux_binaries import verify_program
    programs = {}
    for name, entrypoint, paths in (("cortex-test", "install_candidate.py", ROOT / "scripts"),
                                    ("cortex-agent", "agent_request.py", ROOT / "src")):
        run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", f"--name={name}",
             "--paths", str(paths), "--distpath", str(out / "bin"), "--workpath", str(out / "work" / name),
             "--specpath", str(out / "spec"), str(ROOT / "scripts/release" / entrypoint)])
        programs[name] = verify_program(out / "bin" / name, run=run_linux_qualification)
    # Only after BOTH ELF closures and entry points pass, publish inventories.
    for name, inventory_name in (("cortex-test", "host-archive-inventory.txt"),
                                 ("cortex-agent", "agent-archive-inventory.txt")):
        archive = run([sys.executable, "-m", "PyInstaller.utils.cliutils.archive_viewer", "--recursive", "--brief",
                       str(out / "bin" / name)], read=True)
        (out / inventory_name).write_text(archive + "\n")
        programs[name]["archive_inventory"] = inventory_name
    for name in ("cortex-projects", "_cortex_api.sh"):
        shutil.copy2(ROOT / "scripts/agent-shims" / name, out / "bin" / name)
        (out / "bin" / name).chmod(0o755 if name == "cortex-projects" else 0o644)
    return programs


def host(out: Path, source_sha: str, version: str, runtime: Path, target: str = "macos-arm64") -> None:
    architecture = native_target(target)
    expected_system, expected_machine = ("Linux", "x86_64") if architecture == "amd64" else ("Darwin", "arm64")
    if platform.system() != expected_system or platform.machine() != expected_machine:
        raise RuntimeError("native " + target + " builder required")
    if platform.python_version() != "3.12.14":
        raise RuntimeError("host freeze requires the pinned CPython 3.12.14 builder")
    source_identity(source_sha)
    runtime = runtime.resolve()
    native = json.loads((runtime / "runtime-inventory.json").read_text())
    bootstrap = "bootstrap-linux-runtime.py" if architecture == "amd64" else "bootstrap-macos-runtime.py"
    lock = ROOT / "scripts/release" / ("requirements-linux-host-build.txt" if architecture == "amd64" else "requirements-host-build.txt")
    expected = json.loads(run([sys.executable, str(ROOT / "scripts/release" / bootstrap), "--describe"], read=True))
    if (native.get("schema") != "cortex.native-ci-runtime.v1" or native.get("inputs") != expected
            or not Path(sys.executable).resolve().is_relative_to(runtime)
            or native.get("runtime", {}).get("python") != platform.python_version()):
        raise RuntimeError("host interpreter does not bind to the pinned native bootstrap")
    if out.exists():
        raise RuntimeError("host output already exists")
    out.mkdir(parents=True)
    run([sys.executable, "-m", "pip", "install", "--require-hashes", "--only-binary=:all:",
         "--requirement", str(lock)])
    programs = freeze_linux_programs(out) if architecture == "amd64" else freeze_host_programs(out)
    (out / "licenses").mkdir()
    for name, expected_digest in native["licenses"].items():
        if name != Path(name).name or digest(runtime / "licenses" / name) != expected_digest:
            raise RuntimeError("native runtime licence differs from bootstrap")
        shutil.copyfile(runtime / "licenses" / name, out / "licenses" / name)
    builder_packages = []
    builder_names = ("altgraph", "packaging", "pyinstaller", "pyinstaller-hooks-contrib", "setuptools")
    if architecture == "arm64":
        builder_names = ("altgraph", "macholib", "packaging", "pyinstaller", "pyinstaller-hooks-contrib", "setuptools")
    for name in builder_names:
        distribution = metadata.distribution(name)
        notices = [f for f in distribution.files or [] if ".dist-info/" in str(f)
                   and ("license" in str(f).lower() or "copying" in str(f).lower())]
        if not notices:
            raise RuntimeError("builder dependency license notice missing")
        for index, notice in enumerate(notices):
            shutil.copyfile(distribution.locate_file(notice), out / "licenses" / f"{name}-{index}-{Path(notice).name}")
        builder_packages.append({"SPDXID": f"SPDXRef-builder-{name}", "name": name,
                                 "versionInfo": distribution.version, "downloadLocation": "NOASSERTION", "filesAnalyzed": False})
    packages = [{"SPDXID": "SPDXRef-python", "name": "CPython", "versionInfo": platform.python_version(),
                 "downloadLocation": "https://www.python.org/", "filesAnalyzed": False},
                {"SPDXID": "SPDXRef-cortex-installer", "name": "Cortex TEST installer", "versionInfo": version,
                 "downloadLocation": f"https://github.com/Kaidera-AI/cortex/tree/{source_sha}", "filesAnalyzed": False},
                {"SPDXID": "SPDXRef-cortex-agent", "name": "Cortex member agent bridge", "versionInfo": version,
                 "downloadLocation": f"https://github.com/Kaidera-AI/cortex/tree/{source_sha}", "filesAnalyzed": False}]
    packages.append({"SPDXID": "SPDXRef-openssl", "name": "OpenSSL", "versionInfo": native["inputs"]["openssl"]["version"],
                     "downloadLocation": native["inputs"]["openssl"]["url"], "filesAnalyzed": False})
    document = spdx_document("Cortex TEST native host inventory", f"{version}-host-{source_sha}", packages)
    document["comment"] = "CPython/installer/agent runtime inventory. Builder dependencies separately recorded; this does not assert all builder libraries are embedded."
    (out / "host.spdx.json").write_text(json.dumps(document, indent=2) + "\n")
    (out / "host-inventory.json").write_text(json.dumps({"source_sha": source_sha, "version": version, "target": target,
                **({"maximum_glibc": "2.35"} if architecture == "amd64" else {"minimum_macos": "14"}), "python": platform.python_version(),
                "dependencies": programs["cortex-test"]["dependencies"], "programs": programs,
                "builder_packages": builder_packages, "native_runtime_bootstrap": native,
                "build_lock_sha256": digest(lock)}, indent=2) + "\n")
    shutil.rmtree(out / "work")
    shutil.rmtree(out / "spec")


def compose_projection(entries: dict) -> str:
    # JSON is valid YAML. Config IDs are immutable local image identities; the
    # source OCI manifest digests are recorded separately without conflation.
    prefix = "${CORTEX_TEST_NAMESPACE:?Use the native installer}"
    common = {"pull_policy": "never", "restart": "no", "networks": ["test"],
              "profiles": ["package-managed"], "cpus": 1,
              "labels": {"com.kaidera.deployment-class": "TEST",
                         "com.kaidera.candidate": "${CORTEX_TEST_INSTALLATION:?Use the native installer}"}}
    def secret(suffix: str, target: str, uid: int = 10001) -> dict:
        return {"source": suffix, "target": target, "uid": str(uid), "gid": str(uid), "mode": 0o400}
    runtime = {"user": "10001:10001", "read_only": True, "init": True,
               "cap_drop": ["ALL"], "security_opt": ["no-new-privileges"],
               "tmpfs": ["/tmp:rw,noexec,nosuid,size=16m,mode=1777"]}
    services = {}
    for role, image_role in (("db", "db"), ("migrate", "api"), ("api", "api"), ("doc", "doc"), ("embed", "embed"), ("graph", "graph")):
        services[role] = dict(common, image=entries[image_role]["config_id"],
                              container_name=f"{prefix}_{role}", mem_limit="512m" if role in ("db", "api") else "256m")
        if role != "db":
            services[role].update(runtime)
            services[role]["environment"] = {"CORTEX_V2_SANDBOX_INSTANCE": INSTANCE}
    db = services["db"]
    db["networks"] = {"test": {"aliases": ["db"]}}
    db["environment"] = {"POSTGRES_USER": "cortex_v2_owner", "POSTGRES_DB": "cortex_v2",
                         "POSTGRES_PASSWORD_FILE": "/run/secrets/db-owner-password"}
    db["volumes"] = ["pgdata:/var/lib/postgresql"]
    db["secrets"] = [secret(s, s, 999) for s in ("db-owner-password", "db-app-password", "db-migrator-password")]
    db["healthcheck"] = {"test": ["CMD", "pg_isready", "-U", "cortex_v2_owner", "-d", "cortex_v2"], "interval": "2s", "timeout": "3s", "retries": 45}
    migrate = services["migrate"]
    migrate["entrypoint"] = ["python", "-m", "cortex_v2.migrate"]
    migrate["environment"].update(CORTEX_V2_MIGRATOR_DATABASE_URL_FILE="/run/secrets/database-url-migrator", CORTEX_V2_FIXTURE_FILE="/run/secrets/fixture")
    migrate["secrets"] = [secret("database-url-migrator", "database-url-migrator"), secret("fixture", "fixture")]
    migrate["depends_on"] = {"db": {"condition": "service_healthy"}}
    for role in ("api", "doc", "embed", "graph"):
        service = services[role]
        service["environment"].update(CORTEX_V2_DATABASE_URL_FILE="/run/secrets/database-url-app", CORTEX_V2_TOKEN_PEPPER_FILE="/run/secrets/token-pepper")
        service["secrets"] = [secret("database-url-app", "database-url-app"), secret("token-pepper", "token-pepper")]
        service["depends_on"] = {"migrate": {"condition": "service_completed_successfully"}}
        if role == "api":
            service["networks"] = {"test": {"aliases": ["api"]}}
            service["ports"] = ["127.0.0.1:${CORTEX_TEST_PORT:?Use the native installer}:8601"]
        else:
            service["environment"].update(CORTEX_V2_WORKER_TOKEN_FILE="/run/secrets/worker-token", CORTEX_V2_WORKER_PRINCIPAL_ID_FILE="/run/secrets/worker-principal-id")
            service["environment"].update(CORTEX_V2_WORKER_ID=f"{prefix}-{role}", CORTEX_V2_INFERENCE_ROUTING_POLICY="disabled")
            if role == "doc":
                service["environment"]["CORTEX_V2_WORKER_EXECUTOR_ROLES"] = "core,doc"
            elif role == "graph":
                service["environment"]["CORTEX_V2_WORKER_HANDLER_MODULES"] = "cortex_v2.retrieval.jobs"
            service["secrets"] += [secret("worker-token", "worker-token"), secret("worker-principal-id", "worker-principal-id")]
    # Full pinned projection for review. External resources and the opt-in
    # profile prevent accidental construction. The native installer owns setup.
    return json.dumps({"name": prefix, "x-cortex-purpose": "image identity inventory; install with cortex-test",
                       "x-cortex-oci-manifest-digests": {k: v["manifest_digest"] for k, v in entries.items()},
                       "services": services,
                       "networks": {"test": {"external": True, "name": f"{prefix}_net"}},
                       "volumes": {"pgdata": {"external": True, "name": f"{prefix}_pgdata"}},
                       "secrets": {s: {"external": True, "name": f"{prefix}-{s}"} for s in
                                   ("db-owner-password", "db-app-password", "db-migrator-password", "database-url-app",
                                    "database-url-migrator", "fixture", "token-pepper", "worker-token", "worker-principal-id")}}, indent=2) + "\n"


def assemble(out: Path, images_dir: Path, host_dir: Path, source_sha: str, version: str, target: str = "macos-arm64") -> None:
    from podman_policy import POLICY
    architecture = native_target(target)
    source_identity(source_sha)
    inventory = json.loads((images_dir / "image-inventory.json").read_text())
    native = json.loads((host_dir / "host-inventory.json").read_text())
    rehearsal = json.loads((images_dir / "rehearsal-receipt.json").read_text())
    if inventory["source_sha"] != source_sha or inventory["version"] != version or native["source_sha"] != source_sha or native["version"] != version:
        raise RuntimeError("builder receipts do not bind to one frozen source/version")
    if architecture == "amd64" and (inventory.get("target") != target or native.get("target") != target
            or rehearsal.get("target") != target or native.get("maximum_glibc") != "2.35"):
        raise RuntimeError("Linux builder receipts do not bind to the selected native target")
    for role, entry in inventory["images"].items():
        actual = oci_identity(images_dir / entry["archive"], architecture=architecture)
        if any(actual[key] != entry.get(key) for key in actual):
            raise RuntimeError("downloaded OCI bytes differ from native builder inventory")
    programs = native.get("programs", {})
    if set(programs) != {"cortex-test", "cortex-agent"}:
        raise RuntimeError("native installer and agent bridge inventories required")
    for name, receipt in programs.items():
        if digest(host_dir / "bin" / name) != receipt.get("sha256"):
            raise RuntimeError("downloaded host binary differs from its builder inventory")
        if architecture == "amd64" and (receipt.get("architecture") != "x86_64" or receipt.get("help") != "PASS"
                or receipt.get("maximum_glibc") != "2.35" or not receipt.get("embedded_elf_count")):
            raise RuntimeError("complete native ELF and help qualification required")
    for name in ("cortex-projects", "_cortex_api.sh"):
        if digest(host_dir / "bin" / name) != digest(ROOT / "scripts/agent-shims" / name):
            raise RuntimeError("downloaded agent shim differs from frozen source")
    if (rehearsal.get("status") != "PASS" or rehearsal.get("source_sha") != source_sha
            or rehearsal.get("version") != version
            or rehearsal.get("image_ids") != {r: i["config_id"] for r, i in inventory["images"].items()}):
        raise RuntimeError("complete migration/network/restart rehearsal missing or mismatched")
    if out.exists():
        raise RuntimeError("assembly output already exists")
    out.mkdir(parents=True)
    package = out / f"cortex-{version}-{target}"
    package.mkdir()
    shutil.copytree(images_dir / "images", package / "images")
    shutil.copytree(images_dir / "sbom", package / "sbom")
    shutil.copy2(images_dir / "rehearsal-receipt.json", package / "rehearsal-receipt.json")
    shutil.copytree(host_dir / "bin", package / "bin")
    # GitHub artifact upload/download does not preserve executable mode.
    for name in ("cortex-test", "cortex-agent", "cortex-projects"):
        (package / "bin" / name).chmod(0o755)
    (package / "bin/_cortex_api.sh").chmod(0o644)
    shutil.copytree(host_dir / "licenses", package / "licenses")
    shutil.copytree(ROOT / "deploy/release/licenses", package / "licenses", dirs_exist_ok=True)
    shutil.copy2(host_dir / "host.spdx.json", package / "sbom/host.spdx.json")
    shutil.copy2(host_dir / "host-archive-inventory.txt", package / "sbom/host-archive-inventory.txt")
    shutil.copy2(host_dir / "agent-archive-inventory.txt", package / "sbom/agent-archive-inventory.txt")
    shutil.copy2(host_dir / "host-inventory.json", package / "sbom/host-inventory.json")
    lock = "requirements-linux-host-build.txt" if architecture == "amd64" else "requirements-host-build.txt"
    guide = "linux-test-candidate.md" if architecture == "amd64" else "macos-test-candidate.md"
    guide_name = "INSTALL-linux.md" if architecture == "amd64" else "INSTALL-macos.md"
    shutil.copy2(ROOT / "scripts/release" / lock, package / "sbom/host-build-lock.txt")
    shutil.copy2(ROOT / "docs/install" / guide, package / guide_name)
    shutil.copy2(ROOT / "LICENSE", package / "LICENSE")
    (package / "compose-images.yaml").write_text(compose_projection(inventory["images"]))
    files = {p.relative_to(package).as_posix(): digest(p) for p in sorted(package.rglob("*")) if p.is_file()}
    manifest = {"schema": "cortex.test-package.v1", "deployment_class": "TEST", "instance": INSTANCE,
                "version": version, "source_sha": source_sha, "target": target,
                **({"maximum_glibc": "2.35"} if architecture == "amd64" else {"minimum_macos": "14"}),
                "podman_policy": POLICY, "images": inventory["images"], "files": files,
                "builder": inventory["builder"], "qualification": "NOT_INSTALLED; source admission recorded separately; literal local smoke required",
                "signature": "unsigned TEST; not a signed customer release"}
    (package / "release.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    sums = dict(files, **{"release.json": digest(package / "release.json")})
    (package / "SHA256SUMS").write_text("".join(f"{value}  {name}\n" for name, value in sorted(sums.items())))
    archive = out / (package.name + ".tar.gz")
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(package, arcname=package.name)
    (out / "SHA256SUMS").write_text(f"{digest(archive)}  {archive.name}\n")
    print(json.dumps({"archive": str(archive), "sha256": digest(archive), "source_sha": source_sha, "version": version}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("macos-arm64", "linux-x86_64"), default="macos-arm64")
    parser.add_argument("stage", choices=("images", "host", "assemble"))
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--images", type=Path)
    parser.add_argument("--host", type=Path)
    parser.add_argument("--runtime", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"0\.2\.001-test\.[0-9]{8}\.[1-9][0-9]*", args.version):
        parser.error("explicit immutable TEST version required")
    args.output = args.output.resolve()
    if args.output == ROOT or ROOT in args.output.parents:
        parser.error("output must be outside the source checkout")
    if args.stage == "images":
        images(args.output, args.source_sha, args.version, target=args.target)
    elif args.stage == "host":
        if args.runtime is None:
            parser.error("host requires its compiled --runtime prefix")
        host(args.output, args.source_sha, args.version, args.runtime, target=args.target)
    else:
        if args.images is None or args.host is None:
            parser.error("assemble requires --images and --host")
        assemble(args.output, args.images, args.host, args.source_sha, args.version, target=args.target)


if __name__ == "__main__":
    main()
