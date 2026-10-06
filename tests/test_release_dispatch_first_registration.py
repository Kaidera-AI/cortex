"""R211: a missing workflow is never an empty run list without two complete reads.

Literal provider responses only; no GitHub, ref, CI or host mutations.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
WORKFLOW = "cortex-linux-candidate.yml"
RUNS = f"actions/workflows/{WORKFLOW}/runs?head_sha={SHA}&per_page=100"
INVENTORY = "actions/workflows?per_page=100"
ALL_RUNS = f"actions/runs?head_sha={SHA}&per_page=100"
PUSH = "on:\n  push:\n    branches: [ren-cx/linux-package-build-admitted-*]\n  workflow_dispatch:\n"


def load():
    spec = importlib.util.spec_from_file_location("guard_registration", ROOT / "scripts/release/dispatch-once.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workflow(identifier=19, path=".github/workflows/ci.yml", name="CI", state="active"):
    return {"id": identifier, "path": path, "name": name, "state": state}


def inventory(rows=None, total=None):
    rows = [workflow()] if rows is None else rows
    return {"total_count": len(rows) if total is None else total, "workflows": rows}


def runs(rows=None, total=None):
    rows = [] if rows is None else rows
    return {"total_count": len(rows) if total is None else total, "workflow_runs": rows}


def provider(monkeypatch, *, inv=None, repo_runs=None, error=None, delayed=False):
    module = load()
    calls = []
    count = 0
    def request(command, **kwargs):
        nonlocal count
        assert command[:2] == ["gh", "api"]
        endpoint = command[2].removeprefix("repos/Kaidera-AI/cortex/")
        body = json.loads(kwargs["input"]) if kwargs.get("input") else None
        calls.append((endpoint, body))
        if endpoint == RUNS:
            count += 1
            if count == 1:
                code, value = 1, {"message": "Not Found", "status": "404"}
            else:
                code, value = 0, runs([{"id": 33, "head_sha": SHA}] if delayed else [])
        elif endpoint == INVENTORY:
            code, value = 0, inventory() if inv is None else inv
        elif endpoint == ALL_RUNS:
            code, value = 0, runs() if repo_runs is None else repo_runs
        elif endpoint == "git/refs":
            code, value = 0, {"ref": body["ref"], "object": {"sha": SHA}}
        else:
            raise AssertionError("unexpected provider mutation or endpoint: " + endpoint)
        if error and endpoint == error[0]:
            code, value = error[1:]
        return SimpleNamespace(returncode=code, stdout=value if isinstance(value, str) else json.dumps(value), stderr="synthetic failure" if code else "")
    monkeypatch.setattr(module.subprocess, "run", request)
    return module, module.Github([], target="linux-x86_64"), calls


@pytest.mark.parametrize("delayed", [False, True])
def test_first_registration_preserves_one_ref_wait_and_push_manual_exclusion(monkeypatch, delayed):
    module, github, calls = provider(monkeypatch, delayed=delayed)
    waits = []
    with pytest.raises(module.DispatchRefused) as error:
        module.dispatch_once(SHA, PUSH, github, waits.append, target="linux-x86_64")
    assert error.value.code == ("native_ci_run_exists" if delayed else "push_can_trigger_native_ci")
    assert waits == [30]
    assert [endpoint for endpoint, _ in calls] == [RUNS, INVENTORY, ALL_RUNS, "git/refs", RUNS]
    assert calls[3][1] == {"ref": "refs/heads/ren-cx/linux-package-build-admitted-" + SHA, "sha": SHA}


@pytest.mark.parametrize("bad", [
    [], {}, {"total_count": False, "workflows": []}, inventory(total=2),
    inventory([None]), inventory([workflow(identifier=True)]), inventory([workflow(identifier=0)]),
    inventory([workflow(path="ci.yml")]), inventory([workflow(path=".github/workflows/nested/ci.yml")]),
    inventory([workflow(name="")]), inventory([workflow(state="unknown")]),
    inventory([workflow(), workflow(identifier=20)]),
    inventory([workflow(), workflow(path=".github/workflows/other.yml")]),
    inventory([workflow(path=".github/workflows/" + WORKFLOW)]),
    inventory([workflow(name=WORKFLOW)]),
])
def test_ambiguous_incomplete_or_present_workflow_inventory_refuses_before_ref(monkeypatch, bad):
    module, github, calls = provider(monkeypatch, inv=bad)
    with pytest.raises(module.DispatchRefused) as error:
        module.dispatch_once(SHA, PUSH, github, lambda _: pytest.fail("wait reached"), target="linux-x86_64")
    assert error.value.code == "workflow_inventory_invalid"
    assert [endpoint for endpoint, _ in calls] == [RUNS, INVENTORY]


@pytest.mark.parametrize("bad,code", [
    ({}, "ci_run_binding_invalid"), ([], "ci_run_binding_invalid"),
    (runs(total=1), "ci_run_binding_invalid"),
    ({"total_count": False, "workflow_runs": []}, "ci_run_binding_invalid"),
    (runs([None]), "ci_run_binding_invalid"),
    (runs([{"id": 31, "head_sha": "b" * 40}]), "ci_run_binding_invalid"),
    (runs([{"id": 31, "head_sha": SHA, "event": "push", "status": "completed"}]), "native_ci_run_exists"),
    (runs([{"id": 32, "head_sha": SHA, "event": "workflow_dispatch", "status": "queued"}]), "native_ci_run_exists"),
])
def test_all_repository_runs_must_be_complete_exact_and_empty(monkeypatch, bad, code):
    module, github, calls = provider(monkeypatch, repo_runs=bad)
    with pytest.raises(module.DispatchRefused) as error:
        module.dispatch_once(SHA, PUSH, github, lambda _: pytest.fail("wait reached"), target="linux-x86_64")
    assert error.value.code == code
    assert [endpoint for endpoint, _ in calls] == [RUNS, INVENTORY, ALL_RUNS]


@pytest.mark.parametrize("endpoint,value", [
    (RUNS, {"message": "Forbidden", "status": "403"}),
    (RUNS, {"message": "Not Found", "status": "500"}),
    (RUNS, {"message": "Forbidden", "status": "404"}),
    (RUNS, {"message": "Not Found"}), (RUNS, "not json"),
    (INVENTORY, {"message": "Not Found", "status": "404"}),
    (ALL_RUNS, {"message": "Not Found", "status": "404"}),
])
def test_only_specific_workflow_not_found_can_enter_fallback(monkeypatch, endpoint, value):
    module, github, calls = provider(monkeypatch, error=(endpoint, 1, value))
    with pytest.raises(module.DispatchRefused) as error:
        module.dispatch_once(SHA, PUSH, github, lambda _: pytest.fail("wait reached"), target="linux-x86_64")
    assert error.value.code == "github_request_failed"
    assert calls[-1][0] == endpoint and all(body is None for _, body in calls)


def test_duplicate_provider_json_is_ambiguous_and_refuses(monkeypatch):
    module, github, calls = provider(monkeypatch, error=(INVENTORY, 0, '{"total_count":1,"total_count":0,"workflows":[]}'))
    with pytest.raises(module.DispatchRefused) as error:
        module.dispatch_once(SHA, PUSH, github, lambda _: pytest.fail("wait reached"), target="linux-x86_64")
    assert error.value.code == "github_request_failed"
    assert [endpoint for endpoint, _ in calls] == [RUNS, INVENTORY]


def test_registered_workflow_keeps_original_two_read_path(monkeypatch):
    module, github, calls = provider(monkeypatch, error=(RUNS, 0, runs()))
    with pytest.raises(module.DispatchRefused, match="push_can_trigger_native_ci"):
        module.dispatch_once(SHA, PUSH, github, lambda _: None, target="linux-x86_64")
    assert [endpoint for endpoint, _ in calls] == [RUNS, "git/refs", RUNS]


def test_second_missing_read_repeats_both_checks_without_second_ref(monkeypatch):
    module, github, calls = provider(monkeypatch, error=(RUNS, 1, {"message": "Not Found", "status": "404"}))
    with pytest.raises(module.DispatchRefused, match="push_can_trigger_native_ci"):
        module.dispatch_once(SHA, PUSH, github, lambda _: None, target="linux-x86_64")
    assert [endpoint for endpoint, _ in calls] == [RUNS, INVENTORY, ALL_RUNS, "git/refs", RUNS, INVENTORY, ALL_RUNS]


def test_success_status_with_error_object_is_not_missing_workflow(monkeypatch):
    module, github, calls = provider(monkeypatch, error=(RUNS, 0, {"message": "Not Found", "status": "404"}))
    with pytest.raises(module.DispatchRefused, match="ci_run_binding_invalid"):
        module.dispatch_once(SHA, PUSH, github, lambda _: None, target="linux-x86_64")
    assert [endpoint for endpoint, _ in calls] == [RUNS]
