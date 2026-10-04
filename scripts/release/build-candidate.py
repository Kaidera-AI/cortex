"""Build-once TEST artifacts. Run only on an admitted native CI builder."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
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


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_identity(source_sha: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise RuntimeError("exact source SHA required")
    if run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], read=True) != source_sha:
        raise RuntimeError("builder HEAD differs from admitted source")
    if run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=normal"], read=True):
        raise RuntimeError("source checkout is dirty; output must be outside it")


def oci_identity(archive: Path) -> dict:
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
        if config.get("os") != "linux" or config.get("architecture") != "arm64":
            raise RuntimeError("image is not native linux/arm64")
        for layer in manifest["layers"]:
            layer_digest = layer["digest"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", layer_digest):
                raise RuntimeError("invalid OCI layer digest")
            member = stream.getmember("blobs/sha256/" + layer_digest.removeprefix("sha256:"))
            if not member.isfile() or member.size != layer["size"]:
                raise RuntimeError("OCI layer length/type differs")
            with stream.extractfile(member) as handle:
                if "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest() != layer_digest:
                    raise RuntimeError("OCI layer checksum differs")
        return {"manifest_digest": manifest_digest, "config_id": config_id,
                "os": "linux", "architecture": "arm64",
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


def sbom(image: str, role: str, identity: str) -> dict:
    packages = []
    system = run(["podman", "run", "--rm", "--pull=never", "--network=none", "--entrypoint=dpkg-query",
                  image, "-W", "-f=${Package}\t${Version}\t${Architecture}\n"], read=True)
    for index, line in enumerate(system.splitlines()):
        name, version, arch = line.split("\t")
        packages.append({"SPDXID": f"SPDXRef-deb-{index}", "name": name, "versionInfo": version,
                         "downloadLocation": "NOASSERTION", "filesAnalyzed": False,
                         "externalRefs": [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl",
                                           "referenceLocator": f"pkg:deb/debian/{quote(name)}@{quote(version)}?arch={quote(arch)}"}]})
    if role != "db":
        code = "import importlib.metadata,json; print(json.dumps(sorted((d.metadata['Name'],d.version) for d in importlib.metadata.distributions())))"
        python_packages = json.loads(run(["podman", "run", "--rm", "--pull=never", "--network=none",
                                          "--entrypoint=python", image, "-c", code], read=True))
        for index, (name, version) in enumerate(python_packages):
            packages.append({"SPDXID": f"SPDXRef-python-{index}", "name": name, "versionInfo": version,
                             "downloadLocation": "NOASSERTION", "filesAnalyzed": False})
    return spdx_document(f"Cortex TEST {role} installed-package inventory", identity, packages)


def images(out: Path, source_sha: str, version: str) -> None:
    if platform.system() != "Linux" or platform.machine() not in ("aarch64", "arm64"):
        raise RuntimeError("native Linux arm64 builder required; no emulation")
    source_identity(source_sha)
    if out.exists():
        raise RuntimeError("build output already exists; candidate bytes are immutable")
    out.mkdir(parents=True)
    (out / "images").mkdir()
    (out / "sbom").mkdir()
    entries = {}
    for role, target in TARGETS.items():
        tag = f"localhost/cortex-v2-test-{role}:{version}"
        command = ["podman", "build", "--format=oci", "--layers", "--label", f"org.opencontainers.image.revision={source_sha}",
                   "--label", f"org.opencontainers.image.version={version}", "--label", "com.kaidera.deployment-class=TEST", "--tag", tag]
        if target is None:
            command += ["--file", str(ROOT / "deploy/release/Containerfile.db")]
        else:
            command += ["--target", target, "--file", str(ROOT / "Dockerfile")]
        run(command + [str(ROOT)])
        archive = out / "images" / f"{role}.oci.tar"
        run(["podman", "save", "--format=oci-archive", "--output", str(archive), tag])
        identity = oci_identity(archive)
        identity["archive"] = f"images/{role}.oci.tar"
        entries[role] = identity
        (out / "sbom" / f"{role}.spdx.json").write_text(json.dumps(sbom(tag, role, f"{version}-{role}-{identity['manifest_digest'][7:]}"), indent=2) + "\n")
    (out / "image-inventory.json").write_text(json.dumps({"source_sha": source_sha, "version": version, "images": entries,
            "builder": {"podman": run(["podman", "version", "--format", "{{.Client.Version}}"], read=True),
                        "system": platform.platform(), "architecture": platform.machine()}}, indent=2) + "\n")


def host(out: Path, source_sha: str, version: str) -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("native macOS arm64 builder required")
    if platform.python_version() != "3.12.14":
        raise RuntimeError("host freeze requires the pinned CPython 3.12.14 builder")
    source_identity(source_sha)
    if out.exists():
        raise RuntimeError("host output already exists")
    out.mkdir(parents=True)
    run(["python3", "-m", "pip", "install", "--require-hashes", "--only-binary=:all:",
         "--requirement", str(ROOT / "scripts/release/requirements-host-build.txt")])
    run(["python3", "-m", "PyInstaller", "--noconfirm", "--onefile", "--target-arch=arm64", "--name=cortex-test",
         "--paths", str(ROOT / "scripts"), "--distpath", str(out / "bin"), "--workpath", str(out / "work"),
         "--specpath", str(out / "spec"), str(ROOT / "scripts/release/install_candidate.py")])
    dependencies = run(["otool", "-L", str(out / "bin/cortex-test")], read=True)
    if run(["lipo", "-archs", str(out / "bin/cortex-test")], read=True) != "arm64":
        raise RuntimeError("host executable is not the admitted native architecture")
    archive_inventory = run(["python3", "-m", "PyInstaller.utils.cliutils.archive_viewer", "--recursive", "--brief",
                             str(out / "bin/cortex-test")], read=True)
    (out / "host-archive-inventory.txt").write_text(archive_inventory + "\n")
    (out / "licenses").mkdir()
    builder_packages = []
    for name in ("altgraph", "macholib", "packaging", "pyinstaller", "pyinstaller-hooks-contrib", "setuptools"):
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
                 "downloadLocation": f"https://github.com/Kaidera-AI/cortex/tree/{source_sha}", "filesAnalyzed": False}]
    document = spdx_document("Cortex TEST native host inventory", f"{version}-host-{source_sha}", packages)
    document["comment"] = "CPython/installer runtime inventory. Builder dependencies separately recorded; this does not assert all builder libraries are embedded."
    (out / "host.spdx.json").write_text(json.dumps(document, indent=2) + "\n")
    (out / "host-inventory.json").write_text(json.dumps({"source_sha": source_sha, "version": version, "target": "macos-arm64",
                "minimum_macos": "14", "python": platform.python_version(), "dependencies": dependencies.splitlines(),
                "builder_packages": builder_packages,
                "build_lock_sha256": digest(ROOT / "scripts/release/requirements-host-build.txt")}, indent=2) + "\n")
    shutil.rmtree(out / "work")
    shutil.rmtree(out / "spec")


def compose_projection(entries: dict) -> str:
    # JSON is valid YAML. Config IDs are immutable local image identities; the
    # source OCI manifest digests are recorded separately without conflation.
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
                              container_name=f"{INSTANCE}_{role}", mem_limit="512m" if role in ("db", "api") else "256m")
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
            service["environment"].update(CORTEX_V2_WORKER_ID=f"cortex-v2-package-test-{role}", CORTEX_V2_INFERENCE_ROUTING_POLICY="disabled")
            if role == "doc":
                service["environment"]["CORTEX_V2_WORKER_EXECUTOR_ROLES"] = "core,doc"
            elif role == "graph":
                service["environment"]["CORTEX_V2_WORKER_HANDLER_MODULES"] = "cortex_v2.retrieval.jobs"
            service["secrets"] += [secret("worker-token", "worker-token"), secret("worker-principal-id", "worker-principal-id")]
    # Full pinned projection for review. External resources and the opt-in
    # profile prevent accidental construction. The native installer owns setup.
    return json.dumps({"name": INSTANCE, "x-cortex-purpose": "image identity inventory; install with cortex-test",
                       "x-cortex-oci-manifest-digests": {k: v["manifest_digest"] for k, v in entries.items()},
                       "services": services,
                       "networks": {"test": {"external": True, "name": f"{INSTANCE}_net"}},
                       "volumes": {"pgdata": {"external": True, "name": f"{INSTANCE}_pgdata"}},
                       "secrets": {s: {"external": True, "name": f"{INSTANCE}-{s}"} for s in
                                   ("db-owner-password", "db-app-password", "db-migrator-password", "database-url-app",
                                    "database-url-migrator", "fixture", "token-pepper", "worker-token", "worker-principal-id")}}, indent=2) + "\n"


def assemble(out: Path, images_dir: Path, host_dir: Path, source_sha: str, version: str) -> None:
    source_identity(source_sha)
    inventory = json.loads((images_dir / "image-inventory.json").read_text())
    native = json.loads((host_dir / "host-inventory.json").read_text())
    if inventory["source_sha"] != source_sha or inventory["version"] != version or native["source_sha"] != source_sha or native["version"] != version:
        raise RuntimeError("builder receipts do not bind to one frozen source/version")
    if out.exists():
        raise RuntimeError("assembly output already exists")
    out.mkdir(parents=True)
    package = out / f"cortex-{version}-macos-arm64"
    package.mkdir()
    shutil.copytree(images_dir / "images", package / "images")
    shutil.copytree(images_dir / "sbom", package / "sbom")
    shutil.copytree(host_dir / "bin", package / "bin")
    # GitHub artifact upload/download does not preserve executable mode.
    (package / "bin/cortex-test").chmod(0o755)
    shutil.copytree(host_dir / "licenses", package / "licenses")
    shutil.copytree(ROOT / "deploy/release/licenses", package / "licenses", dirs_exist_ok=True)
    shutil.copy2(host_dir / "host.spdx.json", package / "sbom/host.spdx.json")
    shutil.copy2(host_dir / "host-archive-inventory.txt", package / "sbom/host-archive-inventory.txt")
    shutil.copy2(host_dir / "host-inventory.json", package / "sbom/host-inventory.json")
    shutil.copy2(ROOT / "scripts/release/requirements-host-build.txt", package / "sbom/host-build-lock.txt")
    shutil.copy2(ROOT / "docs/install/macos-test-candidate.md", package / "INSTALL-macos.md")
    shutil.copy2(ROOT / "LICENSE", package / "LICENSE")
    (package / "compose-images.yaml").write_text(compose_projection(inventory["images"]))
    files = {p.relative_to(package).as_posix(): digest(p) for p in sorted(package.rglob("*")) if p.is_file()}
    manifest = {"schema": "cortex.test-package.v1", "deployment_class": "TEST", "instance": INSTANCE,
                "version": version, "source_sha": source_sha, "target": "macos-arm64", "minimum_macos": "14",
                "podman_supported_family": "6.0.x", "images": inventory["images"], "files": files,
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
    parser.add_argument("stage", choices=("images", "host", "assemble"))
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--images", type=Path)
    parser.add_argument("--host", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"0\.2\.001-test\.[0-9]{8}\.[1-9][0-9]*", args.version):
        parser.error("explicit immutable TEST version required")
    args.output = args.output.resolve()
    if args.output == ROOT or ROOT in args.output.parents:
        parser.error("output must be outside the source checkout")
    if args.stage == "images":
        images(args.output, args.source_sha, args.version)
    elif args.stage == "host":
        host(args.output, args.source_sha, args.version)
    else:
        if args.images is None or args.host is None:
            parser.error("assemble requires --images and --host")
        assemble(args.output, args.images, args.host, args.source_sha, args.version)


if __name__ == "__main__":
    main()
