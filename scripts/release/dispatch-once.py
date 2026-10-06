"""Guard one admitted native CI submission from a clean source checkout.

Use the locked source test group (PyYAML is already pinned). The operator must
have Vera's exact source PASS and Kai's fresh CI admission. Inbox input checks
bind the full SHA and reject explicit stop/negative inputs; they cannot establish
authorization from arbitrary prose. A matching push
predicate creates the ref once, waits/re-lists, and REFUSES manual dispatch.
Observe that push run. This does not authorize a build, retry or publication.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import subprocess
import time

import yaml


ROOT = Path(__file__).resolve().parents[2]
REPO = "Kaidera-AI/cortex"
WORKFLOW = "cortex-candidate.yml"
PREFIX = "ren-cx/package-build-admitted-"


class DispatchRefused(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class WorkflowMissing(DispatchRefused):
    """Only the selected workflow's literal provider 404 enters registration."""


def route(target):
    routes = {"macos-arm64": (WORKFLOW, PREFIX),
              "linux-x86_64": ("cortex-linux-candidate.yml", "ren-cx/linux-package-build-admitted-")}
    if not isinstance(target, str) or target not in routes:
        raise DispatchRefused("native_target_invalid")
    return routes[target]


def check_routes(texts, target, sha):
    selected, prefix = route(target)
    if selected not in texts:
        raise DispatchRefused("workflow_route_missing")
    for name, text in texts.items():
        can_push = push_can_trigger(text, prefix + sha)
        if name != selected and can_push:
            raise DispatchRefused("workflow_route_overlap")


class WorkflowLoader(yaml.BaseLoader):
    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate workflow mapping key")
        return super().construct_mapping(node, deep=deep)


def push_can_trigger(workflow_text, branch):
    try:
        # BaseLoader retains the GitHub YAML 'on' key and constructs no objects.
        document = yaml.load(workflow_text, Loader=WorkflowLoader)
        events = document["on"]
        if isinstance(events, str):
            if events not in ("push", "workflow_dispatch"):
                raise ValueError
            return events == "push"
        if isinstance(events, list):
            if not events or any(e not in ("push", "workflow_dispatch") for e in events):
                raise ValueError
            return "push" in events
        if not isinstance(events, dict) or not events or set(events) - {"push", "workflow_dispatch"}:
            raise ValueError
        if "workflow_dispatch" in events and events["workflow_dispatch"] != "" and not isinstance(events["workflow_dispatch"], dict):
            raise ValueError
        if "push" not in events:
            return False
        push = events["push"]
        if push == "" or push == {}:
            return True
        if not isinstance(push, dict) or set(push) != {"branches"}:
            raise ValueError
        patterns = push["branches"]
        if not isinstance(patterns, list) or not patterns:
            raise ValueError
        matched = False
        for pattern in patterns:
            if not isinstance(pattern, str) or not re.fullmatch(r"(?:[A-Za-z0-9_./-]+\*?|\*)", pattern):
                raise ValueError
            matched |= branch.startswith(pattern[:-1]) if pattern.endswith("*") else branch == pattern
        return matched
    except (KeyError, TypeError, ValueError, yaml.YAMLError):
        raise DispatchRefused("workflow_push_predicate_unknown") from None


def check_runs(runs, sha):
    if not isinstance(runs, list) or any(not isinstance(r, dict) or r.get("head_sha") != sha for r in runs):
        raise DispatchRefused("ci_run_binding_invalid")
    if runs:
        raise DispatchRefused("native_ci_run_exists")


def dispatch_once(sha, workflow_text, github, wait, target="macos-arm64"):
    workflow, prefix = route(target)
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise DispatchRefused("source_sha_invalid")
    branch = prefix + sha
    can_push = push_can_trigger(workflow_text, branch)
    check_runs(github.list_runs(sha), sha)
    github.create_ref(branch, sha)
    wait(30)
    check_runs(github.list_runs(sha), sha)
    if can_push:
        raise DispatchRefused("push_can_trigger_native_ci")
    github.dispatch(branch)
    result = {"status": "manual_dispatch_submitted", "source_sha": sha, "branch": branch}
    if target != "macos-arm64":
        result["workflow"] = workflow
    return result


