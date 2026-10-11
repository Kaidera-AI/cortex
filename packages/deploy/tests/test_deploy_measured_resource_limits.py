"""Frozen source/rendering invariants for the three measured services only."""
import copy
import importlib.util
import json
import math
from pathlib import Path

import pytest
import yaml


DEPLOY = Path(__file__).resolve().parents[1]
COMPOSE = DEPLOY / "docker-compose.yml"
SERVICES = ("cortex-migrate", "cortex-api", "cortex-pdf-worker")
FIELDS = ("mem_limit", "cpus")
CASES = [(service, field) for service in SERVICES for field in FIELDS]
PLATFORMS = ("linux/arm64", "linux/amd64")


def source_compose():
    return yaml.safe_load(COMPOSE.read_text())


def assert_limit(config, service, field):
    value = config["services"][service].get(field)
    if field == "mem_limit":
        assert isinstance(value, int) and not isinstance(value, bool) and value > 0, (
            f"{service}: {field} must be a positive byte limit, got {value!r}")
    else:
        assert (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value > 0), (
            f"{service}: {field} must be a positive finite CPU limit, got {value!r}")
    return value


@pytest.fixture
def release_module(monkeypatch):
    monkeypatch.syspath_prepend(str(DEPLOY))
    spec = importlib.util.spec_from_file_location("measured_limits_release", DEPLOY / "package-release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.images.__file__).resolve() == (DEPLOY / "image_manifest.py").resolve()
    return module


def rendered(module, platform, compose=None):
    """Actual renderer, complete synthetic locks, actual relative source inputs."""
    identity = {"schema": "cortex.payload.v1", "version": "0.1.004",
                "source_revision": "a" * 40, "schema_revision": "b" * 40}
    inventory = {"schema": "cortex.images.v1", "version": identity["version"],
                 "source_revision": identity["source_revision"], "platforms": {}}
    for arch in PLATFORMS:
        inventory["platforms"][arch] = {
            role: {"repository": "registry.example.invalid/cortex/" + role,
                   "tag": "0.1.004", "manifest_digest": "sha256:" + "a" * 64}
            for role in set(module.images.SERVICE_ROLES.values())
        }
    files = {
        "packages/deploy/docker-compose.yml": (
            COMPOSE.read_bytes() if compose is None else yaml.safe_dump(compose).encode(), 0o644),
        "packages/deploy/image_manifest.py": ((DEPLOY / "image_manifest.py").read_bytes(), 0o644),
        "packages/deploy/backup-db.sh": ((DEPLOY / "backup-db.sh").read_bytes(), 0o755),
        "packages/deploy/tls/init.sh": ((DEPLOY / "tls/init.sh").read_bytes(), 0o755),
    }
    result = module.installation_files(identity, files, {"packages/schema/migrations"}, inventory, platform)
    return json.loads(result[module.images.COMPOSE_FILE][0])


@pytest.mark.parametrize("service,field", CASES)
def test_measured_source_service_carries_limit(service, field):
    assert_limit(source_compose(), service, field)


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("service,field", CASES)
def test_actual_renderer_carries_and_preserves_measured_limit(release_module, platform, service, field):
    output = rendered(release_module, platform)
    actual = assert_limit(output, service, field)
    original = assert_limit(source_compose(), service, field)
    assert actual == original, f"{service}: renderer changed {field} for {platform}"


def limited_control():
    config = source_compose()
    for service in SERVICES:
        config["services"][service].update(mem_limit=536870912,
            cpus=2.0 if service == "cortex-pdf-worker" else 1.5)
    return config


def test_healthy_in_memory_source_control_accepts_all_six_limits():
    for service, field in CASES:
        assert_limit(limited_control(), service, field)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_healthy_actual_renderer_preserves_all_six_limits(release_module, platform):
    source = limited_control()
    output = rendered(release_module, platform, source)
    assert set(output["services"]) == set(release_module.images.SERVICE_ROLES)
    for service, field in CASES:
        assert assert_limit(output, service, field) == assert_limit(source, service, field)


@pytest.mark.parametrize("field,value", [
    ("mem_limit", None), ("mem_limit", 0), ("mem_limit", -1), ("mem_limit", True),
    ("cpus", None), ("cpus", 0), ("cpus", -1), ("cpus", True),
    ("cpus", float("inf")), ("cpus", float("nan")),
])
def test_invalid_limit_controls_are_rejected(field, value):
    config = limited_control()
    config["services"]["cortex-api"][field] = value
    with pytest.raises(AssertionError, match="cortex-api: " + field):
        assert_limit(config, "cortex-api", field)


def test_source_service_set_matches_runtime_contract(release_module):
    assert set(source_compose()["services"]) == set(release_module.images.SERVICE_ROLES)
