from __future__ import annotations

import re
from pathlib import Path

import pytest

from cortex_v2.config import (
    FULL_V2_MIGRATIONS,
    FULL_V2_OPERATION_MODULES,
    INSTANCE_PROFILES,
    INTEGRATED_OPERATION_MODULES,
    KAI_TEST_INSTANCE,
    MIGRATE_RUN_ID_PATTERN,
    SANDBOX_INSTANCE,
    W1_INSTANCE,
    ConfigurationError,
)

PODMAN_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
LOWER_GROUP_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]*$")
INTEGRATED_ROLES = ("db", "api", "doc", "embed", "graph", "tests")


def test_all_profiles_are_registered_with_unique_ids() -> None:
    ids = [profile.instance_id for profile in INSTANCE_PROFILES.values()]
    assert len(set(ids)) == len(ids)


def test_full_v2_migrations_apply_in_strict_ascending_order() -> None:
    numbers = [int(name.split("_", 1)[0]) for name in FULL_V2_MIGRATIONS]
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == len(numbers)
    assert FULL_V2_MIGRATIONS[0] == "0001_core.sql"
    on_disk = sorted(
        path.name
        for path in (Path(__file__).resolve().parents[1] / "migrations").iterdir()
        if path.suffix == ".sql"
    )
    assert list(FULL_V2_MIGRATIONS) == on_disk
    for profile in (INSTANCE_PROFILES[W1_INSTANCE], INSTANCE_PROFILES[KAI_TEST_INSTANCE]):
        assert profile.migrations == FULL_V2_MIGRATIONS
    assert (
        INSTANCE_PROFILES[W1_INSTANCE].operation_modules == FULL_V2_OPERATION_MODULES
    )
    assert (
        INSTANCE_PROFILES[KAI_TEST_INSTANCE].operation_modules
        == INTEGRATED_OPERATION_MODULES
    )
    assert set(FULL_V2_OPERATION_MODULES) < set(INTEGRATED_OPERATION_MODULES)


def test_integrated_machine_group_and_container_names_match_the_plan() -> None:
    profile = INSTANCE_PROFILES[KAI_TEST_INSTANCE]
    assert profile.instance_id == "cortex_kai_test"
    assert LOWER_GROUP_PATTERN.fullmatch(profile.instance_id)
    assert LOWER_GROUP_PATTERN.fullmatch(profile.resource_prefix)
    for role in INTEGRATED_ROLES:
        name = profile.container_name(role)
        assert name == f"Cortex_kai_test_{role}"
        assert PODMAN_NAME_PATTERN.fullmatch(name)
    assert profile.volume_name("pgdata") == "cortex_kai_test_pgdata"
    assert profile.network_name("net") == "cortex_kai_test_net"


def test_migrate_run_id_is_validated_and_lowercase_hex_only() -> None:
    profile = INSTANCE_PROFILES[KAI_TEST_INSTANCE]
    assert profile.migrate_container_name("0a1b2c3d") == "Cortex_kai_test_migrate_0a1b2c3d"
    assert profile.migrate_container_name("deadbeefcafe0123") == (
        "Cortex_kai_test_migrate_deadbeefcafe0123"
    )
    for bad in ("", "0A1B2C3D", "0g12abcd", "0a1b2c", "0a1b2c3d4e5f6a7b8", "run-0a1b2c3d"):
        assert not MIGRATE_RUN_ID_PATTERN.fullmatch(bad)
        with pytest.raises(ConfigurationError):
            profile.migrate_container_name(bad)


def test_secret_names_are_valid_podman_names_for_every_profile() -> None:
    suffixes = (
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
    )
    for profile in INSTANCE_PROFILES.values():
        for suffix in suffixes:
            assert PODMAN_NAME_PATTERN.fullmatch(profile.secret_name(suffix))


def test_legacy_profiles_refuse_to_invent_container_names() -> None:
    for instance in (SANDBOX_INSTANCE, W1_INSTANCE):
        profile = INSTANCE_PROFILES[instance]
        with pytest.raises(ConfigurationError):
            profile.container_name("db")
        with pytest.raises(ConfigurationError):
            profile.volume_name("pgdata")


def test_integrated_network_hosts_match_compose_service_keys() -> None:
    profile = INSTANCE_PROFILES[KAI_TEST_INSTANCE]
    assert profile.database_host == "db"
    assert profile.api_host == "api"
    assert profile.api_port == 8601
    assert profile.deployment_class == "isolated-kai-test"
    assert profile.contract_version == "cortex.api.v2-integrated"
    assert profile.state_directory == "cortex-kai-test"


def test_preflight_port_probe_fails_closed(monkeypatch) -> None:
    import importlib.util
    import subprocess
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "preflight_cortex_kai_test",
        repo_root / "scripts" / "preflight_cortex_kai_test.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def timeout_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="lsof", timeout=30)

    def missing_run(*args, **kwargs):
        raise FileNotFoundError("lsof")

    monkeypatch.setattr(module.subprocess, "run", timeout_run)
    with pytest.raises(RuntimeError):
        module._port_listener_state()

    monkeypatch.setattr(module.subprocess, "run", missing_run)
    with pytest.raises(RuntimeError):
        module._port_listener_state()
