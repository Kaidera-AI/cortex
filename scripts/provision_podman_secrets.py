from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

SANDBOX_INSTANCE = "cortex-v2-v0-02-001-sandbox"
W1_INSTANCE = "cortex-v2-w1-candidate"
KAI_TEST_INSTANCE = "cortex_kai_test"
STATE_DIRECTORIES = {
    SANDBOX_INSTANCE: "cortex-v2-sandbox",
    W1_INSTANCE: "cortex-v2-w1-candidate",
    KAI_TEST_INSTANCE: "cortex-kai-test",
}
EXPECTED_PODMAN_CONNECTION = "kos-e020-uat"
SOURCE_HASH_LABEL = "com.kaidera.v2-sandbox-source-sha256"
SANDBOX_SECRET_SOURCES = (
    ("db-owner-password", "db-owner-password"),
    ("db-app-password", "db-app-password"),
    ("db-migrator-password", "db-migrator-password"),
    ("database-url-app", "database-url-app"),
    ("database-url-migrator", "database-url-migrator"),
    ("token-pepper", "token-pepper"),
    ("fixture.json", "fixture"),
    ("worker.token", "worker-token"),
)
W1_SECRET_SOURCES = (
    ("db-owner-password", "db-owner-password"),
    ("db-app-password", "db-app-password"),
    ("db-migrator-password", "db-migrator-password"),
    ("database-url-app", "database-url-app"),
    ("database-url-migrator", "database-url-migrator"),
    ("token-pepper", "token-pepper"),
    ("fixture.json", "fixture"),
    ("owner.token", "owner-token"),
    ("worker.token", "worker-token"),
    ("recovery.token", "recovery-token"),
)
KAI_SECRET_SOURCES = (
    *W1_SECRET_SOURCES,
    ("worker-principal-id", "worker-principal-id"),
)
SECRET_SOURCES_BY_INSTANCE = {
    SANDBOX_INSTANCE: SANDBOX_SECRET_SOURCES,
    W1_INSTANCE: W1_SECRET_SOURCES,
    KAI_TEST_INSTANCE: KAI_SECRET_SOURCES,
}


class ProvisionError(RuntimeError):
    pass


def _verify_private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProvisionError("sandbox secret source directory is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ProvisionError("sandbox secret source directory has unsafe ownership")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ProvisionError("sandbox secret source directory must have mode 0700")


def _verify_private_parent(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProvisionError("sandbox state parent is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ProvisionError("sandbox state parent has unsafe ownership")


def _open_private_sources(
    secrets_dir: Path, secret_sources: tuple[tuple[str, str], ...], instance: str
) -> list[tuple[str, str, bytes, str]]:
    _verify_private_parent(secrets_dir.parent.parent)

    _verify_private_directory(secrets_dir.parent)
    _verify_private_directory(secrets_dir)
    try:
        names = set(os.listdir(secrets_dir))
    except OSError as exc:
        raise ProvisionError("sandbox secret source names are unavailable") from exc
    expected_names = {source for source, _ in secret_sources}
    if names != expected_names:
        raise ProvisionError(
            "sandbox secret source names do not match the expected set"
        )

    sources: list[tuple[str, str, bytes, str]] = []
    for source, secret_suffix in secret_sources:
        path = secrets_dir / source
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise ProvisionError(
                        f"sandbox secret source has unsafe ownership or mode: {source}"
                    )
                chunks = []
                while chunk := os.read(descriptor, 64 * 1024):
                    chunks.append(chunk)
                content = b"".join(chunks)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ProvisionError("sandbox secret source could not be read") from exc
        secret_name = f"{instance}-{secret_suffix}"
        digest = hashlib.sha256(content).hexdigest()
        sources.append((source, secret_name, content, digest))
    return sources


def _podman(
    args: list[str],
    *,
    capture_stdout: bool = False,
    input_data: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    connection_name = os.environ.get("PODMAN_CONNECTION_NAME")
    if connection_name != EXPECTED_PODMAN_CONNECTION:
        raise ProvisionError(
            "PODMAN_CONNECTION_NAME must select the accepted sandbox machine"
        )
    try:
        return subprocess.run(
            ["podman", "--connection", connection_name, *args],
            check=False,
            input=input_data,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvisionError("Podman command could not be completed") from exc


def _existing_secret_matches(secret_name: str, digest: str) -> bool:
    exists = _podman(["secret", "exists", secret_name])
    if exists.returncode == 1:
        return False
    if exists.returncode != 0:
        raise ProvisionError(f"Podman could not check candidate secret: {secret_name}")

    inspected = _podman(
        [
            "secret",
            "inspect",
            "--format",
            f'{{{{index .Spec.Labels "{SOURCE_HASH_LABEL}"}}}}',
            secret_name,
        ],
        capture_stdout=True,
    )
    if inspected.returncode != 0:
        raise ProvisionError(
            f"Podman could not inspect candidate secret: {secret_name}"
        )
    if inspected.stdout.decode("utf-8", errors="replace").strip() != digest:
        raise ProvisionError(
            f"source-hash label mismatch for candidate secret: {secret_name}"
        )
    return True


def provision(instance: str) -> None:
    secrets_dir = (
        Path.home() / ".kaidera-os" / STATE_DIRECTORIES[instance] / "secrets"
    )
    sources = _open_private_sources(
        secrets_dir, SECRET_SOURCES_BY_INSTANCE[instance], instance
    )
    to_create: list[tuple[str, str, bytes, str]] = []
    for item in sources:
        _, secret_name, _, digest = item
        if not _existing_secret_matches(secret_name, digest):
            to_create.append(item)

    created: list[str] = []
    for _, secret_name, content, digest in to_create:
        result = _podman(
            [
                "secret",
                "create",
                "--label",
                f"{SOURCE_HASH_LABEL}={digest}",
                secret_name,
                "-",
            ],
            input_data=content,
        )
        if result.returncode != 0:
            names = ", ".join([*created, secret_name])
            raise ProvisionError(
                f"creation failed; no cleanup performed; inspect candidate "
                f"secrets: {names}"
            )
        created.append(secret_name)
        try:
            verified = _existing_secret_matches(secret_name, digest)
        except ProvisionError as exc:
            names = ", ".join(created)
            raise ProvisionError(
                "post-create label verification failed; no cleanup "
                f"performed; inspect candidate secrets: {names}"
            ) from exc
        if not verified:
            names = ", ".join(created)
            raise ProvisionError(
                "created candidate secret disappeared before label "
                f"verification; no cleanup performed; inspect candidate "
                f"secrets: {names}"
            )
    if created:
        print(f"Provisioned candidate Podman secrets: {', '.join(created)}")
    else:
        print("All candidate Podman secrets already match their private sources.")


def main() -> int:
    instance = os.environ.get("CORTEX_V2_SANDBOX_INSTANCE", "")
    if instance not in STATE_DIRECTORIES:
        print(
            "sandbox provisioning refused: candidate instance marker is required",
            file=sys.stderr,
        )
        return 2
    if os.environ.get("PODMAN_CONNECTION_NAME") != EXPECTED_PODMAN_CONNECTION:
        print(
            "sandbox provisioning refused: exact Podman connection is required",
            file=sys.stderr,
        )
        return 2
    try:
        provision(instance)
    except ProvisionError as exc:
        print(f"sandbox provisioning refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
