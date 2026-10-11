"""Independent receipt check for X01a; reads committed Git bytes and raw logs."""

import hashlib
import json
from pathlib import Path
import subprocess


EVIDENCE = Path(__file__).resolve().parent
REPO = Path("/Users/amadmalik/DevVault/helix/cortex")
COMMIT = "27867d8552018e2b3dbfdcf87aa6c5cb64b25351"
BASE = "1e44f64d373806019e98fe02265e3368b301b940"
SOURCE = "next/src/cortex_core/recovery_fixture.py"


def git(*args):
    return subprocess.check_output(["git", "-C", str(REPO), *args])


def receipt(filename):
    lines = [line.split("=", 1)[1] for line in (EVIDENCE / filename).read_text().splitlines()
             if line.startswith("CORTEX_TEST_RESULT=")]
    assert len(lines) == 1, filename
    return json.loads(lines[0])


assert git("merge-base", COMMIT, BASE).decode().strip() == BASE
committed = git("show", f"{COMMIT}:{SOURCE}")
source_sha = hashlib.sha256(committed).hexdigest()
checks = {}
for name, expected in (("green-py312.txt", 15), ("green-py314.txt", 15),
                       ("inherited-o01a-py312.txt", 6),
                       ("inherited-o01b-py312.txt", 14)):
    value = receipt(name)
    assert value["tests_run"] == expected and not value["failures"] and not value["errors"], name
    checks[name] = expected
for name in ("red-003.txt", "fault-red-001.txt"):
    body = (EVIDENCE / name).read_text()
    assert "\nFAIL: " in body and "FAILED (failures=" in body
    assert "\nERROR: " not in body and "errors=" not in body
for name in ("controller-red-001.txt", "fixture-membership-red-001.txt",
             "detach-red-001.txt"):
    value = receipt(name)
    assert value["failures"] and not value["errors"], name
    assert all(row["phase"] == "test" and row["is_assertion"] for row in value["failures"]), name
mutations = json.loads((EVIDENCE / "mutation-final-004.jsonl").read_text().splitlines()[-1])
assert mutations["source_sha256"] == source_sha
assert mutations["mutants"] == mutations["killed"] == 15 and not mutations["inconclusive"]
for row in mutations["rows"]:
    assert row["status"] == "killed" and row["receipt"]["tests_run"] == 15
    assert not row["receipt"]["errors"]
    assert any(failure["id"].split(" (")[0] == row["expected_test"]
               for failure in row["receipt"]["failures"])
    assert all(failure["phase"] == "test" and failure["is_assertion"]
               and failure["phase_attribution"] == "traceback+active-unittest-v2"
               for failure in row["receipt"]["failures"])
dry = json.loads((EVIDENCE / "dry-run.json").read_text())
assert dry["ack_count"] == 2 and dry["host_commands_executed"] == 0
for edition in ("linux_plan", "mac_plan"):
    plan = dry[edition]
    assert plan["dry_run"] and len({plan["source_volume"], plan["archive_volume"],
                                    plan["restore_volume"]}) == 3
    flattened = " ".join(part for command in plan["commands"] for part in command)
    assert "--network none" in flattened and "-w /wal" in flattened
    assert "--publish" not in flattened and "type=bind" not in flattened
assert (EVIDENCE / "ruff.txt").read_text().strip() == "All checks passed!"
assert not (EVIDENCE / "diff-check.txt").read_text().strip()
print(json.dumps({"status": "verified", "commit": COMMIT, "base": BASE,
                  "source_sha256": source_sha, "green": checks,
                  "mutations_killed": 15, "dry_run_host_commands": 0}, sort_keys=True))
