"""Exact workflow bytes must remain frozen at both provider mutation points."""
from pathlib import Path
import importlib.util
import json
import sys
from types import SimpleNamespace

import pytest


SHA = "a" * 40
MANUAL = b"on:\n  workflow_dispatch:\n"


@pytest.mark.parametrize("stage", ["pre_list", "wait", "post_list", "unchanged"])
def test_cli_refuses_newline_only_workflow_drift(monkeypatch, tmp_path, stage):
    path = Path(__file__).resolve().parents[1] / "scripts/release/dispatch-once.py"
    spec = importlib.util.spec_from_file_location("byte_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tmp_path.chmod(0o700)
    workflow = tmp_path / ".github/workflows/cortex-candidate.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_bytes(MANUAL)
    inbox = tmp_path / "inbox.md"
    inbox.write_text("**▶ DO NOW (r900):** One CI on " + SHA + ".\n")
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"verdict": "PASS", "target_receipt": {"head_commit": SHA}}))
    evidence = tmp_path / "receipt.json"
    calls = []

    def git(command, **kwargs):
        assert command[:3] == ["git", "-C", str(tmp_path)]
        assert command[3:] in [["rev-parse", "HEAD"], ["status", "--porcelain"]]
        return SHA + "\n" if command[3:] == ["rev-parse", "HEAD"] else ""

    def drift():
        workflow.write_bytes(MANUAL.replace(b"\n", b"\r\n"))

    def provider(command, **kwargs):
        assert command[:2] == ["gh", "api"]
        endpoint = command[2]
        calls.append(endpoint)
        if "/runs?" in endpoint:
            count = sum("/runs?" in e for e in calls)
            if (stage == "pre_list" and count == 1) or (stage == "post_list" and count == 2):
                drift()
            response = {"total_count": 0, "workflow_runs": []}
        elif endpoint.endswith("/git/refs"):
            response = {"ref": "refs/heads/ren-cx/package-build-admitted-" + SHA, "object": {"sha": SHA}}
        else:
            assert endpoint.endswith("/dispatches")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")

    def wait(seconds):
        assert seconds == 30
        if stage == "wait":
            drift()

    module.ROOT = tmp_path
    monkeypatch.setattr(module.subprocess, "check_output", git)
    monkeypatch.setattr(module.subprocess, "run", provider)
    monkeypatch.setattr(module.time, "sleep", wait)
    monkeypatch.setattr(sys, "argv", ["dispatch-once.py", "--source-sha", SHA,
        "--review-receipt", str(review), "--inbox", str(inbox), "--evidence", str(evidence)])
    code = module.main()
    record = json.loads(evidence.read_text())
    dispatches = sum(endpoint.endswith("/dispatches") for endpoint in calls)
    if stage == "unchanged":
        assert code == 0 and record["status"] == "manual_dispatch_submitted" and dispatches == 1
    else:
        assert code == 2 and record["status"] == "manual_dispatch_refused" and dispatches == 0
        assert record["reason"] == "source_or_workflow_changed"
        assert sum(endpoint.endswith("/git/refs") for endpoint in calls) == (stage != "pre_list")
