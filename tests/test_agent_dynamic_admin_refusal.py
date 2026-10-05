"""R110 follow-up: unsupported operator calls keep the literal admission refusal."""
import pytest

from test_agent_dynamic_caller_contract import shell, UNAVAILABLE


@pytest.mark.parametrize('arguments', [(), ('GET', '/projects'),
    ('GET', '/beat/embeddings/jobs/public-id'),
    ('POST', '/skills/public-slug/bind', '{}'),
    ('GET', '/admin/projects/fixture-project/export'),
    ('POST', '/admin/projects/fixture-project/import', '{"archive":"public"}')])
def test_admin_adapter_unavailable_before_owner_profile_or_transport(arguments):
    result=shell('cortex_api_call_admin',*arguments,env={
        'CORTEX_CONNECTION_PROFILE':'/missing/public-profile',
        'CORTEX_ADMIN_TOKEN':'unissued-public-marker',
        'CORTEX_API_PAYLOAD_FILE':'/missing/public-input'})
    assert (result.returncode,result.stdout,result.stderr)==(2,'',UNAVAILABLE)
