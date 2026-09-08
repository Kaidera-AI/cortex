"""Offline admission regressions using canonical code, never importing main.

Only the actual apply_schema_migrations admission prefix is compiled. Database
discovery is a closed in-memory fixture; the DDL/ledger execution portion is
deliberately excluded. This proves admission, not SQL or activation correctness.
Run with --noconftest and plugin autoload disabled as documented in W2_PLAN.md.
"""
import __future__
import ast
import copy
import hashlib
from pathlib import Path
import re

import pytest


API = Path(__file__).resolve().parents[1]
MIGRATION = API.parent / 'data/migrations/2026-09-05-01-service-auth.sql'


class AdmissionError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


def canonical_admission(sql):
    """Extract the real prefix; a moved/renamed boundary fails this test closed."""
    tree = ast.parse((API / 'main.py').read_text())
    function = copy.deepcopy(next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                                  and node.name == 'apply_schema_migrations'))
    boundary = next(i for i, node in enumerate(function.body) if isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name) and node.target.id == 'results')
    function.body = function.body[:boundary] + [ast.Return(value=ast.Constant(value=True))]
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    item = {'id': MIGRATION.name, 'checksum_sha256': hashlib.sha256(sql.encode()).hexdigest(),
            'sql': sql, 'status': 'pending'}

    async def plan(conn, **kwargs):
        assert conn is None  # no connection or pool exists in this regression
        return {'migrations': [item]}

    async def ensure(conn):
        # This suite isolates admission only; SQL initialization has its own
        # write/read boundary regressions in test_schema_migrations.py.
        assert conn is None

    namespace = {'schema_migration_plan': plan, 'schema_migration_files': lambda _directory: [item],
                 'ensure_schema_migrations_table': ensure,
                 'HTTPException': AdmissionError, 're': re, 'REVIEWED_ONLINE_INDEX_MIGRATIONS': {}}
    exec(compile(module, str(API / 'main.py') + ':admission-only', 'exec',
                 flags=__future__.annotations.compiler_flag), namespace)
    return namespace['apply_schema_migrations']


@pytest.mark.asyncio
async def test_new_auth_schema_is_admitted_by_actual_canonical_engine():
    admit = canonical_admission(MIGRATION.read_text())
    assert await admit(None, dry_run=False) is True


@pytest.mark.asyncio
@pytest.mark.parametrize('sql', ['BEGIN;\nSELECT 1;\nCOMMIT;', 'COMMIT;', 'VACUUM;',
                                'CREATE INDEX CONCURRENTLY fixture_idx ON fixture(id);'])
async def test_admission_fixture_preserves_real_fail_closed_transaction_gate(sql):
    admit = canonical_admission(sql)
    with pytest.raises(AdmissionError) as caught:
        await admit(None, dry_run=False)
    assert caught.value.status_code == 409
    assert 'crash-safe nontransactional' in caught.value.detail
