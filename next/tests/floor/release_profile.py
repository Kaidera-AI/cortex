"""Retained floor inventory and stricter attribution around the existing oracle."""
from pathlib import Path
import runpy

from next.tests.ci_gates.receipt import ReceiptRefusal, decode, digest, read_json, require

ROOT = Path(__file__).resolve().parents[3]
_ORACLE = runpy.run_path(str(ROOT / "next/tests/test_receipts.py"))
PROFILE = Path(__file__).with_name("profile.json")
PROFILE_SHA256 = "f0d77af40791bcb97ad9f718d91fcc2313e7b7897d01bcc4b4f5e7fed3d5f092"


def oracle_controls(completed_process, expected_nodes):
    """Only exact collected nodes with intended BODY assertions can count as kills."""
    if type(completed_process.returncode) is not int:
        return "inconclusive"
    lines = [line.split("=", 1)[1] for line in (completed_process.stdout or "").splitlines()
             if line.startswith("CORTEX_TEST_RESULT=")]
    try:
        if len(lines) != 1:
            return "inconclusive"
        decode(lines[0])  # Same strict JSON loader; reject duplicate/nonfinite data.
    except ReceiptRefusal:
        return "inconclusive"
    value = _ORACLE["report"](completed_process)
    if value is None or not expected_nodes:
        return "inconclusive"
    selected = value.get("selected")
    if (type(selected) is not list or any(type(node) is not str for node in selected)
            or len(selected) != len(set(selected)) or set(selected) != set(expected_nodes)
            or value["tests_run"] != len(selected) or value.get("skipped") != []
            or value.get("expected_failures", []) != []
            or value.get("unexpected_successes", []) != []):
        return "inconclusive"
    failed = [row["id"].split(" (")[0] for row in value["failures"]]
    if len(failed) != len(set(failed)) or not set(failed) <= set(expected_nodes):
        return "inconclusive"
    return _ORACLE["classify"](completed_process, set(expected_nodes))


def profile(root, baseline_manifest):
    """Validate retained bytes/identities; deliberately awards zero execution credit."""
    try:
        root = Path(root).resolve()
        require(digest(PROFILE.read_bytes()) == PROFILE_SHA256, "floor_profile_drift")
        value = read_json(PROFILE)
        baseline = read_json(baseline_manifest)
        require(type(baseline.get("run")) is list and bool(baseline["run"])
            and type(baseline.get("excluded")) is list, "floor_inventory")
        require(len(set(baseline["run"])) == len(baseline["run"]), "floor_inventory")
        # Check the independent pinned admission snapshot cannot silently shrink.
        require(set(value["prior_run"]) <= set(baseline["run"])
            and all(row in baseline["excluded"] for row in value["prior_excluded"])
            and all(baseline.get(key) == expected for key, expected in
                    value["prior_controls"].items()), "floor_prior_admissions")
        rows = value["rows"]
        require(len(rows) == 107 and len({row["node"] for row in rows}) == 107
            and sum(row["real_pg"] for row in rows) == 81, "floor_inventory")
        crypto = value["crypto"]
        require(len(crypto["nodes"]) == len(set(crypto["nodes"])) == 32, "floor_inventory")
        for relative, expected in value["source_hashes"].items():
            path = root / relative
            require(not path.is_symlink() and path.resolve().is_relative_to(root)
                and digest(path.read_bytes()) == expected, "floor_source_drift")
        require(all(row["source"] in value["source_hashes"] for row in rows)
            and crypto["source"] in value["source_hashes"], "floor_inventory")
        return dict(floor_nodes=[row["node"] for row in rows], crypto_nodes=crypto["nodes"],
            real_pg_required=[row["node"] for row in rows if row["real_pg"]],
            prior_run=baseline["run"], prior_excluded=baseline["excluded"], passed=0,
            qualification="inventory_only")
    except (KeyError, TypeError, AttributeError, OSError):
        raise ReceiptRefusal("floor_incomplete") from None
