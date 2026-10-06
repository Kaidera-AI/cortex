"""R171 behavioral single-dispatch contract; every GitHub call is synthetic."""
from pathlib import Path
import importlib.util

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
BRANCH = "ren-cx/package-build-admitted-" + SHA
PUSH = 'on:\n  push:\n    branches: ["ren-cx/package-build-admitted-*"]\n  workflow_dispatch:\n'
MANUAL = 'on:\n  workflow_dispatch:\n'


@pytest.fixture
def subject():
    spec = importlib.util.spec_from_file_location("dispatch_once", ROOT / "scripts/release/dispatch-once.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Github:
    def __init__(self, *, pre=(), post=(), ref_failure=False):
        self.pre, self.post = list(pre), list(post)
        self.calls = []
        self.elapsed = 0
        self.ref_failure = ref_failure

    def list_runs(self, sha):
        self.calls.append(("list", sha, self.elapsed))
        return self.post if self.elapsed else self.pre

    def create_ref(self, branch, sha):
        self.calls.append(("ref", branch, sha))
        if self.ref_failure:
            raise RuntimeError("synthetic ref failure")

    def wait(self, seconds):
        self.calls.append(("wait", seconds))
        self.elapsed += seconds

    def dispatch(self, branch):
        self.calls.append(("dispatch", branch))


def run(subject, workflow, github, sha=SHA):
    return subject.dispatch_once(sha, workflow, github, github.wait)


def refused(subject, workflow, github, code, sha=SHA):
    with pytest.raises(subject.DispatchRefused) as exc:
        run(subject, workflow, github, sha)
    assert exc.value.code == code
    assert not any(call[0] == "dispatch" for call in github.calls)
    return github.calls


def test_delayed_push_appears_after_ref_wait_and_relist(subject):
    github = Github(post=[{"id": 17, "head_sha": SHA, "event": "push"}])
    calls = refused(subject, PUSH, github, "native_ci_run_exists")
    assert calls == [("list", SHA, 0), ("ref", BRANCH, SHA), ("wait", 30), ("list", SHA, 30)]


def test_push_may_start_after_empty_observation_window(subject):
    github = Github()
    calls = refused(subject, PUSH, github, "push_can_trigger_native_ci")
    assert calls == [("list", SHA, 0), ("ref", BRANCH, SHA), ("wait", 30), ("list", SHA, 30)]


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_existing_completed_or_pending_run_refuses_before_ref(subject, event):
    github = Github(pre=[{"id": 18, "head_sha": SHA, "event": event, "status": "completed"}])
    assert refused(subject, MANUAL, github, "native_ci_run_exists") == [("list", SHA, 0)]


def test_manual_only_control_dispatches_once_after_wait_and_relist(subject):
    github = Github()
    assert run(subject, MANUAL, github) == {"status": "manual_dispatch_submitted", "source_sha": SHA, "branch": BRANCH}
    assert github.calls == [("list", SHA, 0), ("ref", BRANCH, SHA), ("wait", 30), ("list", SHA, 30), ("dispatch", BRANCH)]


@pytest.mark.parametrize("workflow", ['on: push\n', 'on: [push, workflow_dispatch]\n', 'on:\n  push:\n', 'on:\n  push: {}\n'])
def test_unfiltered_push_is_also_fenced(subject, workflow):
    refused(subject, workflow, Github(), "push_can_trigger_native_ci")


@pytest.mark.parametrize("pattern", [BRANCH, "ren-cx/package-build-admitted-*", "*"])
def test_matching_literal_or_prefix_push_predicate(subject, pattern):
    workflow = f'on:\n  push:\n    branches: ["{pattern}"]\n  workflow_dispatch:\n'
    refused(subject, workflow, Github(), "push_can_trigger_native_ci")


def test_known_nonmatching_branch_control_is_manual_only_for_this_ref(subject):
    workflow = 'on:\n  push:\n    branches: ["main"]\n  workflow_dispatch:\n'
    github = Github()
    assert run(subject, workflow, github)["status"] == "manual_dispatch_submitted"
    assert [call[0] for call in github.calls].count("dispatch") == 1


@pytest.mark.parametrize("workflow", ["{}", "on: 42", "on: [42]", "on: {push: {branches: main}}", "on: {push: {branches: []}}", "on: {push: {branches-ignore: [main]}}", "on: {push: {tags: ['*']}}", "on: {push: {branches: ['!main']}}", "on: {push: {branches: ['x+y']}}", "on: {push: {branches: ['x?y']}}", "on: {push: {branches: ['x[ab]']}}", "on: {push: {branches: ['x**y']}}", "on: {push: {paths: ['src/**']}}", "on: ["])
def test_unknown_workflow_predicate_refuses_without_mutation(subject, workflow):
    github = Github()
    assert refused(subject, workflow, github, "workflow_push_predicate_unknown") == []


@pytest.mark.parametrize("sha", ["", "A" * 40, "a" * 39, "z" * 40, "a" * 40 + "/other"])
def test_invalid_sha_never_reaches_github(subject, sha):
    github = Github()
    assert refused(subject, MANUAL, github, "source_sha_invalid", sha) == []


@pytest.mark.parametrize("stage", ["pre", "post"])
@pytest.mark.parametrize("bad", [[{"id": 9, "head_sha": "b" * 40}], {"workflow_runs": []}, [None]])
def test_run_binding_or_shape_failure_never_dispatches(subject, stage, bad):
    github = Github()
    setattr(github, stage, bad)
    refused(subject, MANUAL, github, "ci_run_binding_invalid")
    if stage == "pre":
        assert github.calls == [("list", SHA, 0)]


def test_ref_creation_failure_is_not_hidden_or_retried(subject):
    github = Github(ref_failure=True)
    with pytest.raises(RuntimeError, match="synthetic ref failure"):
        run(subject, MANUAL, github)
    assert github.calls == [("list", SHA, 0), ("ref", BRANCH, SHA)]


def test_actual_pinned_workflow_predicate_blocks_manual_even_with_no_visible_run(subject):
    workflow = (ROOT / ".github/workflows/cortex-candidate.yml").read_text()
    refused(subject, workflow, Github(), "push_can_trigger_native_ci")
