"""R110 dynamic expressions: explicit source bridge admission, never installed proof."""
import io
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

from cortex_v2.cli import agent_request as bridge
from test_agent_frozen_caller import project_server, runners  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
UNAVAILABLE = 'ERROR: facade request unavailable in this release\n'
HELPER = ROOT / 'scripts/agent-shims/_cortex_api.sh'


def shell(function, *arguments, env=None):
    # Data stays in positional argv; no interpolation, eval or file/credential IO.
    base = dict(os.environ)
    for key in ('CORTEX_CONNECTION_PROFILE', 'CORTEX_API_PAYLOAD_FILE',
                'CORTEX_CTO_OVERRIDE', 'CORTEX_API_WITH_ADMIN', 'CORTEX_AGENT_ID'):
        base.pop(key, None)
    base.update(env or {})
    return subprocess.run(['bash', '-eu', '-c', 'source "$1"; shift; "$@"',
                           'fixture', str(HELPER), function, *arguments],
                          env=base, capture_output=True, text=True, timeout=10)


DYNAMIC_CASES = [
    ('GET', '/boot/worker?budget=1200&full=true&query=public%20marker', ''),
    ('GET', '/bootstrap/worker?budget=1200', ''),
    ('GET', '/brief/worker?budget=1024', ''),
    ('GET', '/diary/worker/stats', ''),
    ('POST', '/diary/worker', '{"entry":"public marker"}'),
    ('GET', '/beat/embeddings/jobs/public-id', ''),
    ('GET', '/graph/build/jobs/public-id', ''),
    ('GET', '/cortex-graph-search?q=x%2Fy&expand=true', ''),
    ('GET', '/handoffs/public-prefix', ''),
    ('POST', '/handoffs/public-prefix/return', '{"summary":"public marker"}'),
    ('POST', '/handoffs/public-prefix/claim', '{}'),
    ('POST', '/handoffs/public-prefix/complete', ''),
    ('POST', '/handoffs/public-prefix/release', '{}'),
    ('GET', '/history?agent=worker&since=2026-10-05', ''),
    ('GET', '/projects/fixture-project/runtime', ''),
    ('POST', '/knowledge/ingest', '{"source":"original"}'),
    ('POST', '/invalidate/public-id', '{}'),
    ('GET', '/onboard/diagnostics?agent=worker', ''),
    ('GET', '/agents/worker/persona', ''),
    ('PATCH', '/projects/fixture-project', '{"repo_root":"/original"}'),
    ('POST', '/save-chat/worker', '{"messages":[]}'),
    ('GET', '/search?q=a%26b&hall=project&rerank=false', ''),
    ('POST', '/skills/public-slug/bind', '{"subject":"worker"}'),
    ('GET', '/beat/events?after=public-cursor', ''),
    ('GET', '/verify/table/memory%2Fother', ''),
    ('GET', '/work-products/public-prefix', ''),
    ('GET', '/board?status=claimed', ''),
    ('PATCH', '/board/public-id', '{"status":"done"}'),
    ('POST', '/artifacts/describe-image', ''),
    ('GET', '/projects?scope=other', ''),
    ('GET', '/projects?', ''),
    ('GET', '/projects#fragment', ''),
    ('GET', '/projects/', ''),
    ('GET', '/%70rojects', ''),
    ('GET', '//foreign.example/projects', ''),
    ('GET', 'https://public-marker@foreign.example/projects', ''),
    ('get', '/projects', ''),
    ('DELETE', '/projects', ''),
    ('GET', '/projects', '{}'),
    ('GET', '/projects', ' '),
]


@pytest.mark.parametrize('function', ['cortex_api_call', 'cortex_api_call_json', 'cortex_api_json'])
@pytest.mark.parametrize('method,path,payload', DYNAMIC_CASES)
def test_dynamic_refusal_before_profile_or_transport(function, method, path, payload):
    result = shell(function, method, path, payload)
    assert (result.returncode, result.stdout, result.stderr) == (2, '', UNAVAILABLE)


@pytest.mark.parametrize('arguments', [(), ('GET',), ('PATCH', '/projects'),
    ('POST', '/projects', '{}'), ('PUT', '/projects', ''),
    ('GET', '/projects', 'scope=other'), ('GET', '/projects', ''),
    ('GET', '/projects?scope=other')])
def test_dynamic_legacy_wrapper_refuses_undeclared_method_query_body(arguments):
    result = shell('cortex_api', *arguments)
    assert (result.returncode, result.stdout, result.stderr) == (2, '', UNAVAILABLE)


