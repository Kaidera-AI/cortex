#!/usr/bin/env python3
"""Derive a replacement TEST ledger without changing release artifacts or trust."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def measure(path):
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"size": path.stat().st_size, "sha256": digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = json.loads(args.base_ledger.read_text())
    source = Path(__file__).parent
    frozen = json.loads((source / "before-custody.json").read_text())["files"]
    changed = {"prepare-host.py", "new-owner-setup.sh", "host-tools-setup.sh"}
    assert base["trust"] == "TEST_ONLY_GATE_A" and base["qualification"] is False
    assert len(base["files"]) == 43
    for name, row in base["files"].items():
        assert measure(Path(row["local_path"])) == {k: row[k] for k in ("size", "sha256")}, name
        if name in changed:
            assert row["sha256"] == frozen[name]
    commands_before = args.base_ledger.parent / "COMMANDS.md"
    assert measure(commands_before)["sha256"] == frozen["COMMANDS.md"]
    output = args.output.absolute()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    files = {}
    for name, row in base["files"].items():
        original = Path(row["local_path"])
        if row["kind"] == "release_artifact":
            path = original
        else:
            path = output / name
            shutil.copy2(source / name if name in changed else original, path)
        files[name] = {**row, "local_path": str(path), **measure(path)}
    assert sum(row["kind"] == "release_artifact" for row in files.values()) == 22
    differences = {name for name in files if files[name]["sha256"] != base["files"][name]["sha256"]}
    assert differences == changed
    tip = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    ledger = {**base, "TEST_fixture_producer": tip, "execution_ready": False,
              "pending": ["ren-cx replacement run-sheet binding", "Vera review", "Kai fresh-rerun admission", "fresh host observations"],
              "previous_ledger_sha256": measure(args.base_ledger)["sha256"], "files": files}
    (output / "command-custody.json").write_text(json.dumps(ledger, indent=2) + "\n")
    (output / "transfer-files.json").write_text(json.dumps([row["local_path"] for row in files.values()], indent=2) + "\n")
    (output / "SHA256SUMS.TEST").write_text("".join(row["sha256"] + "  " + name + "\n" for name, row in sorted(files.items())))
    commands = (source / "COMMANDS.md").read_text().replace(str(args.base_ledger.parent.absolute()), str(output))
    (output / "COMMANDS.md").write_text(commands)
    print(json.dumps({"files": 43, "changed_helpers": sorted(differences), "release_artifacts_unchanged": 22,
                      "TEST_fixture_producer": tip, "execution_ready": False,
                      "ledger_sha256": measure(output / "command-custody.json")["sha256"]}))


if __name__ == "__main__":
    main()
