"""Read-only provenance policy oracles, no database/host access."""
import importlib.util
from pathlib import Path
SPEC=importlib.util.spec_from_file_location('history_plan',Path(__file__).resolve().parents[1]/'cortex_history_plan.py')
MODULE=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(MODULE)

def test_missing_codex_source_is_reported_as_orphan_and_parser_unavailable(tmp_path):
    rows=[{'id':'10000000-0000-0000-0000-000000000003','project':'synthetic','provider':'codex','source_kind':'codex-session','source_path':str(tmp_path/'missing.jsonl')}]
    result=MODULE.plan(rows,[],tmp_path,'synthetic')
    entry=result['entries'][0]
    assert entry['action']=='PRESERVE'
    assert 'ORPHAN' in entry['reason'] and 'parser-unavailable' in entry['reason']
