"""C11b3 source/SQL fault probes on the bounded C11b2 real-PG runner."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'c11b2'))
import run_pg as runner

runner.MUTANTS = {
    'provider_bypass': ('src/cortex_core/api/c11b2.py',
        "embedded = await self._cache(search).get(principal['principal_id'],\n                                                     self.identity, query)",
        "embedded = type('Embedded', (), {'vector': PROBE_VECTOR})()"),
    'session_claim_hook': ('src/cortex_core/records.py',
        'if self.before_commit is not None:', 'if False:'),
    'session_iso': ('src/cortex_core/api/c11b.py',
        "datetime.fromisoformat(timestamp.replace('Z', '+00:00'))",
        "datetime.fromisoformat('2026-10-10')"),
    'session_size': ('src/cortex_core/api/c11a.py',
        'await _memory_body(receive, 8 * 1024 * 1024)', 'await _memory_body(receive)'),
    'manifest_ledger': ('schema/manifest.json',
        '"id": "retrieval-0003"', '"id": "retrieval-0003-mutated"'),
    'query_cache_rls': ('schema/retrieval/002-query-cache.sql',
        'ALTER TABLE retrieval.query_embeddings FORCE ROW LEVEL SECURITY;',
        '-- removed FORCE ROW LEVEL SECURITY'),
    'source_claim_sql': ('schema/coordination/005-session-source.sql',
        'IF claimed IS DISTINCT FROM p_record_id THEN', 'IF False THEN'),
}


if __name__ == '__main__':
    raise SystemExit(runner.main())
