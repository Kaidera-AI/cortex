"""Additive R171 refusals; all provider and git calls are synthetic."""
from pathlib import Path
import importlib.util
import json
import sys
from types import SimpleNamespace

import pytest


SHA = "a" * 40
BRANCH = "ren-cx/package-build-admitted-" + SHA
MANUAL = "on: {workflow_dispatch: {}}\n"
PUSH = 'on: {push: {branches: ["ren-cx/package-build-admitted-*"]}}\n'


def subject():
    path = Path(__file__).resolve().parents[1] / "scripts/release/dispatch-once.py"
    spec = importlib.util.spec_from_file_location("guard_refusals", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("workflow", [
    PUSH + MANUAL,
    'on: {push: {}, push: {branches: [main]}, workflow_dispatch: {}}',
    'on: {push: {branches: ["*"], branches: [main]}, workflow_dispatch: {}}',
    'on: {workflow_dispatch: {}, workflow_dispatch: {}}',
    'on: {workflow_dispatch: [unexpected]}',
    'on: {workflow_dispatch: unexpected}',
    'on: {push: {}, workflow_dispatch: [unexpected]}',
])
def test_ambiguous_mapping_or_manual_event_refuses_before_provider(workflow):
    module = subject()
    with pytest.raises(module.DispatchRefused) as error:
        module.dispatch_once(SHA, workflow, None, None)
    assert error.value.code == "workflow_push_predicate_unknown"


@pytest.mark.parametrize("response", [None, [], {"object": None},
    {"object": {"sha": SHA}}, {"ref": "refs/heads/other", "object": {"sha": SHA}},
    {"ref": "refs/heads/" + BRANCH, "object": {"sha": "b" * 40}},
    {"ref": "refs/heads/" + BRANCH, "object": []}])
def test_created_ref_requires_typed_exact_name_and_sha(response):
    module = subject()
    github = module.Github([])
    github.request = lambda *args: response
    with pytest.raises(module.DispatchRefused) as error:
        github.create_ref(BRANCH, SHA)
    assert error.value.code == "created_ref_binding_invalid"


@pytest.mark.parametrize("response", [None, [], {"total_count": 0},
    {"total_count": 0, "workflow_runs": None}, {"total_count": 0, "workflow_runs": {}},
    {"total_count": False, "workflow_runs": []}])
def test_run_response_requires_typed_complete_list(response):
    module = subject()
    github = module.Github([])
    github.request = lambda *args: response
    with pytest.raises(module.DispatchRefused) as error:
        github.list_runs(SHA)
    assert error.value.code == "ci_run_binding_invalid"


def cli(monkeypatch, tmp_path, *, change=None, stage="pre", admission=None,
        review=None, ref=None):
    module = subject()
    tmp_path.chmod(0o700)
    workflow = tmp_path / ".github/workflows/cortex-candidate.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(MANUAL)
    inbox = tmp_path / "inbox.md"
    line = admission or "**▶ DO NOW (r900):** One CI on " + SHA + "."
    inbox.write_text(line + "\n")
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review if review is not None else
        {"verdict": "PASS", "target_receipt": {"head_commit": SHA}}))
    evidence = tmp_path / "receipt.json"
    calls, state = [], {"changed": False}

    def drift():
        state["changed"] = True
        if change == "inbox":
            inbox.write_text("**▶ DO NOW (r901):** STOP; CI withdrawn.\n" + line + "\n")
        elif change == "review":
            review_path.write_text(json.dumps({"verdict": "REWORK"}))
        elif change == "workflow":
            workflow.write_text(PUSH)

    def git(command, **kwargs):
        assert command[:3] == ["git", "-C", str(tmp_path)]
        if command[3:] == ["rev-parse", "HEAD"]:
            return ("b" * 40 if state["changed"] and change == "head" else SHA) + "\n"
        assert command[3:] == ["status", "--porcelain"]
        return " M source.py\n" if state["changed"] and change == "dirty" else ""

    def provider(command, **kwargs):
        assert command[:2] == ["gh", "api"]
        endpoint = command[2]
        body = json.loads(kwargs["input"]) if kwargs.get("input") else None
        calls.append((endpoint, body))
        if "/runs?" in endpoint:
            if len(calls) == 1 and stage == "pre":
                drift()
            response = {"total_count": 0, "workflow_runs": []}
        elif endpoint.endswith("/git/refs"):
            response = ref if ref is not None else {"ref": "refs/heads/" + BRANCH, "object": {"sha": SHA}}
        else:
            assert endpoint.endswith("/dispatches")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")

    def wait(seconds):
        assert seconds == 30
        if stage == "post":
            drift()

    module.ROOT = tmp_path
    monkeypatch.setattr(module.subprocess, "check_output", git)
    monkeypatch.setattr(module.subprocess, "run", provider)
    monkeypatch.setattr(module.time, "sleep", wait)
    monkeypatch.setattr(sys, "argv", ["dispatch-once.py", "--source-sha", SHA,
        "--review-receipt", str(review_path), "--inbox", str(inbox), "--evidence", str(evidence)])
    code = module.main()
    return code, json.loads(evidence.read_text()), calls


@pytest.mark.parametrize("change", ["inbox", "review", "workflow", "head", "dirty"])
@pytest.mark.parametrize("stage", ["pre", "post"])
def test_authority_and_source_drift_refuses_next_mutation(monkeypatch, tmp_path, change, stage):
    code, record, calls = cli(monkeypatch, tmp_path, change=change, stage=stage)
    assert code == 2 and record["status"] == "manual_dispatch_refused"
    assert not any(endpoint.endswith("/dispatches") for endpoint, _ in calls)
    assert sum(endpoint.endswith("/git/refs") for endpoint, _ in calls) == (stage == "post")


@pytest.mark.parametrize("text", ["STOP; CI on ", "WAIT; CI on ", "No CI on ",
    "CI not admitted on ", "Do not dispatch CI on ", "No native CI on "])
def test_explicit_stop_wait_or_negative_ci_is_not_input_admission(monkeypatch, tmp_path, text):
    code, _, calls = cli(monkeypatch, tmp_path, admission="**▶ DO NOW (r900):** " + text + SHA)
    assert code == 2 and calls == []


@pytest.mark.parametrize("binding", [SHA[:10], SHA[:10] + "b" * 30])
def test_admission_requires_full_exact_sha_not_shared_prefix(monkeypatch, tmp_path, binding):
    code, _, calls = cli(monkeypatch, tmp_path, admission="**▶ DO NOW (r900):** One CI on " + binding)
    assert code == 2 and calls == []


@pytest.mark.parametrize("review", [[], {"verdict": "PASS", "target_receipt": []},
    {"verdict": "PASS", "target_receipt": None}])
def test_malformed_review_produces_refusal_receipt(monkeypatch, tmp_path, review):
    code, record, calls = cli(monkeypatch, tmp_path, review=review)
    assert code == 2 and record["status"] == "manual_dispatch_refused" and calls == []


def test_manual_only_cli_control_submits_exactly_once(monkeypatch, tmp_path):
    code, record, calls = cli(monkeypatch, tmp_path)
    assert code == 0 and record["status"] == "manual_dispatch_submitted"
    assert sum(endpoint.endswith("/git/refs") for endpoint, _ in calls) == 1
    assert sum(endpoint.endswith("/dispatches") for endpoint, _ in calls) == 1


def test_exact_created_ref_control():
    module = subject()
    github = module.Github([])
    github.request = lambda *args: {"ref": "refs/heads/" + BRANCH, "object": {"sha": SHA}}
    assert github.create_ref(BRANCH, SHA) is None
