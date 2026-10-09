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
            if not isinstance(value.get(key), list) or any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    or not row["id"] or not isinstance(row.get("traceback"), str) or not row["traceback"] for row in value[key]):
                return None
        return value
    except (ValueError, TypeError):
        return None


def classify(result, expected):
    value = report(result)
    if result.returncode not in (0, 1) or value is None or value["errors"]:
        return "inconclusive"
    if any(row.get("phase") != "test" or row.get("is_assertion") is not True for row in value["failures"]):
        return "inconclusive"
    failed = {row["id"].split(" (")[0] for row in value["failures"]}
    if result.returncode == 0:
        return "survived" if not failed else "inconclusive"
    return "killed" if failed.intersection(expected) else "inconclusive"


def suite(directory):
    return subprocess.run([sys.executable, str(Path(__file__).resolve()), str(directory)], capture_output=True, text=True)


class AssertionResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.assertions = []

    def record_assertion(self, test, err):
        case = getattr(test, "test_case", test)
        method = getattr(case, getattr(case, "_testMethodName", ""), None)
        code = getattr(getattr(method, "__func__", method), "__code__", None)
        frames, tb = [], err[2]
        while tb is not None:
            frames.append(tb.tb_frame.f_code)
            tb = tb.tb_next
        fixture_codes = {getattr(cls, name).__code__
                         for cls in (unittest.TestCase, unittest.IsolatedAsyncioTestCase)
                         for name in ("_callSetUp", "_callTearDown", "_callCleanup")}
        fixture = any(frame in fixture_codes for frame in frames)
        phase = "test" if code is not None and code in frames and not fixture else "fixture"
        self.assertions.append({"id": test.id(), "phase": phase,
                                "is_assertion": issubclass(err[0], AssertionError),
                                "exception_type": err[0].__module__ + "." + err[0].__qualname__,
                                "traceback": self._exc_info_to_string(err, test)})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record_assertion(test, err)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None and issubclass(err[0], test.failureException):
            self.record_assertion(subtest, err)


def main():
    tests = unittest.defaultTestLoader.discover(sys.argv[1])
    result = unittest.TextTestRunner(verbosity=2, resultclass=AssertionResult).run(tests)
    value = {"tests_run": result.testsRun,
             "failures": result.assertions,
             "errors": [{"id": test.id(), "traceback": traceback} for test, traceback in result.errors]}
    print(MARKER + json.dumps(value), flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
