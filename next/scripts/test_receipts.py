"""Stdlib unittest receipts: assertion failures and errors stay separate."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

MARKER = "CORTEX_TEST_RESULT="


def report(result):
    lines = [line[len(MARKER):] for line in (result.stdout or "").splitlines() if line.startswith(MARKER)]
    if len(lines) != 1:
        return None
    try:
        value = json.loads(lines[0])
        if not isinstance(value, dict) or type(value.get("tests_run")) is not int or value["tests_run"] < 1:
            return None
        for key in ("failures", "errors"):
            if not isinstance(value.get(key), list) or any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in value[key]):
                return None
        return value
    except (ValueError, TypeError):
        return None


def classify(result, expected):
    value = report(result)
    if result.returncode not in (0, 1) or value is None or value["errors"]:
        return "inconclusive"
    failed = {row["id"].split(" (")[0] for row in value["failures"]}
    if result.returncode == 0:
        return "survived" if not failed else "inconclusive"
    return "killed" if failed.intersection(expected) else "inconclusive"


def suite(directory):
    return subprocess.run([sys.executable, str(Path(__file__).resolve()), str(directory)], capture_output=True, text=True)


def main():
    tests = unittest.defaultTestLoader.discover(sys.argv[1])
    result = unittest.TextTestRunner(verbosity=2).run(tests)
    value = {"tests_run": result.testsRun,
             "failures": [{"id": test.id(), "traceback": traceback} for test, traceback in result.failures],
             "errors": [{"id": test.id(), "traceback": traceback} for test, traceback in result.errors]}
    print(MARKER + json.dumps(value), flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
