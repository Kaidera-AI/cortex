"""Projection unit contract; synthetic records, no database/RLS qualification."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from cortex_v2.project_registry import list_projects
from cortex_v2.store import ApiProblem

SCOPE = UUID('00000000-0000-0000-0000-000000000071')
PRIMARY = {'path': '/fixture/project', 'kind': 'primary', 'repo_type': 'repo'}
REFERENCE = {'path': '/fixture/reference', 'kind': 'reference', 'source': {'preserve': [3, 1]}}
DATE = datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc)


class Connection:
    def __init__(self, roots, *, missing=False):
        self.scopes = [{'scope_id': SCOPE, 'alias': 'exact-key'}]
        self.row = {'project_scope_id': SCOPE, 'original_project_id': 'original-project-id',
                    'display_name': 'Exact name', 'default_agent': 'exact-agent',
                    'status': 'active', 'parent_project_key': 'exact-parent',
                    'repo_root': '/fixture/project', 'roots': json.dumps(roots),
                    'created_at': DATE, 'updated_at': DATE, 'agent_count': 7, 'profile_count': 9}
        self.missing = missing
        self.calls = []
    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if 'SELECT s.scope_id' in sql:
            return self.scopes
        return [] if self.missing else [self.row]
    async def execute(self, sql, *args):
        self.calls.append((sql, args))


def project_read(connection):
    return asyncio.run(list_projects(connection, SimpleNamespace(principal_id=SCOPE)))


def test_preserves_all_fields_order_reference_roots_and_original_records():
    roots = [REFERENCE, PRIMARY]
    connection = Connection(roots)
    original = dict(connection.row)
    assert project_read(connection) == [{
        'project_key': 'exact-key', 'project_id': 'original-project-id',
        'display_name': 'Exact name', 'default_agent': 'exact-agent', 'status': 'active',
        'parent_project_key': 'exact-parent', 'repo_root': '/fixture/project', 'roots': roots,
        'created_at': DATE.isoformat(), 'updated_at': DATE.isoformat(),
        'agent_count': 7, 'profile_count': 9,
    }]
    assert connection.row == original
    assert connection.calls[1][1] == (str(SCOPE),)
    assert connection.calls[2][1] == ([SCOPE],)


@pytest.mark.parametrize('other', [None, False, 8, 'root', [], {},
    {'path': '/fixture/reference'}, {'kind': 'reference'},
    {'path': None, 'kind': 'reference'}, {'path': 7, 'kind': 'reference'},
    {'path': '', 'kind': 'reference'}, {'path': '/fixture/reference', 'kind': None},
    {'path': '/fixture/reference', 'kind': 7}, {'path': '/fixture/reference', 'kind': ''},
])
def test_malformed_other_root_is_typed_conflict_instead_of_silent_success(other):
    with pytest.raises(ApiProblem) as refused:
        project_read(Connection([PRIMARY, other]))
    assert refused.value.status == 409
    assert refused.value.code == 'project_registry_incomplete'


@pytest.mark.parametrize('roots', [[], [REFERENCE], [PRIMARY, PRIMARY],
    [{'path': '/fixture/wrong', 'kind': 'primary'}]])
def test_existing_primary_root_conflicts_remain_typed(roots):
    with pytest.raises(ApiProblem) as refused:
        project_read(Connection(roots))
    assert refused.value.status == 409 and refused.value.code == 'project_registry_incomplete'


def test_missing_preservation_row_remains_typed_conflict():
    with pytest.raises(ApiProblem) as refused:
        project_read(Connection([PRIMARY], missing=True))
    assert refused.value.status == 409 and refused.value.code == 'project_registry_incomplete'
