"""Reject a recipe that overwrites receipts before checking their frozen manifest."""
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[5]
readme = ROOT / "next/benchmarks/vector_baseline/README.md"
text = readme.read_text().split("Reproduce the bounded repair", 1)[1]
blocks = re.findall(r"```sh\n(.*?)```", text, re.S)
verifiers = [i for i, block in enumerate(blocks) if "cleanup-rework/verify-evidence.py" in block]
producers = [i for i, block in enumerate(blocks) if "mutate_vector_baseline.py" in block]
assert len(verifiers) == len(producers) == 1, "repair recipe must name each verification and reproduction once"
assert verifiers[0] < producers[0], "verify the frozen packet before any mutation producer overwrites its receipts"
assert "new manifest" in text, "fresh reproduction needs an explicit new-manifest instruction"
print("PASS frozen packet verified first; fresh reproduction separate; new manifest required")
sys.exit(0)
