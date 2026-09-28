"""Read-only collision preflight for the cortex_kai_test integrated stack.

Checks exact resource names only — never enumerates by prefix, never adopts or
renames by name match, never removes anything. Aborts with exit 2 when any
planned container, volume, network or secret name already exists, so the
integrated stack is only ever created into a clean namespace.

Usage (host, read-only):
    PODMAN_CONNECTION_NAME=kos-e020-uat \
    python3 cortex/v2/scripts/preflight_cortex_kai_test.py \
        --migrate-run-id "$(openssl rand -hex 4)"
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cortex_v2.config import (  # noqa: E402
    INSTANCE_PROFILES,
    KAI_TEST_INSTANCE,
    MIGRATE_RUN_ID_PATTERN,
)

EXPECTED_PODMAN_CONNECTION = "kos-e020-uat"
ROLES = ("db", "api", "doc", "embed", "graph", "tests")
SECRET_SUFFIXES = (
    "db-owner-password",
    "db-app-password",
    "db-migrator-password",
    "database-url-app",
    "database-url-migrator",
    "token-pepper",
    "fixture",
    "owner-token",
    "worker-token",
    "recovery-token",
    "worker-principal-id",
)
API_HOST_PORT = 8602


def _podman_exists(connection: str, resource: str, name: str) -> bool:
    try:
        result = subprocess.run(
            ["podman", "--connection", connection, resource, "exists", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"podman could not check {resource} name: {name}"
        ) from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise RuntimeError(f"podman could not check {resource} name: {name}")


def planned_names(run_id: str) -> dict[str, list[str]]:
    profile = INSTANCE_PROFILES[KAI_TEST_INSTANCE]
    if not MIGRATE_RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("migrate run id must be 8-16 lowercase hex characters")
    return {
        "container": [
            *(profile.container_name(role) for role in ROLES),
            profile.migrate_container_name(run_id),
        ],
        "volume": [profile.volume_name("pgdata")],
        "network": [profile.network_name("net")],
        "secret": [profile.secret_name(suffix) for suffix in SECRET_SUFFIXES],
    }


def _port_listener_state() -> str:
    try:
        listener = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{API_HOST_PORT}", "-sTCP:LISTEN"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"cannot verify host port {API_HOST_PORT} state"
        ) from exc
    return "bound" if listener.returncode == 0 and listener.stdout else "unbound"


PRIVATE_SECRETS_DIR = Path.home() / ".kaidera-os" / "cortex-kai-test" / "secrets"
SOURCE_HASH_LABEL = "com.kaidera.v2-sandbox-source-sha256"
SECRET_SOURCE_FILES = {
    "db-owner-password": "db-owner-password",
    "db-app-password": "db-app-password",
    "db-migrator-password": "db-migrator-password",
    "database-url-app": "database-url-app",
    "database-url-migrator": "database-url-migrator",
    "token-pepper": "token-pepper",
    "fixture": "fixture.json",
    "owner-token": "owner.token",
    "worker-token": "worker.token",
    "recovery-token": "recovery.token",
    "worker-principal-id": "worker-principal-id",
}


def _secret_origin(
    connection: str, secret_name: str, suffix: str
) -> str:
    """Classify an existing secret: 'ours' only on a source-hash label match.

    A matching name is never sufficient proof of ownership; only the
    provenance label written by the guarded provisioner against the private
    source file counts. Anything else is a foreign collision.
    """
    if not _podman_exists(connection, "secret", secret_name):
        return "absent"
    source_path = PRIVATE_SECRETS_DIR / SECRET_SOURCE_FILES[suffix]
    try:
        expected = hashlib.sha256(source_path.read_bytes()).hexdigest()
    except OSError:
        return "foreign"
    try:
        inspected = subprocess.run(
            [
                "podman", "--connection", connection, "secret", "inspect",
                "--format",
                f'{{{{index .Spec.Labels "{SOURCE_HASH_LABEL}"}}}}',
                secret_name,
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"podman could not inspect secret: {secret_name}"
        ) from exc
    if inspected.returncode != 0:
        return "foreign"
    label = inspected.stdout.decode("utf-8", errors="replace").strip()
    return "ours" if label == expected else "foreign"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate-run-id", required=True)
    arguments = parser.parse_args()

    connection = os.environ.get("PODMAN_CONNECTION_NAME")
    if connection != EXPECTED_PODMAN_CONNECTION:
        print(
            "preflight refused: PODMAN_CONNECTION_NAME must select the accepted "
            "machine",
            file=sys.stderr,
        )
        return 2
    try:
        names = planned_names(arguments.migrate_run_id)
    except ValueError as exc:
        print(f"preflight refused: {exc}", file=sys.stderr)
        return 2

    try:
        collisions: list[str] = []
        checked = 0
        secret_states: dict[str, int] = {"absent": 0, "ours": 0, "foreign": 0}
        for resource, resource_names in names.items():
            for name in resource_names:
                checked += 1
                if resource == "secret":
                    suffix = name.split(f"{KAI_TEST_INSTANCE}-", 1)[1]
                    state = _secret_origin(connection, name, suffix)
                    secret_states[state] += 1
                    if state == "foreign":
                        collisions.append(f"secret:{name} (foreign origin)")
                elif _podman_exists(connection, resource, name):
                    collisions.append(f"{resource}:{name}")
        port_state = _port_listener_state()
    except RuntimeError as exc:
        print(f"preflight refused: {exc}", file=sys.stderr)
        return 2

    print(f"checked {checked} exact planned names on {connection}")
    print(
        "secrets: {absent} absent, {ours} provisioner-origin-verified, "
        "{foreign} foreign".format(**secret_states)
    )
    print(f"host port {API_HOST_PORT}: {port_state} (informational)")
    print(
        "start-time recheck required after the W1 api stop: "
        f"lsof -nP -iTCP:{API_HOST_PORT} -sTCP:LISTEN must show no listener"
    )
    if collisions:
        print("COLLISIONS PRESENT — refusing to create:", file=sys.stderr)
        for collision in collisions:
            print(f"  {collision}", file=sys.stderr)
        return 2
    print("no collisions; namespace is clean for creation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
