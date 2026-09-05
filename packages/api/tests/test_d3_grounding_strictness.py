"""K0 finding 2 (v0.2.006 lane, 2026-09-04): grounding strictness for the
completion evidence floor.

- applicability is by actor kind ONLY: the legacy ``evidence_quality`` metadata
  stamp no longer counts, and a human carrying worker metadata is never floored;
- evidence validation is recursive: `[{}]`, `[""]`, `[{"cmd": {}}]` and blank
  artifacts are NOT evidence (the pre-rework validator accepted `str({})`);
- an agent with no meaningful evidence class is floored to partial; real
  evidence keeps completed.

Written RED-first against the reviewed fold tip 19883f24.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_api(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, API_MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.path.insert(0, str(API_MAIN_PATH.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(API_MAIN_PATH.parent))
    return module


def _report(**overrides):
    base = {"schema": "cortex.handoff_completion_report.v1", "outcome": "completed",
            "summary": "done", "decision": None, "work_product_id": None,
            "tests_run": [], "artifacts": [], "metadata": {}}
    base.update(overrides)
    return base


def test_human_with_worker_metadata_is_never_floored():
    """Applicability is by actor kind only; the metadata stamp does not floor humans."""
    module = load_api("cortex_api_d3_grounding_test")
    out = module.apply_completion_evidence_floor(
        _report(metadata={"evidence_quality": "auto_transcript"}), actor_kind="human",
    )
    assert out["outcome"] == "completed", "human returns are never floored"
    assert "evidence_floor" not in out["metadata"]


def test_nested_blank_values_are_not_evidence():
    """Recursive validation: {"cmd": {}} stringifies to a non-empty string and
    slipped past the pre-rework validator (probe: agent_nested_empty completed).
    """
    module = load_api("cortex_api_d3_grounding_nested_test")
    for bad in ([{"cmd": {}}], [{"cmd": ""}], [{"cmd": {"rc": ""}}], [{}], [""]):
        out = module.apply_completion_evidence_floor(
            _report(tests_run=list(bad)), actor_kind="agent",
        )
        assert out["outcome"] == "partial", f"nested blank evidence accepted: {bad}"

    for good in ([{"cmd": "pytest", "rc": 0}], ["pytest -q: 1 passed"]):
        out = module.apply_completion_evidence_floor(
            _report(tests_run=list(good)), actor_kind="agent",
        )
        assert out["outcome"] == "completed", f"real evidence floored: {good}"

    out = module.apply_completion_evidence_floor(
        _report(artifacts=[""]), actor_kind="agent",
    )
    assert out["outcome"] == "partial", "blank artifacts are not evidence"
