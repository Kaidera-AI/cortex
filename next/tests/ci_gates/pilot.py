"""Reproducible causal-unit pilot around the existing phase-attributing runner."""
import argparse
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tarfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from next.tests.ci_gates.receipt import (
    ReceiptRefusal, digest, mutation_complete, read_json, require,
)
_ORACLE = runpy.run_path(str(ROOT / "next/tests/test_receipts.py"))


def _nodes(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _nodes(item)
        else:
            yield item.id()


def execute(root, source, nodes):
    """Execute named tests; report collection, fixture phases and observed bytes."""
    root = Path(root).resolve()
    target = (root/source).resolve()
    require(target.is_relative_to(root) and target.is_file(), "pilot_source")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    for module in ("ci_gates", "floor", "live_contract"):
        sys.path.insert(0, str(root/"next/tests"/module/"tests"))
    suite = unittest.defaultTestLoader.loadTestsFromNames(nodes)
    selected = list(_nodes(suite))
    result = unittest.TextTestRunner(verbosity=2,
        resultclass=_ORACLE["AssertionResult"]).run(suite)
    observed = []
    for module in list(sys.modules.values()):
        origin = getattr(module, "__file__", None)
        data = getattr(module, "PROFILE", None)
        if origin and Path(origin).resolve() == target:
            observed.append("code")
        elif data and isinstance(data, Path) and data.resolve() == target:
            observed.append("inventory")
    if observed:
        print("CORTEX_LOADED_SOURCE=" + json.dumps(dict(source=source,
            origin=str(target), sha256=digest(target.read_bytes()),
            kind=sorted(set(observed)))), flush=True)
    print(_ORACLE["MARKER"] + json.dumps(dict(
        tests_run=result.testsRun, selected=selected,
        skipped=[test.id() for test, _ in result.skipped],
        expected_failures=[test.id() for test, _ in result.expectedFailures],
        unexpected_successes=[test.id() for test in result.unexpectedSuccesses],
        failures=result.assertions,
        errors=[dict(id=test.id(), traceback=traceback) for test, traceback in result.errors]
    )), flush=True)
    if not observed or not selected:
        return 2
    return 0 if result.wasSuccessful() else 1


def mutate(root, specs, output, scratch):
    """Run specs in disposable tracked-source copies; never mutate the author tree."""
    root, output, scratch = Path(root).resolve(), Path(output).resolve(), Path(scratch).resolve()
    require(not output.exists() and not scratch.exists(), "pilot_fresh_directory")
    require(not output.is_relative_to(root) and not scratch.is_relative_to(root), "pilot_output_scope")
    require(bool(specs) and len(specs) == len(set(specs)), "pilot_specs")
    output.mkdir(parents=True)
    scratch.mkdir(parents=True)
    archive = subprocess.check_output(["git", "-C", str(root), "archive", "HEAD"])
    committed = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    run_paths = []
    changed = []
    for number, spec_path in enumerate(specs, 1):
        spec = read_json(spec_path)
        source = spec["source"]
        require(type(source) is str and not Path(source).is_absolute()
                and ".." not in Path(source).parts, "pilot_source")
        case = scratch/str(number)
        case.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as packed:
            packed.extractall(case, filter="data")
        target = case/source
        require(target.is_file() and not target.is_symlink()
            and target.resolve().is_relative_to(case), "pilot_source")
        original = target.read_bytes()
        require(digest(original) == spec["source_sha256"], "pilot_source_drift")
        before, after = spec["mutation"]["before"].encode(), spec["mutation"]["after"].encode()
        require(before and before != after and original.count(before) == 1, "pilot_anchor")
        altered = original.replace(before, after, 1)
        mutant_source = output/(str(number)+".mutant")
        mutant_source.write_bytes(altered)
        packet = dict(source=source, source_commit=committed,
            spec_sha256=digest(Path(spec_path).read_bytes()), mutant_source=str(mutant_source))
        for phase in ("healthy", "mutant", "restored"):
            target.write_bytes(altered if phase == "mutant" else original)
            command = [sys.executable, "-B", "-m", "next.tests.ci_gates.pilot", "run",
                       "--root", str(case), "--source", source]
            for node in spec["expected_nodes"]:
                command.extend(["--node", node])
            # Fresh child process + archive without pycache avoids stale module/code reuse.
            try:
                done = subprocess.run(command, cwd=case, capture_output=True, timeout=45,
                    env={key: value for key, value in os.environ.items()
                         if key in {"PATH", "SYSTEMROOT", "TMPDIR", "LANG", "LC_ALL"}})
                code, stdout, stderr = done.returncode, done.stdout, done.stderr
            except subprocess.TimeoutExpired as error:
                code, stdout, stderr = 124, error.stdout or b"", error.stderr or b""
            finally:
                target.write_bytes(original)
            outpath, errpath = output/(str(number)+"-"+phase+".stdout"), output/(str(number)+"-"+phase+".stderr")
            outpath.write_bytes(stdout)
            errpath.write_bytes(stderr)
            packet[phase] = dict(command=command, cwd=str(case), returncode=code,
                stdout=str(outpath), stderr=str(errpath), stdout_sha256=digest(stdout),
                stderr_sha256=digest(stderr))
        path = output/(str(number)+"-run.json")
        path.write_text(json.dumps(packet, indent=2)+"\n")
        run_paths.append(str(path))
        changed.append(source)
    packet = dict(root=str(root), changed_sources=changed, specs=[str(Path(p).resolve()) for p in specs],
                  runs=run_paths, source_commit=committed)
    (output/"admission.json").write_text(json.dumps(packet, indent=2)+"\n")
    result = mutation_complete(root, changed, specs, run_paths)
    (output/"result.json").write_text(json.dumps(result, indent=2)+"\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="mode", required=True)
    run = actions.add_parser("run")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--source", required=True)
    run.add_argument("--node", action="append", required=True)
    generate = actions.add_parser("mutate")
    generate.add_argument("--root", type=Path, required=True)
    generate.add_argument("--spec", type=Path, action="append", required=True)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.mode == "run":
            return execute(args.root, args.source, args.node)
        result = mutate(args.root, args.spec, args.output, args.scratch)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ReceiptRefusal as error:
        print(json.dumps(dict(refusal=error.code)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
