"""Receipt-pinned independent parts/total, replacing exactly the withdrawn count case."""
import hashlib
import json
from collections import Counter
from pathlib import Path
from cortex_v2 import build_catalog

RECEIPT_SHA256 = '5ed608321cca0a6da2ab02577736de2dba1485dd91ad75154142ec8cd8b306f0'


def test_actual_catalogue_has_every_receipted_namespace_part_total_and_boot_row():
    # Byte-identical copy of qualification-r428-actual-relations.json, not product output.
    raw=(Path(__file__).parent/'fixtures/r428-actual-relations.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest()==RECEIPT_SHA256
    expected=json.loads(raw)
    expected_parts=Counter(r['name'].split('.')[0] for r in expected)
    expected_boot=[r['name'] for r in expected if r['name'].startswith('cortex_context.boot_')]
    actual=build_catalog._relations(expected)
    actual_parts=Counter(r['name'].split('.')[0] for r in actual)
    assert actual_parts==expected_parts
    assert len(actual)==len(expected)==sum(expected_parts.values())
    assert [r['name'] for r in actual if r['name'].startswith('cortex_context.boot_')]==expected_boot
    assert set(actual_parts)==set(expected_parts)
