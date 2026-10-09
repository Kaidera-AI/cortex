"""Reject a typo in a recorded unittest discovery start directory before launch."""
import json
import re
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[5]
receipt = json.loads(Path(sys.argv[1]).read_text())
command = receipt["command"]
assert "discover" in command and "-s" in command
start = root / command[command.index("-s") + 1]
assert start.is_dir(), f"missing unittest discovery start directory: {start}"
readme = root / "next/benchmarks/vector_baseline/README.md"
for path in re.findall(r"`(next/[^`\s]+)`", readme.read_text()):
    assert (root / path).is_file(), f"missing documented file: {path}"
print("PASS discovery start directory and documented files exist")