@pytest.mark.parametrize('function', ['cortex_api_call_to_file', 'cortex_api_download_admin'])
def test_file_and_form_adapters_refuse_before_file_or_profile(tmp_path, function):
    protected = tmp_path / 'existing-public-output'
    protected.write_text('preserve original')
    args = ('POST', '/artifacts/parse-document', 'worker', str(protected), '1024', '30',
            '--form', 'artifact=@/missing/public-input') if function.endswith('to_file') else (
                '/admin/projects/fixture-project/export', str(protected), 'worker')
    result = shell(function, *args, env={'CORTEX_API_PAYLOAD_FILE': '/missing/public-input'})
    assert (result.returncode, result.stdout, result.stderr) == (2, '', UNAVAILABLE)
    assert protected.read_text() == 'preserve original'
    assert list(tmp_path.iterdir()) == [protected]


@pytest.mark.parametrize('function', ['cortex_api_call', 'cortex_api_call_json', 'cortex_api_json', 'cortex_api'])
@pytest.mark.parametrize('variable,value', [('CORTEX_API_PAYLOAD_FILE','/missing/public-input'),
    ('CORTEX_API_WITH_ADMIN','1'), ('CORTEX_CTO_OVERRIDE','public-decision')])
def test_dynamic_environment_cannot_change_admitted_get(function, variable, value):
    result = shell(function, 'GET', '/projects', env={variable:value})
    assert (result.returncode, result.stdout, result.stderr) == (2, '', UNAVAILABLE)


@pytest.mark.parametrize('function,safe', [('cortex_api_urlencode','/'), ('cortex_api_urlencode_strict','')])
@pytest.mark.parametrize('value', ['', 'worker@fixture-project', 'x/y?z&a=1', 'a b', 'café/雪', 'a\nb', '$(public marker)'])
def test_dynamic_encoding_is_exact_credential_free_data(function, safe, value):
    result = shell(function, value)
    assert (result.returncode, result.stdout, result.stderr) == (0, quote(value,safe=safe), '')


@pytest.mark.parametrize('name,expected', [('worker@fixture-project','worker'),
    ('worker:retired','worker'), ('worker@fixture-project:retired','worker'), ('worker','worker')])
def test_name_utility_only_normalizes_caller_string(name, expected):
    result = shell('cortex_agent_base_name', name)
    assert (result.returncode, result.stdout, result.stderr) == (0, expected, '')


@pytest.mark.parametrize('argv', [
    ['--con','/public/profile','api','GET','/projects'],
    ['api','GET','/projects','--ag','worker'],
    ['--config','/public/a','--config','/public/b','api','GET','/projects'],
    ['api','GET','/projects','--agent-name','owner','--agent-name','worker'],
    ['--config=/public/a','--config=/public/b','api','GET','/projects'],
    ['api','GET','/projects','--agent-name=owner','--agent-name=worker'],
])
def test_dynamic_selector_options_are_declared_and_unambiguous(monkeypatch, argv):
    def forbidden(*args, **kwargs):
        pytest.fail('undeclared/duplicate selectors reached profile or HTTP')
    monkeypatch.setattr(bridge,'load_member_profile',forbidden)
    monkeypatch.setattr(bridge,'http_request',forbidden)
    stdout,stderr=io.StringIO(),io.StringIO()
    code=bridge.main(argv,stdin=io.StringIO(),stdout=stdout,stderr=stderr)
    assert (code,stdout.getvalue(),stderr.getvalue())==(2,'',UNAVAILABLE)


@pytest.mark.parametrize('name,argv', [
    ('cortex-boot',['worker@fixture-project','--budget','2048','--query','x/y?z&a=1']),
    ('cortex-graph-search',['x/y?z&a=1','--expand','--low','--limit','7','--json']),
])
def test_exact_frozen_dynamic_callers_reach_unavailable_helper(tmp_path, name, argv):
    directory=tmp_path/'captured';directory.mkdir()
    shutil.copyfile(ROOT/'tests/fixtures'/('legacy-dynamic-'+name),directory/name)
    shutil.copyfile(HELPER,directory/'_cortex_api.sh')
    result=subprocess.run(['bash',str(directory/name),*argv],capture_output=True,text=True,
                          env=dict(os.environ,CORTEX_PROJECT='fixture-project'),timeout=10)
    assert (result.returncode,result.stdout,result.stderr)==(2,'',UNAVAILABLE)


@pytest.mark.parametrize('status',[200,403])
def test_dynamic_admitted_wrapper_uses_selected_member_once(tmp_path,project_server,status):
    origin,records,state=project_server;state['status']=status
    directory,env=runners(tmp_path,origin)
    result=subprocess.run(['bash','-eu','-c','source "$1"; method="$2"; path="$3"; cortex_api "$method" "$path"',
        'fixture',str(directory/'_cortex_api.sh'),'GET','/projects'],env=env,capture_output=True,text=True,timeout=10)
    assert len(records)==1
    assert records[0]=={'method':'GET','path':'/projects','selected_member':True,'scope':'fixture-project'}
    if status==200:
        assert result.returncode==0 and not result.stderr and 'Preserved project' in result.stdout
    else:
        assert result.returncode==22 and result.stdout=='' and result.stderr=='API error 403: selected_member_denied\n'
