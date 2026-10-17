"""Day-1 policy assertions; fixtures are synthetic evidence, never release credit."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from next.tests.ci_gates import receipt as gate

HEAD = "a" * 40
TREE = "b" * 40


def history():
    runs = []
    attempts = []
    for run_id in (11, 12):
        runs.append(dict(id=run_id, head_sha=HEAD, run_attempt=1, name="offline",
                         status="completed", conclusion="success", updated_at="2026-10-17T12:01:00Z"))
        attempts.append(dict(run_id=run_id, attempt=1, status="completed", conclusion="success", head_sha=HEAD, total_count=2, jobs=[
            dict(id=run_id*10+i, name=name, status="completed", conclusion="success",
                 head_sha=HEAD, run_attempt=1)
            for i, name in enumerate(("py313", "py314"))]))
    return dict(head=HEAD, pages=[dict(page=1, total_count=2, workflow_runs=runs)],
                attempts=attempts, closures=[], accepted_gate=dict(commit="d"*40, accepted_at="2026-10-17T12:00:00Z", receipt_sha256="f"*64))


class Receipt(unittest.TestCase):
    def test_merged_tree(self):
        with self.assertRaises(gate.ReceiptRefusal):
            gate.merged_tree(ROOT, dict(base=HEAD, head=HEAD, composition=HEAD,
                                       tree=TREE, lanes=["offline"]), [])

    def test_history_complete(self):
        value = history()
        value["pages"][0]["total_count"] = 3
        with self.assertRaises(gate.ReceiptRefusal):
            gate.history_complete(value, HEAD, {"offline": ["py313", "py314"]})

    def test_history_retains_prior_heads_and_every_attempt(self):
        for change in ("missing_attempt", "old_timeout", "one_run", "missing_job", "head_drift"):
            with self.subTest(change=change):
                value = history()
                if change == "missing_attempt":
                    value["pages"][0]["workflow_runs"][0]["run_attempt"] = 2
                elif change == "old_timeout":
                    value["pages"][0]["total_count"] = 3
                    value["pages"][0]["workflow_runs"].append(dict(
                        id=10, head_sha="c"*40, run_attempt=1, name="offline",
                        status="completed", conclusion="timed_out", updated_at="2026-10-17T12:01:00Z"))
                    value["attempts"].append(dict(run_id=10, attempt=1, status="completed", conclusion="timed_out", head_sha="c"*40, total_count=1,
                        jobs=[dict(id=100, name="py313", status="completed",
                                   conclusion="timed_out", head_sha="c"*40, run_attempt=1)]))
                elif change == "one_run":
                    value["pages"][0]["workflow_runs"].pop()
                    value["pages"][0]["total_count"] = 1
                    value["attempts"].pop()
                elif change == "missing_job":
                    value["attempts"][0]["jobs"].pop()
                else:
                    value["attempts"][0]["jobs"][0]["head_sha"] = "c"*40
                with self.assertRaises(gate.ReceiptRefusal):
                    gate.history_complete(value, HEAD, {"offline": ["py313", "py314"]})

    def test_two_independent_complete_runs(self):
        self.assertEqual(gate.history_complete(history(), HEAD,
            {"offline": ["py313", "py314"]})["clean_runs"], {"offline": [11, 12]})


    def test_old_accepted_history_is_preserved_but_rerun_red_blocks(self):
        value = history()
        old = dict(value["pages"][0]["workflow_runs"][0], id=10, head_sha="c"*40,
                   conclusion="timed_out", updated_at="2026-10-17T11:59:00Z")
        value["pages"][0]["workflow_runs"].append(old)
        value["pages"][0]["total_count"] = 3
        value["attempts"].append(dict(run_id=10, attempt=1, status="completed",
            head_sha="c"*40, conclusion="timed_out", total_count=2, jobs=[
                dict(job, head_sha="c"*40, conclusion="timed_out")
                for job in value["attempts"][0]["jobs"]]))
        self.assertEqual(gate.history_complete(value, HEAD,
            {"offline": ["py313", "py314"]})["run_count"], 3)
        old["updated_at"] = "2026-10-17T12:02:00Z"
        with self.assertRaises(gate.ReceiptRefusal):
            gate.history_complete(value, HEAD, {"offline": ["py313", "py314"]})

    def test_empty_or_duplicate_inventory_refuses(self):
        for required in ({}, {"offline": []}, {"offline": ["py313", "py313"]}):
            with self.subTest(required=required), self.assertRaises(gate.ReceiptRefusal):
                gate.history_complete(history(), HEAD, required)


    def test_collector_keeps_every_attempt_and_raw_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "raw"
            value = history()
            run = copy.deepcopy(value["pages"][0]["workflow_runs"][0])
            run["run_attempt"] = 2
            responses = {
                "repos/fixture/project/actions/runs?per_page=100&page=1":
                    dict(total_count=1, workflow_runs=[run]),
            }
            for attempt in (1, 2):
                prefix = "repos/fixture/project/actions/runs/11/attempts/" + str(attempt)
                responses[prefix] = dict(run, run_attempt=attempt)
                responses[prefix+"/jobs?per_page=100&page=1"] = dict(total_count=2,
                    jobs=value["attempts"][0]["jobs"])
            called = []
            def fetch(endpoint):
                called.append(endpoint)
                return json.dumps(responses[endpoint]).encode()
            collected = gate.collect_history("fixture/project", HEAD, root, fetch=fetch)
            self.assertEqual(len(collected["attempts"]), 2)
            self.assertEqual(len(collected["raw_artifacts"]), 6)
            self.assertEqual(len(called), 6)
            for row in collected["raw_artifacts"]:
                self.assertEqual(hashlib.sha256(Path(row["path"]).read_bytes()).hexdigest(), row["sha256"])
            with self.assertRaises(gate.ReceiptRefusal):
                gate.history_complete(collected, HEAD, {"offline": ["py313", "py314"]})
            with self.assertRaises(gate.ReceiptRefusal):
                gate.collect_history("fixture/project", HEAD, root, fetch=fetch)


    def test_pilot_retains_required_skip_and_loaded_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"probe.py").write_text(
                "import unittest\nclass Probe(unittest.TestCase):\n"
                " @unittest.skip('deliberate required skip')\n"
                " def test_expected(self): self.assertTrue(True)\n")
            command = [sys.executable, str(ROOT/"next/tests/ci_gates/pilot.py"), "run",
                       "--root", str(root), "--source", "probe.py", "--node",
                       "probe.Probe.test_expected"]
            done = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            packet = json.loads(next(line.split("=", 1)[1] for line in done.stdout.splitlines()
                if line.startswith("CORTEX_TEST_RESULT=")))
            self.assertEqual(packet["skipped"], ["probe.Probe.test_expected"])
            loaded = json.loads(next(line.split("=", 1)[1] for line in done.stdout.splitlines()
                if line.startswith("CORTEX_LOADED_SOURCE=")))
            self.assertEqual(loaded["sha256"], hashlib.sha256((root/"probe.py").read_bytes()).hexdigest())
            from next.tests.floor.release_profile import oracle_controls
            self.assertEqual(oracle_controls(done, {"probe.Probe.test_expected"}), "inconclusive")

    def test_mutation_complete(self):
        with self.assertRaises(gate.ReceiptRefusal):
            gate.mutation_complete(ROOT, ["next/tests/ci_gates/receipt.py"], [], [])

    def test_no_changed_source_no_mutation_credit(self):
        with self.assertRaises(gate.ReceiptRefusal):
            gate.mutation_complete(ROOT, [], [], [])


    def test_mutation_artifacts_must_bind_source_spec_tests_and_body(self):
        # Synthetic packet control only; the separate pilot executes real mutants.
        node = "test_guard.Guard.test_expected"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = b"GUARD = True\n"
            (root / "guard.py").write_bytes(raw)
            (root / "test_guard.py").write_text("# independently hashed assertion fixture\n")
            sha = lambda value: hashlib.sha256(value).hexdigest()
            spec = dict(source="guard.py", source_sha256=sha(raw),
                tests={"test_guard.py": sha((root/"test_guard.py").read_bytes())},
                expected_nodes=[node], mutation=dict(before="True", after="False"))
            spec_path = root/"spec.json"
            spec_path.write_text(json.dumps(spec))
            mutant = raw.replace(b"True", b"False")
            (root/"mutant.py").write_bytes(mutant)
            packet = dict(source="guard.py", spec_sha256=sha(spec_path.read_bytes()),
                          mutant_source=str(root/"mutant.py"))
            for phase in ("healthy", "mutant", "restored"):
                observed = mutant if phase == "mutant" else raw
                value = dict(tests_run=1, selected=[node], skipped=[], errors=[], failures=[])
                if phase == "mutant":
                    value["failures"] = [dict(id=node, phase="test", is_assertion=True,
                        traceback="AssertionError: synthetic BODY", exception_type="builtins.AssertionError")]
                stdout = ("CORTEX_LOADED_SOURCE=" + json.dumps(dict(source="guard.py",
                          sha256=sha(observed), origin=str(root/"guard.py"))) + "\n" +
                          "CORTEX_TEST_RESULT=" + json.dumps(value) + "\n").encode()
                (root/(phase+".out")).write_bytes(stdout)
                (root/(phase+".err")).write_bytes(b"")
                packet[phase] = dict(stdout=str(root/(phase+".out")),
                    stderr=str(root/(phase+".err")), stdout_sha256=sha(stdout),
                    stderr_sha256=sha(b""), returncode=1 if phase == "mutant" else 0)
            run_path = root/"run.json"
            run_path.write_text(json.dumps(packet))
            self.assertEqual(gate.mutation_complete(root, ["guard.py"], [spec_path],
                [run_path])["body_kills"], 1)
            for defect in ("spec", "source", "log", "runner"):
                with self.subTest(defect=defect):
                    changed = copy.deepcopy(packet)
                    if defect == "spec":
                        changed["spec_sha256"] = "0"*64
                    elif defect == "source":
                        (root/"guard.py").write_bytes(b"changed")
                    elif defect == "log":
                        changed["mutant"]["stdout_sha256"] = "0"*64
                    else:
                        changed["mutant"]["returncode"] = 3
                    run_path.write_text(json.dumps(changed))
                    with self.assertRaises(gate.ReceiptRefusal):
                        gate.mutation_complete(root, ["guard.py"], [spec_path], [run_path])
                    (root/"guard.py").write_bytes(raw)
            run_path.write_text(json.dumps(packet))
            with self.assertRaises(gate.ReceiptRefusal):
                gate.mutation_complete(root, ["guard.py", "missing.py"], [spec_path], [run_path])

    def test_real_git_composition_not_pr_head(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args, input=None):
                return subprocess.check_output(["git", "-C", directory, *args],
                    input=input, text=True, env=dict(__import__("os").environ,
                    GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@invalid",
                    GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@invalid")).strip()
            git("init", "-q")
            tree = git("mktree", input="")
            base = git("commit-tree", tree, input="base\n")
            head = git("commit-tree", tree, "-p", base, input="head\n")
            composition = git("commit-tree", tree, "-p", base, "-p", head, input="composition\n")
            expected = dict(base=base, head=head, composition=composition, tree=tree, lanes=["offline"])
            receipts = [dict(lane="offline", source_commit=composition, tree=tree)]
            self.assertEqual(gate.merged_tree(directory, expected, receipts)["tree"], tree)
            receipts[0]["source_commit"] = head
            with self.assertRaises(gate.ReceiptRefusal):
                gate.merged_tree(directory, expected, receipts)
            receipts[0]["source_commit"] = composition
            expected["head"] = base
            with self.assertRaises(gate.ReceiptRefusal):
                gate.merged_tree(directory, expected, receipts)


if __name__ == "__main__":
    unittest.main()
