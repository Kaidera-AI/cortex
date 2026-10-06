"""Required disposable native-CI rehearsal; never an installed product entrypoint."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import time
import uuid
from pathlib import Path

from install_candidate import (INSTANCE, ROLES, Podman, Refusal, erase, names,
                               namespace, private_root, provision, smoke,
                               start_stack, write_record)
from prepare_sandbox import prepare

ROOT = Path(__file__).resolve().parents[2]


class NativeBuilder(Podman):
    def __init__(self, target="macos-arm64"):
        if target not in ("macos-arm64", "linux-x86_64"):
            raise Refusal("closed native rehearsal target required")
        self.architecture = "amd64" if target == "linux-x86_64" else "arm64"
        machines = ("x86_64",) if self.architecture == "amd64" else ("arm64", "aarch64")
        if (os.environ.get("GITHUB_ACTIONS") != "true" or platform.system() != "Linux"
                or platform.machine() not in machines):
            raise Refusal("package rehearsal requires the disposable native CI builder")
        executable = shutil.which("podman")
        if not executable:
            raise Refusal("builder Podman unavailable")
        self.prefix = [executable]
        if self.architecture == "amd64":
            self.prefix.append("--remote=false")
            self.local_abi = True
            self.preflight()
        if self.run(["info", "--format", "{{.Host.Security.Rootless}}"], read=True) != "true":
            raise Refusal("package rehearsal requires rootless Podman")


def rehearse(entries: dict, source_sha: str, version: str, target="macos-arm64") -> dict:
    engine = NativeBuilder(target)
    installation = str(uuid.uuid4())
    root = Path.home() / ".cortex" / "test" / ("ci-rehearsal-" + installation)
    while True:
        with socket.socket() as stream:
            stream.bind(("127.0.0.1", 0))
            port = stream.getsockname()[1]
        if port not in (8501, 5499, 5500):
            break
    record = {"schema": "cortex.test-install.v1", "version": version, "source_sha": source_sha,
              "installation": installation, "connection": "ci-native", "port": port,
              "stage": "staged", "loaded_images": []}
    record["namespace"] = namespace(record)
    for kind, name in names(record):
        if engine.exists(kind, name):
            raise Refusal("rehearsal object collision")
    private_root(root, create=True)
    write_record(root, record)
    # All private bytes stay in the runner's owner-only home and Podman secrets.
    prepare(root / "state", INSTANCE)
    manifest = {"images": entries}
    outcome = {"scope": "native Linux " + engine.architecture + " CI; not installed product qualification",
               "target": target,
               "source_sha": source_sha, "version": version,
               "podman": engine.run(["version", "--format", "{{.Client.Version}}"], read=True),
               "image_ids": {r: entries[r]["config_id"] for r in ROLES}}
    try:
        if target == 'linux-x86_64':
            from linux_build_catalog import catalog_bytes, read_json
            provision(engine, manifest, root, record, build_catalog_source=source_sha)
        else:
            provision(engine, manifest, root, record)
        start_stack(engine, manifest, record, root)
        migration = (read_json(root / 'migration-receipt.json') if target == 'linux-x86_64'
                     else json.loads((root / "migration-receipt.json").read_text()))
        if len(migration.get("migrations", [])) != 15:
            raise Refusal("rehearsal did not execute the complete migration set")
        outcome["migration"] = migration
        if target == 'linux-x86_64':
            initial_catalog = catalog_bytes(migration, source_sha=source_sha,
                source_root=ROOT / 'src', migration_root=ROOT / 'migrations')
            outcome['build_catalog'] = json.loads(initial_catalog)
            outcome['build_catalog_sha256'] = hashlib.sha256(initial_catalog).hexdigest()
        # Readiness is reached from the host through the internal network's
        # loopback publication. The authenticated smoke then uses that same URL.
        smoke(root, record)
        outcome["internal_network_loopback_publish"] = "PASS"
        outcome["smoke"] = json.loads((root / "smoke-receipt.json").read_text())
        for role in reversed(ROLES):
            engine.run(["stop", "--time=30", namespace(record) + "_" + role])
        start_stack(engine, manifest, record, root)
        repeated = (read_json(root / 'migration-receipt.json') if target == 'linux-x86_64'
                    else json.loads((root / "migration-receipt.json").read_text()))
        if repeated["migrations"] != migration["migrations"]:
            raise Refusal("migration replay checksums differ")
        if target == 'linux-x86_64':
            replay_catalog = catalog_bytes(repeated, source_sha=source_sha,
                source_root=ROOT / 'src', migration_root=ROOT / 'migrations')
            if replay_catalog != initial_catalog:
                raise Refusal('native build catalog changed on replay')
        smoke(root, record)
        outcome["migration_replay"] = repeated
        outcome["restart_smoke"] = json.loads((root / "smoke-receipt.json").read_text())
        outcome["status"] = "PASS"
        outcome["epoch"] = int(time.time())
    finally:
        # Explicit CI-owned fixture retirement, never an automatic host install
        # rollback. Image bytes are retained for export/assembly in this job.
        outcome["cleanup"] = erase(engine, root, record, installation)
    return outcome
