"""X01a source-copy faults; only expected test-body assertions count."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "next/src/cortex_core/recovery_fixture.py"
TESTS = ROOT / "next/tests/recovery_fixture"
RUNNER = ROOT / "next/tests/test_receipts.py"
source = SOURCE.read_text()
source_sha = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
cases = [
    ("fault_root", "if self.directory == fault or self.directory in fault.parents or fault in self.directory.parents:",
     "if False:", "test_fixture.LedgerTests.test_external_ledger_rejects_faulted_root_and_symlink"),
    ("server_commit", 'result.get("state") != "committed"', "False",
     "test_fixture.LedgerTests.test_only_matching_server_confirmed_commit_enters_ack_chain"),
    ("receipt_match", 'any(result.get(key) != request[key] for key in _FIELDS)', "False",
     "test_fixture.LedgerTests.test_only_matching_server_confirmed_commit_enters_ack_chain"),
    ("chain_digest", 'if digest != record["sha256"]:', "if False:",
     "test_fixture.LedgerTests.test_single_entry_content_hash_is_checked"),
    ("ambiguous_split", 'return self._append("ambiguous", {**request, "reason": reason})',
     'return self._append("acks", {**request, "reason": reason})',
     "test_fixture.LedgerTests.test_ambiguous_is_separate_and_can_later_resolve"),
    ("ack_prefix", 'restored != {row["request_id"] for row in acks[:prefix]}', "False",
     "test_fixture.ReconciliationTests.test_noncontiguous_or_altered_recovery_is_refused"),
    ("rpo_gate", "rpo_ns <= rpo_limit_ns and rto_ns <= rto_limit_ns",
     "True and rto_ns <= rto_limit_ns", "test_fixture.ReconciliationTests.test_rpo_or_rto_miss_is_red_not_averaged"),
    ("edition_os", "os_name != expected_os or arch != expected_arch", "False",
     "test_fixture.NativeAdapterTests.test_wrong_platform_or_shared_volume_refused_before_plan"),
    ("volume_isolation", "len({source_volume, archive_volume, target_volume, target_container}) != 4",
     "False", "test_fixture.NativeAdapterTests.test_wrong_platform_or_shared_volume_refused_before_plan"),
    ("wal_verify", '"-q", "-w", "/wal", "/pgdata"', '"-q", "-n", "/pgdata"',
     "test_fixture.NativeAdapterTests.test_both_editions_render_native_dry_run_without_host_execution"),
    ("two_tenants", "for number in (1, 2):", "for number in (1,):",
     "test_fixture.FaultFixtureTests.test_synthetic_fixture_is_deterministic_and_cross_domain"),
    ("owned_fault", "any(not isinstance(name, str) or not _RESOURCE.fullmatch(name) for name in names)",
     "False", "test_fixture.FaultFixtureTests.test_faults_only_name_owned_synthetic_primary"),
    ("timeout_not_ack", '        return "ambiguous"', '        return "committed"',
     "test_fixture.LedgerTests.test_controller_timeout_is_ambiguous_not_ack"),
    ("fixture_member", 'record_id not in {row.get("record_id") for row in fixture["records"]',
     'False and record_id not in {row.get("record_id") for row in fixture["records"]',
     "test_fixture.FaultFixtureTests.test_faults_only_name_owned_synthetic_primary"),
    ("detached_recovery", '"podman", "run", "--detach", "--name"',
     '"podman", "run", "--name"',
     "test_fixture.NativeAdapterTests.test_both_editions_render_native_dry_run_without_host_execution"),
]
rows = []
for name, before, after, expected in cases:
    assert source.count(before) == 1, name
    with tempfile.TemporaryDirectory(prefix="cox-x01a-fault-") as folder:
        module = Path(folder) / "cortex_core"
        module.mkdir()
        mutated = module / SOURCE.name
        mutated.write_text(source.replace(before, after, 1))
        env = os.environ.copy()
        env["PYTHONPATH"] = folder + os.pathsep + str(ROOT / "next/src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(["python3.12", str(RUNNER), str(TESTS)],
                                capture_output=True, text=True, env=env)
        lines = [line.split("=", 1)[1] for line in result.stdout.splitlines()
                 if line.startswith("CORTEX_TEST_RESULT=")]
        receipt = json.loads(lines[0]) if len(lines) == 1 else None
        failures = receipt["failures"] if receipt else []
        killed = (result.returncode == 1 and receipt is not None
                  and receipt["tests_run"] == 15 and not receipt["errors"]
                  and any(row["id"].split(" (")[0] == expected for row in failures)
                  and all(row["phase"] == "test" and row["is_assertion"]
                          and row["phase_attribution"] == "traceback+active-unittest-v2"
                          for row in failures))
        row = {"name": name, "status": "killed" if killed else "inconclusive",
               "expected_test": expected, "exit_code": result.returncode,
               "receipt": receipt, "stdout": result.stdout, "stderr": result.stderr,
               "mutated_sha256": hashlib.sha256(mutated.read_bytes()).hexdigest()}
        rows.append(row)
        print(json.dumps({"name": name, "status": row["status"],
                          "failures": len(failures),
                          "errors": len(receipt["errors"]) if receipt else None}), flush=True)
assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == source_sha
print(json.dumps({"source_sha256": source_sha, "mutants": len(rows),
                  "killed": sum(row["status"] == "killed" for row in rows),
                  "inconclusive": [row["name"] for row in rows if row["status"] != "killed"],
                  "rows": rows}), flush=True)
if any(row["status"] != "killed" for row in rows):
    raise SystemExit(1)
