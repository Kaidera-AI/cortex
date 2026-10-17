"""Day-1 receipt policy interfaces (permissive RED baseline only)."""
from pathlib import Path
import runpy

_CORE = runpy.run_path(str(Path(__file__).resolve().parents[3] /
                          ".github/scripts/pytest_u13_receipt.py"))
ReceiptRefusal = _CORE["ReceiptRefusal"]
require = _CORE["require"]


def merged_tree(repository, expected, receipts):
    pass


def history_complete(index, expected_head, required_jobs):
    pass


def mutation_complete(root, changed_sources, specs, runs):
    pass
