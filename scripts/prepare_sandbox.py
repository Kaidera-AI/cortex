from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

SANDBOX_INSTANCE = "cortex-v2-v0-02-001-sandbox"
W1_INSTANCE = "cortex-v2-w1-candidate"
KAI_TEST_INSTANCE = "cortex_kai_test"
PACKAGE_TEST_INSTANCE = "cortex_v2_package_test"
STATE_DIRECTORIES = {
    SANDBOX_INSTANCE: "cortex-v2-sandbox",
    W1_INSTANCE: "cortex-v2-w1-candidate",
    KAI_TEST_INSTANCE: "cortex-kai-test",
    PACKAGE_TEST_INSTANCE: "cortex-v2-package-test",
}
DATABASE_HOSTS = {
    SANDBOX_INSTANCE: "db",
    W1_INSTANCE: "w1-db",
    KAI_TEST_INSTANCE: "db",
    PACKAGE_TEST_INSTANCE: "db",
}
SANDBOX_SECRET_FILES = (
    "db-owner-password",
    "db-app-password",
    "db-migrator-password",
    "database-url-app",
    "database-url-migrator",
    "token-pepper",
    "worker.token",
    "fixture.json",
)
W1_SECRET_FILES = (
    "db-owner-password",
    "db-app-password",
    "db-migrator-password",
    "database-url-app",
    "database-url-migrator",
    "token-pepper",
    "owner.token",
    "worker.token",
    "recovery.token",
    "fixture.json",
)
KAI_SECRET_FILES = (
    *W1_SECRET_FILES,
    "worker-principal-id",
)
SECRET_FILES_BY_INSTANCE = {
    SANDBOX_INSTANCE: SANDBOX_SECRET_FILES,
    W1_INSTANCE: W1_SECRET_FILES,
    KAI_TEST_INSTANCE: KAI_SECRET_FILES,
    PACKAGE_TEST_INSTANCE: KAI_SECRET_FILES,
}


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"refusing a symlink sandbox path: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise RuntimeError(f"sandbox path is not a directory: {path}")
    os.chmod(path, 0o700)
    current_owner = os.getuid() if hasattr(os, "getuid") else None
    if current_owner is not None and path.stat().st_uid != current_owner:
        raise RuntimeError(f"sandbox directory is not owned by this user: {path}")


def _verify_private_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"invalid sandbox credential file: {path.name}")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError(f"sandbox credential permissions are too broad: {path.name}")
    current_owner = os.getuid() if hasattr(os, "getuid") else None
    if current_owner is not None and path.stat().st_uid != current_owner:
        raise RuntimeError(
            f"sandbox credential file has an unexpected owner: {path.name}"
        )


def _digest(pepper: bytes, token: str) -> str:
    return hmac.new(pepper, token.encode(), hashlib.sha256).hexdigest()


def _database_urls(instance: str, app_password: str, migrator_password: str) -> tuple[str, str]:
    host = DATABASE_HOSTS[instance]
    app_url = (
        f"postgresql://cortex_v2_app:{quote(app_password, safe='')}@{host}:5432/cortex_v2"
    )
    migrator_url = (
        f"postgresql://cortex_v2_migrator:"
        f"{quote(migrator_password, safe='')}@{host}:5432/cortex_v2"
    )
    return app_url, migrator_url


def _sandbox_values(instance: str) -> dict[str, bytes]:
    owner_password = secrets.token_hex(32)
    app_password = secrets.token_hex(32)
    migrator_password = secrets.token_hex(32)
    worker_token = secrets.token_urlsafe(32)
    pepper = secrets.token_bytes(32)

    fixture = {
        "instance_id": instance,
        "installation_id": str(uuid.uuid4()),
        "principal_id": str(uuid.uuid4()),
        "credential_id": str(uuid.uuid4()),
        "credential_generation": 1,
        "credential_hash": _digest(pepper, worker_token),
        "project_scope_id": str(uuid.uuid4()),
        "shared_scope_id": str(uuid.uuid4()),
        "local_scope_id": str(uuid.uuid4()),
        "shared_record_id": str(uuid.uuid4()),
        "shared_event_id": str(uuid.uuid4()),
        "project_alias": "sandbox-project",
        "shared_alias": "shared-library",
        "local_alias": "local-state",
    }
    app_url, migrator_url = _database_urls(instance, app_password, migrator_password)
    return {
        "db-owner-password": owner_password.encode(),
        "db-app-password": app_password.encode(),
        "db-migrator-password": migrator_password.encode(),
        "database-url-app": app_url.encode(),
        "database-url-migrator": migrator_url.encode(),
        "token-pepper": pepper.hex().encode(),
        "worker.token": worker_token.encode(),
        "fixture.json": (json.dumps(fixture, sort_keys=True, indent=2) + "\n").encode(),
    }


