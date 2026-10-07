"""Actual context SQL must keep boot rows out of ordinary scope preparation."""
import uuid
import pytest
from fixtures.agent_boot_native_r425 import boot_database
from fixtures.state_import_native import state_cluster
from test_agent_boot_metadata_sql_r425 import insert, manifest, native
from cortex_v2.interface.context import prepare_context, select_persona_revision
from cortex_v2.store import Principal, Scope, ScopeContext

@native
@pytest.mark.parametrize('revision', (None, 1))
async def test_ordinary_persona_selection_ignores_newer_agent_boot(boot_database, revision):
    async with boot_database() as fixture:
        legacy = await insert(fixture, 'persona', None)
        private = await insert(fixture, 'persona', manifest('persona'), audience='agent_boot')
        selected = await select_persona_revision(fixture['db'], fixture['scope'], revision)
        assert selected['persona_id'] == legacy['persona_id']
        assert selected['persona_id'] != private['persona_id']

@native
async def test_actual_context_prepare_excludes_agent_boot_mandatory_rule(boot_database):
    async with boot_database() as fixture:
        legacy = await insert(fixture, 'rule', None)
        private = await insert(fixture, 'rule', manifest('rule'), audience='agent_boot')
        scope = Scope(alias='public-project', scope_id=fixture['scope'], kind='project',
                      can_read=True, can_write=True, can_publish=False)
        context = ScopeContext(principal=Principal(principal_id=fixture['principal'],
                               installation_id=fixture['installation']), selected=scope, read_scopes=(scope,))
        async with fixture['db'].transaction():
            status, receipt, replayed = await prepare_context(fixture['db'], context,
                'PUBLIC-audience-check', {'intent':'prose_edit','budget_bytes':8192,'recall_limit':0}, {})
        assert (status, replayed) == (201, False)
        rules = receipt['context']['mandatory_rules']['items']
        assert [row['rule_id'] for row in rules] == [str(legacy['rule_id'])]
        assert all(row['rule_id'] != str(private['rule_id']) for row in rules)
