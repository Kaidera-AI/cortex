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
                         status="completed", conclusion="success"))
        attempts.append(dict(run_id=run_id, attempt=1, total_count=2, jobs=[
            dict(id=run_id*10+i, name=name, status="completed", conclusion="success",
                 head_sha=HEAD, run_attempt=1)
            for i, name in enumerate(("py313", "py314"))]))
    return dict(head=HEAD, pages=[dict(page=1, total_count=2, workflow_runs=runs)],
                attempts=attempts, closures=[])


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
                        status="completed", conclusion="timed_out"))
                    value["attempts"].append(dict(run_id=10, attempt=1, total_count=1,
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

    def test_empty_or_duplicate_inventory_refuses(self):
        for required in ({}, {"offline": []}, {"offline": ["py313", "py313"]}):
            with self.subTest(required=required), self.assertRaises(gate.ReceiptRefusal):
                gate.history_complete(history(), HEAD, required)

    def test_mutation_complete(self):
        with self.assertRaises(gate.ReceiptRefusal):
            gate.mutation_complete(ROOT, ["next/tests/ci_gates/receipt.py"], [], [])

    def test_no_changed_source_no_mutation_credit(self):
        with self.assertRaises(gate.ReceiptRefusal):
            gate.mutation_complete(ROOT, [], [], [])

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