def _w1_values(instance: str) -> dict[str, bytes]:
    owner_password = secrets.token_hex(32)
    app_password = secrets.token_hex(32)
    migrator_password = secrets.token_hex(32)
    owner_token = secrets.token_urlsafe(32)
    worker_token = secrets.token_urlsafe(32)
    recovery_token = secrets.token_urlsafe(32)
    pepper = secrets.token_bytes(32)

    fixture = {
        "instance_id": instance,
        "installation_id": str(uuid.uuid4()),
        "owner_principal_id": str(uuid.uuid4()),
        "worker_principal_id": str(uuid.uuid4()),
        "owner_actor_id": str(uuid.uuid4()),
        "worker_actor_id": str(uuid.uuid4()),
        "owner_credential_id": str(uuid.uuid4()),
        "worker_credential_id": str(uuid.uuid4()),
        "credential_generation": 1,
        "owner_credential_hash": _digest(pepper, owner_token),
        "worker_credential_hash": _digest(pepper, worker_token),
        "recovery_hash": _digest(pepper, recovery_token),
        "recovery_generation": 1,
        "project_scope_id": str(uuid.uuid4()),
        "shared_scope_id": str(uuid.uuid4()),
        "local_scope_id": str(uuid.uuid4()),
        "ungranted_scope_id": str(uuid.uuid4()),
        "shared_record_id": str(uuid.uuid4()),
        "shared_event_id": str(uuid.uuid4()),
        "ungranted_record_id": str(uuid.uuid4()),
        "ungranted_event_id": str(uuid.uuid4()),
        "connector_id": str(uuid.uuid4()),
        "project_alias": "sandbox-project",
        "shared_alias": "shared-library",
        "local_alias": "local-state",
        "ungranted_alias": "ungranted-probe",
    }
    app_url, migrator_url = _database_urls(instance, app_password, migrator_password)
    return {
        "db-owner-password": owner_password.encode(),
        "db-app-password": app_password.encode(),
        "db-migrator-password": migrator_password.encode(),
        "database-url-app": app_url.encode(),
        "database-url-migrator": migrator_url.encode(),
        "token-pepper": pepper.hex().encode(),
        "owner.token": owner_token.encode(),
        "worker.token": worker_token.encode(),
        "recovery.token": recovery_token.encode(),
        "fixture.json": (json.dumps(fixture, sort_keys=True, indent=2) + "\n").encode(),
    }


def _kai_values(instance: str) -> dict[str, bytes]:
    values = _w1_values(instance)
    fixture = json.loads(values["fixture.json"])
    values["worker-principal-id"] = fixture["worker_principal_id"].encode()
    return values


VALUES_BUILDERS = {
    SANDBOX_INSTANCE: _sandbox_values,
    W1_INSTANCE: _w1_values,
    KAI_TEST_INSTANCE: _kai_values,
    PACKAGE_TEST_INSTANCE: _kai_values,
}


def prepare(root: Path, instance: str) -> bool:
    secret_files = SECRET_FILES_BY_INSTANCE[instance]
    root = root.expanduser()
    if root.parent.is_symlink():
        raise RuntimeError(
            f"refusing a symlink parent for sandbox credentials: {root.parent}"
        )
    secrets_dir = root / "secrets"
    _ensure_private_directory(root)
    _ensure_private_directory(secrets_dir)

    existing = [
        name for name in secret_files
        if (secrets_dir / name).exists() or (secrets_dir / name).is_symlink()
    ]
    if existing:
        if set(existing) != set(secret_files):
            raise RuntimeError(
                "partial sandbox credentials found; refusing to overwrite or "
                "rotate them"
            )
        for name in secret_files:
            _verify_private_file(secrets_dir / name)
        return False

    values = VALUES_BUILDERS[instance](instance)
    written: list[Path] = []
    try:
        for name in secret_files:
            path = secrets_dir / name
            _write_private(path, values[name])
            written.append(path)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return True


def main() -> int:
    instance = os.environ.get("CORTEX_V2_SANDBOX_INSTANCE", "")
    if instance not in STATE_DIRECTORIES:
        print(
            "sandbox preparation refused: a named candidate instance is required",
            file=sys.stderr,
        )
        return 2
    root = Path.home() / ".kaidera-os" / STATE_DIRECTORIES[instance]
    try:
        created = prepare(root, instance)
    except (OSError, RuntimeError) as exc:
        print(f"sandbox preparation refused: {exc}", file=sys.stderr)
        return 2
    print(
        f"{instance} credentials prepared privately."
        if created
        else f"Existing private {instance} credentials verified; none were changed."
    )
    print(f"Sandbox state directory: {root}")
    print("No credential value was printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
