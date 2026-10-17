"""Causal controls for the API-login-only ledger grant (Kai approval, Cox rework5): each mutant of roles._ledger_steps must FAIL the real-PG LedgerAcl suite (CORTEX_PG_BIN, own heavy lock held by the caller).
Run: python3 mutate_ledger.py"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / 'roles.py'
OLD = "return [f'GRANT USAGE ON SCHEMA core TO {api};', f'GRANT SELECT ON core.schema_migrations TO {api};']"
MUTANTS = [
    ('select_without_usage', "return [f'GRANT SELECT ON core.schema_migrations TO {api};']"),
    ('usage_without_select', "return [f'GRANT USAGE ON SCHEMA core TO {api};']"),
    ('select_to_public', "return [f'GRANT USAGE ON SCHEMA core TO {api};', 'GRANT SELECT ON core.schema_migrations TO PUBLIC;']"),
    ('usage_to_public', "return ['GRANT USAGE ON SCHEMA core TO PUBLIC;', f'GRANT SELECT ON core.schema_migrations TO {api};']"),
    ('select_to_request_group', "return [f'GRANT USAGE ON SCHEMA core TO {api};', f'GRANT SELECT ON core.schema_migrations TO {api}, \"kaidera-runtime-core-request\";']"),
    ('usage_and_select_to_probe', "return [f'GRANT USAGE ON SCHEMA core TO {api}, ' + _q(b.logins['probe']) + ';', f'GRANT SELECT ON core.schema_migrations TO {api};']"),
    ('write_privilege_added', "return [f'GRANT USAGE ON SCHEMA core TO {api};', f'GRANT SELECT, INSERT ON core.schema_migrations TO {api};']"),
    ('all_core_tables', "return [f'GRANT USAGE ON SCHEMA core TO {api};', f'GRANT SELECT ON ALL TABLES IN SCHEMA core TO {api};']"),
    ('create_on_schema', "return [f'GRANT USAGE, CREATE ON SCHEMA core TO {api};', f'GRANT SELECT ON core.schema_migrations TO {api};']"),
    ('revoke_group_baseline', "return [f'GRANT USAGE ON SCHEMA core TO {api};', f'GRANT SELECT ON core.schema_migrations TO {api};', 'REVOKE USAGE ON SCHEMA core FROM \"kaidera-runtime-core-request\";']"),
]
original = SRC.read_text()
assert original.count(OLD) == 1
results = []
try:
    for name, new in MUTANTS:
        SRC.write_text(original.replace(OLD, new))
        r = subprocess.run([sys.executable, '-m', 'unittest', 'test_ledger_acl_pg.LedgerAcl'], cwd=HERE, capture_output=True, text=True)
        results.append({'mutant': name, 'killed': r.returncode != 0})
finally:
    SRC.write_text(original)
print(json.dumps(results, indent=1))
sys.exit(0 if all(x['killed'] for x in results) else 1)
