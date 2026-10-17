"""Evidence admission for Day-1 CI controls; never an automatic release GO."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import runpy
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
_CORE = runpy.run_path(str(ROOT / ".github/scripts/pytest_u13_receipt.py"))
ReceiptRefusal = _CORE["ReceiptRefusal"]
require = _CORE["require"]
object_pairs = _CORE["object_pairs"]
sha256 = _CORE["sha"]


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    try:
        return json.loads(raw, object_pairs_hook=object_pairs,
            parse_constant=lambda _: require(False, "receipt_nonfinite"))
    except (ValueError, TypeError, UnicodeError):
        raise ReceiptRefusal("receipt_json") from None


def read_json(path):
    try:
        path = Path(path)
        require(not path.is_symlink() and path.is_file() and 0 < path.stat().st_size <= 16*1024*1024,
                "receipt_file")
        return decode(path.read_bytes())
    except OSError:
        raise ReceiptRefusal("receipt_file") from None


def _pin(value):
    return type(value) is str and re.fullmatch("[0-9a-f]{40}", value) is not None


def _unique(values):
    return type(values) is list and bool(values) and all(
        type(value) is str and bool(value) for value in values) and len(values) == len(set(values))


def merged_tree(repository, expected, receipts):
    """Check actual Git objects/parents against independent reviewed pins and lanes."""
    try:
        require(type(expected) is dict and type(receipts) is list, "merged_tree")
        require(all(_pin(expected.get(key)) for key in ("base", "head", "composition", "tree")),
                "merged_tree")
        require(_unique(expected.get("lanes")), "merged_tree")
        def git(*args):
            return subprocess.check_output(["git", "-C", str(repository), *args],
                text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
        require(all(git("cat-file", "-t", expected[key]) == "commit"
                    for key in ("base", "head", "composition")), "merged_tree")
        actual = git("show", "-s", "--format=%T %P", expected["composition"]).split()
        require(actual == [expected["tree"], expected["base"], expected["head"]], "merged_tree")
        require(expected["base"] != expected["head"], "merged_tree")
        subprocess.run(["git", "-C", str(repository), "merge-base", "--is-ancestor",
            expected["base"], expected["head"]], check=True, capture_output=True, timeout=10)
        require(len(receipts) == len(expected["lanes"]) and
            {row.get("lane") for row in receipts} == set(expected["lanes"]), "merged_tree")
        require(all(row.get("source_commit") == expected["composition"]
            and row.get("tree") == expected["tree"] for row in receipts), "merged_tree")
        return dict(composition=expected["composition"], tree=actual[0], lanes=expected["lanes"])
    except (KeyError, TypeError, AttributeError, OSError, subprocess.SubprocessError):
        raise ReceiptRefusal("merged_tree") from None


def _raw_history(index):
    """Bind admitted metadata back to every preserved provider response."""
    artifacts = index["raw_artifacts"]
    repository = index["repository"]
    require(type(artifacts) is list and bool(artifacts)
        and len({(row["endpoint"], row.get("capture", "initial")) for row in artifacts})
            == len(artifacts), "history_raw_binding")
    raw = {}
    for artifact in artifacts:
        path = Path(artifact["path"])
        require(digest(path.read_bytes()) == artifact["sha256"], "history_raw_hash")
        raw[(artifact["endpoint"], artifact.get("capture", "initial"))] = read_json(path)
    used = set()
    def take(endpoint, capture="initial"):
        key = (endpoint, capture)
        require(key in raw, "history_raw_binding")
        used.add(key)
        return raw[key]
    prefix = "repos/" + repository + "/actions/runs"
    for number, page in enumerate(index["pages"], 1):
        expected = take(prefix + "?per_page=100&page=" + str(number))
        require(page == dict(expected, page=number), "history_raw_binding")
        final = take(prefix + "?per_page=100&page=" + str(number), "final")
        require(final == expected, "history_snapshot_drift")
    for attempt in index["attempts"]:
        endpoint = prefix+"/"+str(attempt["run_id"])+"/attempts/"+str(attempt["attempt"])
        detail = take(endpoint)
        require(detail.get("id") == attempt["run_id"]
            and detail.get("run_attempt") == attempt["attempt"]
            and all(detail.get(key) == attempt[key] for key in ("head_sha", "status", "conclusion")),
            "history_raw_binding")
        jobs = []
        count = attempt["total_count"]
        require(type(count) is int and count >= 0, "history_raw_binding")
        for number in range(1, max(1, (count+99)//100)+1):
            page = take(endpoint+"/jobs?per_page=100&page="+str(number))
            require(page.get("total_count") == count and type(page.get("jobs")) is list
                and len(page["jobs"]) == min(100, max(0, count-(number-1)*100)), "history_raw_binding")
            jobs.extend(dict(job, run_attempt=attempt["attempt"]) for job in page["jobs"])
        require(jobs == attempt["jobs"], "history_raw_binding")
    require(used == set(raw), "history_raw_binding")


def history_complete(index, expected_head, required_jobs):
    """Admit complete raw page/attempt inventories; require two distinct clean runs.

    The collector/lead supplies the independently reviewed history window. This
    pure check cannot attest that the supplied provider responses are authentic.
    Closures are references for lead review, never self-authorized waivers.
    """
    try:
        require(_pin(expected_head) and type(index) is dict and index.get("head") == expected_head,
                "history_complete")
        require(type(required_jobs) is dict and bool(required_jobs)
            and all(type(key) is str and key and _unique(value)
                    for key, value in required_jobs.items()), "history_complete")
        accepted = index["accepted_gate"]
        require(type(accepted) is dict and _pin(accepted.get("commit"))
            and sha256(accepted.get("receipt_sha256")), "history_accepted_gate")
        cutoff = datetime.fromisoformat(accepted["accepted_at"].replace("Z", "+00:00"))
        require(cutoff.tzinfo is not None, "history_accepted_gate")
        pages = index["pages"]
        per_page = index.get("per_page", 100)
        require(type(per_page) is int and 1 <= per_page <= 100
            and type(pages) is list and bool(pages), "history_complete")
        total = pages[0]["total_count"]
        require(type(total) is int and total > 0 and len(pages) == (total + per_page-1)//per_page,
                "history_complete")
        runs = []
        for number, page in enumerate(pages, 1):
            expected_count = min(per_page, total - (number-1)*per_page)
            require(page["page"] == number and page["total_count"] == total
                and type(page["workflow_runs"]) is list
                and len(page["workflow_runs"]) == expected_count, "history_complete")
            runs.extend(page["workflow_runs"])
        require(all(type(row["id"]) is int and row["id"] > 0 for row in runs)
            and len({row["id"] for row in runs}) == total, "history_complete")
        if "raw_artifacts" in index:
            _raw_history(index)
        attempts = index["attempts"]
        require(type(attempts) is list, "history_complete")
        keys = [(row["run_id"], row["attempt"]) for row in attempts]
        require(len(keys) == len(set(keys)), "history_complete")
        wanted = set()
        clean = {name: [] for name in required_jobs}
        closures = index.get("closures", [])
        require(type(closures) is list, "history_complete")
        for run in runs:
            updated = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
            require(updated.tzinfo is not None, "history_complete")
            in_window = updated >= cutoff
            require(_pin(run["head_sha"]) and type(run["run_attempt"]) is int
                and run["run_attempt"] >= 1 and run["status"] == "completed", "history_complete")
            for number in range(1, run["run_attempt"]+1):
                key = (run["id"], number)
                wanted.add(key)
                require(key in keys, "history_complete")
                attempt = attempts[keys.index(key)]
                require(attempt["status"] == "completed" and attempt["head_sha"] == run["head_sha"],
                        "history_complete")
                jobs = attempt["jobs"]
                require(type(jobs) is list and bool(jobs)
                    and type(attempt["total_count"]) is int
                    and len(jobs) == attempt["total_count"]
                    and len({job["id"] for job in jobs}) == len(jobs)
                    and len({job["name"] for job in jobs}) == len(jobs)
                    and all(type(job["id"]) is int and job["id"] > 0
                        and type(job["name"]) is str and bool(job["name"]) for job in jobs),
                    "history_complete")
                require(all(job["head_sha"] == run["head_sha"] and job["run_attempt"] == number
                    and job["status"] == "completed" for job in jobs), "history_complete")
                required = set(required_jobs.get(run["name"], []))
                require(required <= {job["name"] for job in jobs}, "history_complete")
                bad = [job for job in jobs if job["conclusion"] != "success"]
                # A run-level failure cannot be hidden behind green job records.
                if attempt["conclusion"] != "success":
                    bad.append(dict(id="run", conclusion=attempt["conclusion"]))
                require(number != run["run_attempt"] or run["conclusion"] == attempt["conclusion"],
                        "history_complete")
                for failure in bad if in_window else []:
                    matches = [row for row in closures if
                        (row.get("run_id"), row.get("attempt"), row.get("job_id")) ==
                        (run["id"], number, failure["id"])]
                    require(len(matches) == 1 and run["head_sha"] != expected_head, "history_unresolved_red")
                    closure = matches[0]
                    require(closure.get("fixed_in") == expected_head
                        and sha256(closure.get("evidence_sha256"))
                        and type(closure.get("root_cause")) is str and bool(closure["root_cause"].strip())
                        and type(closure.get("reviewer")) is str and bool(closure["reviewer"].strip()),
                        "history_unresolved_red")
                if in_window and run["name"] in clean and run["head_sha"] == expected_head:
                    # Rerun attempts are history, not independent clean runs.
                    require(not bad and run["run_attempt"] == 1, "history_unresolved_red")
                    clean[run["name"]].append(run["id"])
        require(set(keys) == wanted, "history_complete")
        require(all(len(set(values)) >= 2 for values in clean.values()), "history_two_clean_runs")
        return dict(head=expected_head, run_count=total, attempt_count=len(attempts),
                    clean_runs={key: sorted(set(value)) for key, value in clean.items()},
                    qualification="history_metadata_only")
    except (KeyError, TypeError, AttributeError, IndexError, OSError, ValueError):
        raise ReceiptRefusal("history_complete") from None


def _source(root, relative):
    require(type(relative) is str and not Path(relative).is_absolute()
        and ".." not in Path(relative).parts, "mutation_source")
    path = Path(root) / relative
    require(path.resolve().is_relative_to(Path(root).resolve()) and path.is_file()
        and not path.is_symlink(), "mutation_source")
    return path.read_bytes()


def _process(record, expected_hash, source, tests):
    """Verify raw logs and independently observed loaded-source fingerprint."""
    stdout = Path(record["stdout"]).read_bytes()
    stderr = Path(record["stderr"]).read_bytes()
    require(digest(stdout) == record["stdout_sha256"]
        and digest(stderr) == record["stderr_sha256"], "mutation_log_hash")
    lines = [line.split("=", 1)[1] for line in stdout.decode().splitlines()
             if line.startswith("CORTEX_LOADED_SOURCE=")]
    require(len(lines) == 1, "mutation_loaded_source")
    loaded = decode(lines[0])
    require(loaded.get("source") == source and loaded.get("sha256") == expected_hash
        and type(loaded.get("origin")) is str
        and loaded["origin"].endswith("/" + source), "mutation_loaded_source")
    test_lines = [line.split("=", 1)[1] for line in stdout.decode().splitlines()
                  if line.startswith("CORTEX_LOADED_TESTS=")]
    require(len(test_lines) == 1 and decode(test_lines[0]) == tests, "mutation_loaded_tests")
    return subprocess.CompletedProcess(record.get("command", []), record["returncode"],
                                       stdout.decode(), stderr.decode())


def mutation_complete(root, changed_sources, specs, runs):
    """Exact source/spec/run bijection and real healthy -> BODY kill -> restored."""
    from next.tests.floor.release_profile import oracle_controls
    try:
        require(_unique(changed_sources) and type(specs) is list and type(runs) is list
            and len(specs) == len(runs) == len(changed_sources), "mutation_bijection")
        pairs = [(path, read_json(path)) for path in specs]
        packets = [read_json(path) for path in runs]
        require({spec["source"] for _, spec in pairs} == set(changed_sources)
            and len({row["source"] for row in packets}) == len(packets)
            and {row["source"] for row in packets} == set(changed_sources), "mutation_bijection")
        admitted = []
        for path, spec in pairs:
            source = spec["source"]
            original = _source(root, source)
            require(digest(original) == spec["source_sha256"], "mutation_source_hash")
            tests = spec["tests"]
            require(type(tests) is dict and bool(tests) and all(
                digest(_source(root, name)) == value for name, value in tests.items()),
                "mutation_test_hash")
            expected = spec["expected_nodes"]
            require(_unique(expected), "mutation_nodes")
            before = spec["mutation"]["before"].encode()
            after = spec["mutation"]["after"].encode()
            require(bool(before) and before != after and original.count(before) == 1, "mutation_anchor")
            mutant = original.replace(before, after, 1)
            packet = next(row for row in packets if row["source"] == source)
            require(packet["spec_sha256"] == digest(Path(path).read_bytes()), "mutation_spec_hash")
            require(Path(packet["mutant_source"]).read_bytes() == mutant, "mutation_observed")
            mutant_tests = dict(tests)
            if source in tests:
                require(spec.get("kind") == "oracle-fixture", "mutation_changed_assertions")
                mutant_tests[source] = digest(mutant)
            healthy = _process(packet["healthy"], digest(original), source, tests)
            changed = _process(packet["mutant"], digest(mutant), source, mutant_tests)
            restored = _process(packet["restored"], digest(original), source, tests)
            require(oracle_controls(healthy, set(expected)) == "survived"
                and oracle_controls(changed, set(expected)) == "killed"
                and oracle_controls(restored, set(expected)) == "survived", "mutation_not_body_kill")
            admitted.append(source)
        return dict(sources=sorted(admitted), body_kills=len(admitted), inconclusive=0,
                    qualification="causal_unit_only")
    except (KeyError, TypeError, AttributeError, StopIteration, OSError, UnicodeError):
        raise ReceiptRefusal("mutation_incomplete") from None



def collect_history(repository, head, output_directory, fetch=None):
    """Read-only GitHub collector: all repository runs, all attempts, all job pages.

    No branch/status/latest-only filters. Authentication stays inside gh. A fresh
    directory prevents overwriting any failed snapshot. Provider/API failures
    remain blocking; partial raw files are retained and never retried to green.
    """
    require(type(repository) is str and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
            repository) is not None and _pin(head), "history_scope")
    output = Path(output_directory).resolve()
    require(not output.exists(), "history_output_exists")
    output.mkdir(parents=True)
    artifacts = []
    def get(endpoint, name):
        try:
            raw = fetch(endpoint) if fetch else subprocess.check_output(
                ["gh", "api", "-H", "Accept: application/vnd.github+json", endpoint],
                stderr=subprocess.PIPE, timeout=45)
        except (OSError, subprocess.SubprocessError):
            raise ReceiptRefusal("history_provider_unavailable") from None
        path = output / name
        path.write_bytes(raw)
        artifacts.append(dict(path=str(path), sha256=digest(raw), endpoint=endpoint,
                              capture="final" if name.startswith("runs-final-") else "initial"))
        value = decode(raw)
        require(type(value) is dict, "history_provider_shape")
        return value
    def pages(endpoint, name, field):
        result = []
        count = None
        for number in range(1, 10001):
            packet = get(endpoint + "?per_page=100&page=" + str(number), name+str(number)+".json")
            total = packet.get("total_count")
            require(type(total) is int and total >= 0 and (count is None or total == count),
                    "history_snapshot_drift")
            count = total
            items = packet.get(field)
            require(type(items) is list and len(items) == min(100, max(0, total-(number-1)*100)),
                    "history_pagination")
            result.append(dict(packet, page=number))
            if number*100 >= total:
                return result
        raise ReceiptRefusal("history_pagination_bound")
    run_pages = pages("repos/"+repository+"/actions/runs", "runs-", "workflow_runs")
    attempts = []
    for page in run_pages:
        for run in page["workflow_runs"]:
            require(type(run.get("id")) is int and type(run.get("run_attempt")) is int
                and run["run_attempt"] >= 1, "history_provider_shape")
            for number in range(1, run["run_attempt"]+1):
                endpoint = "repos/"+repository+"/actions/runs/"+str(run["id"])+"/attempts/"+str(number)
                name = "run-"+str(run["id"])+"-attempt-"+str(number)
                detail = get(endpoint, name+".json")
                require(detail.get("id") == run["id"] and detail.get("run_attempt") == number
                    and detail.get("head_sha") == run["head_sha"], "history_attempt_identity")
                job_pages = pages(endpoint+"/jobs", name+"-jobs-", "jobs")
                # run_attempt provenance comes from the attempt-specific URL,
                # not a field invented as a provider-reported job property.
                jobs = [dict(job, run_attempt=number) for item in job_pages for job in item["jobs"]]
                attempts.append(dict(run_id=run["id"], attempt=number,
                    status=detail["status"], conclusion=detail["conclusion"],
                    head_sha=detail["head_sha"], total_count=job_pages[0]["total_count"], jobs=jobs))
    final_pages = pages("repos/"+repository+"/actions/runs", "runs-final-", "workflow_runs")
    require(final_pages == run_pages, "history_snapshot_drift")
    index = dict(head=head, repository=repository, per_page=100, pages=run_pages,
                 attempts=attempts, closures=[], raw_artifacts=artifacts,
                 qualification="all_repository_history_metadata")
    (output/"index.json").write_text(json.dumps(index, indent=2)+"\n")
    return index

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("history", "composition", "mutation", "collect"))
    parser.add_argument("packet", type=Path, help="JSON command packet; see contract.md")
    args = parser.parse_args()
    try:
        value = read_json(args.packet)
        if args.mode == "collect":
            collect_history(value["repository"], value["head"], value["output_directory"])
            print(json.dumps(dict(index=str(Path(value["output_directory"]).resolve()/"index.json"))))
            return 0
        if args.mode == "history":
            index = read_json(value["index"])
            require(bool(index.get("raw_artifacts")), "history_raw_missing")
            accepted = value["accepted_gate"]
            require(digest(Path(accepted["artifact"]).read_bytes()) == accepted["receipt_sha256"],
                    "history_accepted_gate")
            index["accepted_gate"] = {key: accepted[key] for key in
                                     ("commit", "accepted_at", "receipt_sha256")}
            result = history_complete(index, value["expected_head"], value["required_jobs"])
        elif args.mode == "composition":
            result = merged_tree(value["repository"], value["expected"], value["receipts"])
        else:
            changed = subprocess.check_output(["git", "-C", value["root"], "diff",
                "--name-only", value["base"], "HEAD", "--", "*.py",
                "next/tests/floor/profile.json"], text=True).splitlines()
            require(set(changed) == set(value["changed_sources"]), "mutation_changed_source_inventory")
            head = subprocess.check_output(["git", "-C", value["root"], "rev-parse", "HEAD"], text=True).strip()
            require(all(read_json(path).get("source_commit") == head for path in value["runs"]),
                    "mutation_source_commit")
            result = mutation_complete(value["root"], value["changed_sources"], value["specs"], value["runs"])
        print(json.dumps(result, sort_keys=True))
        return 0
    except ReceiptRefusal as error:
        print(json.dumps(dict(refusal=error.code)))
        return 1
    except (KeyError, TypeError, OSError, subprocess.SubprocessError):
        print(json.dumps(dict(refusal="receipt_input")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