class Github:
    def __init__(self, trace, target="macos-arm64"):
        self.trace = trace
        self.workflow, self.prefix = route(target)

    def request(self, endpoint, body=None):
        command = ["gh", "api", f"repos/{REPO}/{endpoint}"]
        if body is not None:
            command += ["--method", "POST", "--input", "-"]
        result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                                capture_output=True, text=True)
        self.trace.append({"endpoint": endpoint, "method": "POST" if body is not None else "GET",
                           "exit_code": result.returncode})
        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("ambiguous provider JSON")
                value[key] = item
            return value
        def invalid_constant(value):
            raise ValueError("non-JSON provider constant")
        try:
            response = (json.loads(result.stdout, object_pairs_hook=unique_object,
                                   parse_constant=invalid_constant)
                        if result.stdout.strip() else None)
        except (ValueError, TypeError):
            raise DispatchRefused("github_request_failed") from None
        if result.returncode:
            if (body is None and endpoint.startswith(f"actions/workflows/{self.workflow}/runs?")
                    and isinstance(response, dict) and response.get("message") == "Not Found"
                    and response.get("status") == "404"):
                raise WorkflowMissing("github_request_failed")
            raise DispatchRefused("github_request_failed")
        return response

    def confirm_missing_workflow(self):
        response = self.request("actions/workflows?per_page=100")
        if (not isinstance(response, dict) or not isinstance(response.get("workflows"), list)
                or type(response.get("total_count")) is not int
                or response["total_count"] != len(response["workflows"])):
            raise DispatchRefused("workflow_inventory_invalid")
        identifiers, paths = set(), set()
        for workflow in response["workflows"]:
            if (not isinstance(workflow, dict) or type(workflow.get("id")) is not int
                    or workflow["id"] <= 0 or workflow["id"] in identifiers
                    or not isinstance(workflow.get("path"), str)
                    or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", workflow["path"])
                    or workflow["path"] in paths or not isinstance(workflow.get("name"), str)
                    or not workflow["name"].strip() or workflow["name"] == self.workflow
                    or workflow["path"] == ".github/workflows/" + self.workflow
                    or workflow.get("state") not in ("active", "deleted", "disabled_fork",
                                                      "disabled_inactivity", "disabled_manually")):
                raise DispatchRefused("workflow_inventory_invalid")
            identifiers.add(workflow["id"])
            paths.add(workflow["path"])

    def list_runs(self, sha):
        try:
            response = self.request(f"actions/workflows/{self.workflow}/runs?head_sha={sha}&per_page=100")
        except WorkflowMissing:
            # Fail closed on incomplete inventories (including pagination).
            # A 404 alone never proves absence or licenses a provider mutation.
            self.confirm_missing_workflow()
            response = self.request(f"actions/runs?head_sha={sha}&per_page=100")
            self.check_run_response(response)
            check_runs(response["workflow_runs"], sha)
        self.check_run_response(response)
        return response["workflow_runs"]

    @staticmethod
    def check_run_response(response):
        if (not isinstance(response, dict) or not isinstance(response.get("workflow_runs"), list)
                or type(response.get("total_count")) is not int
                or response["total_count"] != len(response["workflow_runs"])):
            raise DispatchRefused("ci_run_binding_invalid")

    def create_ref(self, branch, sha):
        response = self.request("git/refs", {"ref": "refs/heads/" + branch, "sha": sha})
        if (not isinstance(response, dict) or response.get("ref") != "refs/heads/" + branch
                or not isinstance(response.get("object"), dict) or response["object"].get("sha") != sha):
            raise DispatchRefused("created_ref_binding_invalid")

    def dispatch(self, branch):
        self.request(f"actions/workflows/{self.workflow}/dispatches", {"ref": branch})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("macos-arm64", "linux-x86_64"), default="macos-arm64")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--review-receipt", type=Path, required=True)
    parser.add_argument("--inbox", type=Path, required=True, help="fresh operator DO NOW, naming the full CI SHA")
    parser.add_argument("--evidence", type=Path, required=True, help="new public receipt file; never overwritten")
    args = parser.parse_args()
    trace = []
    record = {"source_sha": args.source_sha, "target": args.target, "calls": trace}
    with args.evidence.open("x") as stream:
        try:
            if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha):
                raise DispatchRefused("source_sha_invalid")
            review_bytes = args.review_receipt.read_bytes()
            review = json.loads(review_bytes)
            if (not isinstance(review, dict) or review.get("verdict") != "PASS"
                    or not isinstance(review.get("target_receipt"), dict)
                    or review["target_receipt"].get("head_commit") != args.source_sha):
                raise DispatchRefused("exact_source_review_required")
            def active_instruction():
                return next(line for line in args.inbox.read_text().splitlines() if line.startswith("**▶ DO NOW"))
            admission = active_instruction()
            negative = r"\b(?:STOP|WAIT)\b|\bno\s+(?:native\s+)?CI\b|\bCI\s+not\b|\bnot\s+dispatch\s+(?:native\s+)?CI\b"
            if (not re.search(r"(?<![0-9a-f])" + args.source_sha + r"(?![0-9a-f])", admission)
                    or "CI" not in admission or re.search(negative, admission, re.IGNORECASE)):
                raise DispatchRefused("fresh_ci_admission_required")
            def git(*parts):
                return subprocess.check_output(["git", "-C", str(ROOT), *parts], text=True).strip()
            if git("rev-parse", "HEAD") != args.source_sha or git("status", "--porcelain"):
                raise DispatchRefused("clean_exact_source_required")
            selected, _ = route(args.target)
            workflow_path = ROOT / ".github/workflows" / selected
            workflow_bytes = workflow_path.read_bytes()
            workflow = workflow_bytes.decode("utf-8")
            # The legacy Mac-only fixture remains valid. Linux always requires
            # its Mac counterpart. Bind absence too, so later additions refuse.
            route_bytes = {}
            for target in ("macos-arm64", "linux-x86_64"):
                name, _ = route(target)
                path = ROOT / ".github/workflows" / name
                route_bytes[path] = path.read_bytes() if path.exists() else None
                if route_bytes[path] is None and args.target == "linux-x86_64":
                    raise OSError("native workflow counterpart missing")
            check_routes({p.name: b.decode("utf-8") for p, b in route_bytes.items() if b is not None}, args.target, args.source_sha)
            tool_path = Path(__file__)
            tool_bytes = tool_path.read_bytes()
            record.update(review_sha256=hashlib.sha256(review_bytes).hexdigest(), admission=admission)
            record["workflow_sha256"] = {p.name: hashlib.sha256(b).hexdigest() for p, b in route_bytes.items() if b is not None}
            github = Github(trace, target=args.target)
            # Recheck the active instruction immediately before each mutation.
            original_request = github.request
            def request(endpoint, body=None):
                if body is not None:
                    if active_instruction() != admission:
                        raise DispatchRefused("ci_admission_changed")
                    if args.review_receipt.read_bytes() != review_bytes:
                        raise DispatchRefused("review_receipt_changed")
                    if git("rev-parse", "HEAD") != args.source_sha or git("status", "--porcelain"):
                        raise DispatchRefused("clean_exact_source_required")
                    current = {p: p.read_bytes() if p.exists() else None for p in route_bytes}
                    if tool_path.read_bytes() != tool_bytes or current != route_bytes:
                        raise DispatchRefused("source_or_workflow_changed")
                return original_request(endpoint, body)
            github.request = request
            record.update(dispatch_once(args.source_sha, workflow, github, time.sleep, target=args.target))
            code = 0
        except DispatchRefused as exc:
            record.update(status="manual_dispatch_refused", reason=exc.code)
            code = 2
        except (OSError, ValueError, KeyError, TypeError, StopIteration, subprocess.SubprocessError):
            record.update(status="manual_dispatch_refused", reason="operator_input_or_provider_failed")
            code = 2
        stream.write(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
