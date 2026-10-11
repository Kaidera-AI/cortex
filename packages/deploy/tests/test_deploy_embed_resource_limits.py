"""Frozen exact embed limits, using the existing real installer renderer."""
import copy
import math

import pytest

from test_deploy_measured_resource_limits import (
    PLATFORMS,
    release_module,
    rendered,
    source_compose,
)


SERVICE = "cortex-embed-worker"
EXPECTED = {"mem_limit": 2885681152, "cpus": 2.0}


def assert_embed_limit(config, field):
    value = config["services"][SERVICE].get(field)
    if field == "mem_limit":
        valid = isinstance(value, int) and not isinstance(value, bool)
    else:
        valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
                 and math.isfinite(value))
    assert valid and value == EXPECTED[field], (
        f"{SERVICE}: {field} must equal {EXPECTED[field]!r}, got {value!r}")
    return value


def limited_control():
    config = copy.deepcopy(source_compose())
    config["services"][SERVICE].update(EXPECTED)
    return config


@pytest.mark.parametrize("field", EXPECTED)
def test_embed_source_carries_exact_approved_limit(field):
    assert_embed_limit(source_compose(), field)


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("field", EXPECTED)
def test_actual_renderer_preserves_exact_embed_limit(release_module, platform, field):
    output = rendered(release_module, platform)
    assert assert_embed_limit(output, field) == assert_embed_limit(source_compose(), field)


def test_healthy_source_control_accepts_both_approved_limits():
    for field in EXPECTED:
        assert_embed_limit(limited_control(), field)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_healthy_actual_renderer_preserves_both_embed_limits(release_module, platform):
    output = rendered(release_module, platform, limited_control())
    for field in EXPECTED:
        assert_embed_limit(output, field)


@pytest.mark.parametrize("field,value", [
    ("mem_limit", None), ("mem_limit", 0), ("mem_limit", -1),
    ("mem_limit", True), ("mem_limit", "2752m"), ("mem_limit", 536870912),
    ("cpus", None), ("cpus", 0), ("cpus", -1), ("cpus", True),
    ("cpus", float("inf")), ("cpus", float("nan")), ("cpus", 1.5), ("cpus", "2.0"),
])
def test_wrong_or_missing_embed_limit_is_rejected(field, value):
    config = limited_control()
    config["services"][SERVICE][field] = value
    with pytest.raises(AssertionError, match=SERVICE + ": " + field):
        assert_embed_limit(config, field)


def assert_graph_runtime_unassigned(config):
    graph = config["services"]["cortex-graph-worker"]
    assert "mem_limit" not in graph and "cpus" not in graph


def test_graph_build_envelope_is_not_a_source_runtime_cap():
    assert_graph_runtime_unassigned(source_compose())


@pytest.mark.parametrize("platform", PLATFORMS)
def test_actual_renderer_keeps_graph_runtime_unassigned(release_module, platform):
    assert_graph_runtime_unassigned(rendered(release_module, platform))
