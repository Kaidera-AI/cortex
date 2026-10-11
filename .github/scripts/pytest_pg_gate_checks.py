"""Real pytest controls: an outcome gate must never green a collection/setup gap."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from pytest_pg_worker import OutcomeGate


def checks(output):
    output.mkdir(parents=True, exist_ok=True)
    cases = {
        "pass": ("def test_ok(): assert True\n", [], 1, 0, 0),
        "body-red": ("def test_red(): assert False\n", [], 1, 0, 1),
        "expected-body-red": ("def test_red(): assert False\n", [], 1, 1, 0),
        "skip": ("import pytest\ndef test_skip(): pytest.skip('control')\n", [], 1, 0, 1),
        "deselect": ("def test_a(): pass\ndef test_b(): pass\n", ["-k", "test_a"], 1, 0, 1),
        "count-drift": ("def test_ok(): pass\n", [], 2, 0, 1),
        "collection-error": ("def test_ok(): pass\n", ["--continue-on-collection-errors", "test_broken.py"], 1, 0, 1),
        "setup-error": ("import pytest\n@pytest.fixture\ndef bad(): raise RuntimeError('control')\ndef test_bad(bad): pass\n", [], 1, 0, 1),
        "teardown-error": ("import pytest\n@pytest.fixture\ndef bad():\n yield\n raise RuntimeError('control')\ndef test_bad(bad): pass\n", [], 1, 0, 1),
    }
    receipts = []
    with tempfile.TemporaryDirectory(prefix="cortex-pg-gate-") as directory:
        for name, (source, extra, selected, failures, expected) in cases.items():
            test = Path(directory) / "test_control.py"
            test.write_text(source)
            (Path(directory) / "test_broken.py").write_text("raise RuntimeError('collection control')\n")
            report = output / (name + ".json")
            code = ("import sys,pytest;from pathlib import Path;from pytest_pg_worker import OutcomeGate;"
                    "gate=OutcomeGate(Path(sys.argv[1]),int(sys.argv[2]),int(sys.argv[3]));"
                    "raise SystemExit(pytest.main(sys.argv[4:],plugins=[gate]))")
            env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
            env["PYTHONPATH"] = str(Path(__file__).parent) + os.pathsep + env.get("PYTHONPATH", "")
            result = subprocess.run([sys.executable, "-c", code, str(report), str(failures), str(selected),
                                     "-q", "--tb=short", "-o", "cache_dir=" + directory, str(test), *extra],
                                    env=env, cwd=directory, capture_output=True, text=True, timeout=30)
            (output / (name + ".txt")).write_text(result.stdout + result.stderr)
            receipts.append({"name": name, "expected": expected, "actual": result.returncode})
    (output / "gate-controls.json").write_text(json.dumps(receipts, indent=2) + "\n")
    assert all(row["expected"] == row["actual"] for row in receipts), receipts
    print("nine real pytest gate controls passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    checks(parser.parse_args().output)
