"""Fail if receipt summaries disagree with retained independent tool output."""
from pathlib import Path
import hashlib
import json
import re

root = Path(__file__).resolve().parent
value = json.loads((root / 'final-evidence.json').read_text())
for receipt_name, count_name in [('primary', 'primary_findings'), ('followup', 'followup_findings'),
                                 ('phase_followup', 'phase_followup_findings'),
                                 ('final_source_followup', 'final_source_followup_findings')]:
    receipt = root / value['kodus'][receipt_name]
    meta = json.loads(receipt.read_text())
    output = receipt.with_name('kodus.stdout.txt').read_text()
    counts = re.findall(r'^ISSUES_FOUND: (\d+)$', output, re.MULTILINE)
    assert len(counts) == 1 and int(counts[0]) == value['kodus'][count_name], ('Kodus count mismatch', count_name, counts)
    assert meta['exit'] == 0 and meta['analysis_complete'] and meta['source_recheck']
    for stream, expected in meta['output_sha256'].items():
        assert hashlib.sha256(receipt.with_name('kodus.' + stream + '.txt').read_bytes()).hexdigest() == expected
print(json.dumps({'kodus_summary_matches_all_four_raw_passes': True}))
