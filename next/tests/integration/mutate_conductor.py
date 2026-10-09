"""C08 named semantic assertions; baseline and restoration use the whole stack."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from mutate_pg_search import classify_kill, clean_run

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT.parent
SUPERVISOR = ROOT / "src/cortex_core/conductor/supervisor.py"
METRICS = ROOT / "src/cortex_core/conductor/metrics.py"
SQL = ROOT / "schema/coordination/c08-supervisor-leases.sql"
MUTATIONS = [
    (
        SUPERVISOR,
        "renew_qualifies_before_lock",
        "async def renew(self):\n        token = self._token()\n        async with self._transaction() as conn:\n            await self._lock_live(conn, token)",
        "async def renew(self):\n        token = self._token()\n        async with self._transaction() as conn:",
        "test_conductor.ConductorTests.test_renew_waiting_behind_guard_refuses_expired_lease",
    ),
    (
        SUPERVISOR,
        "release_qualifies_before_lock",
        "async def release(self):\n        token = self._token()\n        async with self._transaction() as conn:\n            await self._lock_live(conn, token)",
        "async def release(self):\n        token = self._token()\n        async with self._transaction() as conn:",
        "test_conductor.ConductorTests.test_release_waiting_behind_guard_refuses_expired_lease",
    ),
    (
        SUPERVISOR,
        "second_holder_admitted",
        "WHERE coordination.supervisor_leases.expires_at <= clock_timestamp()",
        "WHERE TRUE",
        "test_conductor.ConductorTests.test_singleton_and_fence_after_expiry",
    ),
    (
        SUPERVISOR,
        "reused_fence",
        "fence=coordination.supervisor_leases.fence+1",
        "fence=coordination.supervisor_leases.fence",
        "test_conductor.ConductorTests.test_release_reclaim_never_reuses_fence",
    ),
    (
        SUPERVISOR,
        "released_row_reported_healthy",
        '"healthy" if row and row["live"] else "supervisor_down"',
        '"healthy" if row else "supervisor_down"',
        "test_conductor.ConductorTests.test_release_reclaim_never_reuses_fence",
    ),
    (
        SUPERVISOR,
        "core_outage_hidden",
        '"core_available": False',
        '"core_available": True',
        "test_conductor.ConductorTests.test_health_core_outage",
    ),
    (
        METRICS,
        "wrong_component_label",
        'component="cortex"',
        'component="other"',
        "test_conductor.MetricTests.test_closed_families_labels_and_actual_timing",
    ),
    (
        METRICS,
        "wrong_search_family",
        '"cortex_search_latency_seconds"',
        '"cortex_search_latency_ms"',
        "test_conductor.MetricTests.test_closed_families_labels_and_actual_timing",
    ),
    (
        METRICS,
        "fabricated_db_size",
        'self._values["cortex_db_bytes"] = db_bytes',
        'self._values["cortex_db_bytes"] = 0',
        "test_conductor.ConductorTests.test_metrics_real_bytes_and_supplied_backlog",
    ),
    (
        METRICS,
        "unsafe_label_admitted",
        'not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", v)',
        "False",
        "test_conductor.MetricTests.test_unsafe_labels_and_nonfinite_values_refused",
    ),
    (
        SQL,
        "rls_not_forced",
        "ALTER TABLE coordination.supervisor_leases FORCE ROW LEVEL SECURITY;",
        "",
        "test_conductor.ConductorTests.test_private_control_table_and_installation_binding",
    ),
    (
        SQL,
        "fence_regression_allowed",
        "IF NEW.fence < OLD.fence THEN",
        "IF FALSE THEN",
        "test_conductor.ConductorTests.test_direct_fence_regression_refused",
    ),
    (
        SQL,
        "unknown_installation_admitted",
        "REFERENCES core.installations(id)",
        "",
        "test_conductor.ConductorTests.test_private_control_table_and_installation_binding",
    ),
]


def git_output(*args):
    # Popen keeps source inspection separate from the mockable test child runner.
    with subprocess.Popen(
        ["git", "-C", str(REPO), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        output, error = process.communicate()
        if process.returncode:
            raise RuntimeError("INCONCLUSIVE: source inspection failed: " + error.decode())
        return output


def source_snapshot():
    paths = git_output("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "next")
    files = {}
    for name in sorted(set(paths.decode().split("\0")) - {""}):
        data = (REPO / name).read_bytes()
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return {
        "scope": "all tracked and nonignored untracked files under next/",
        "head": git_output("rev-parse", "HEAD").decode().strip(),
        "git_tree": git_output("rev-parse", "HEAD^{tree}").decode().strip(),
        "tree_sha256": hashlib.sha256(canonical).hexdigest(),
        "git_status": git_output("status", "--porcelain").decode(),
        "files": files,
    }


def execute(evidence, name, target=None):
    before = source_snapshot()
    env = dict(os.environ)
    env.pop("SEARCH_TEST_TARGET", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = str(evidence / (name + "-unused-pycache"))
    if target:
        env["SEARCH_TEST_TARGET"] = target
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests/integration/run_conductor.py")],
        capture_output=True,
        text=True,
        env=env,
    )
    output = result.stdout + result.stderr
    (evidence / (name + ".log")).write_text(
        output + "\nexit=" + str(result.returncode) + "\n"
    )
    after = source_snapshot()
    stable = all(before[key] == after[key] for key in ("head", "git_tree", "tree_sha256"))
    (evidence / (name + ".source.json")).write_text(json.dumps({
        "before": before, "after": after, "source_stable": stable,
        "target": target, "exit": result.returncode,
        "raw_output_sha256": hashlib.sha256((evidence / (name + ".log")).read_bytes()).hexdigest(),
    }, indent=2) + "\n")
    if not stable:
        raise RuntimeError("INCONCLUSIVE: source changed during the check")
    return result.returncode, output


def run():
    evidence = Path(sys.argv[1])
    evidence.mkdir(parents=True, exist_ok=True)
    receipts = []
    code, output = execute(evidence, "baseline")
    if not clean_run(code, output):
        (evidence / "baseline.json").write_text(
            json.dumps({"status": "INCONCLUSIVE", "exit": code}) + "\n"
        )
        raise RuntimeError("INCONCLUSIVE: clean full baseline required before edits")
    baseline = json.loads((evidence / "baseline.source.json").read_text())["before"]
    if baseline["git_status"]:
        raise RuntimeError("INCONCLUSIVE: commit and clean the source tree before mutation proof")
    for path, name, before, after, target in MUTATIONS:
        original = path.read_bytes()
        text = original.decode()
        if text.count(before) != 1:
            raise RuntimeError("Mutation drift: " + name)
        try:
            path.write_text(text.replace(before, after))
            code, output = execute(evidence, name, target)
            status = classify_kill(code, output, target)
            packet = json.loads((evidence / (name + ".source.json")).read_text())
            receipts.append(
                {
                    "mutation": name,
                    "file": str(path.relative_to(REPO)),
                    "target": target,
                    "status": status,
                    "killed": status == "KILLED",
                    "exit": code,
                    "head": packet["before"]["head"],
                    "baseline_tree_sha256": baseline["tree_sha256"],
                    "mutant_tree_sha256": packet["before"]["tree_sha256"],
                    "original_file_sha256": hashlib.sha256(original).hexdigest(),
                    "mutant_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "recipe": {"before": before, "after": after},
                    "raw_output_sha256": packet["raw_output_sha256"],
                }
            )
            (evidence / "mutations.json").write_text(
                json.dumps(receipts, indent=2) + "\n"
            )
            print(name, status, flush=True)
            if status != "KILLED":
                raise RuntimeError(status + ": " + name)
        finally:
            path.write_bytes(original)
    code, output = execute(evidence, "restored")
    if not clean_run(code, output):
        raise RuntimeError("INCONCLUSIVE: restored full suite failed")
    restored = json.loads((evidence / "restored.source.json").read_text())["before"]
    if restored["tree_sha256"] != baseline["tree_sha256"]:
        raise RuntimeError("INCONCLUSIVE: restored source differs from baseline")
    print(
        f"{len(receipts)}/{len(MUTATIONS)} expected-assertion semantic mutants killed",
        flush=True,
    )


if __name__ == "__main__":
    run()
