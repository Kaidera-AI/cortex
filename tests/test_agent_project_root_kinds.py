"""The project presenter preserves valid reference roots from migration 0015."""
from cortex_v2.cli.agent_request import format_projects
from test_agent_project_shim import PROJECT, legacy_table


def test_reference_roots_remain_valid_legacy_output(tmp_path):
    row = dict(PROJECT, roots=[dict(PROJECT['roots'][0]),
                              {'path': '/fixtures/reference', 'kind': 'reference', 'repo_type': 'repo'}])
    data = {'projects': [row]}
    assert format_projects(data) == legacy_table(tmp_path, data)
