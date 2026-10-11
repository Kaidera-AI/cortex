"""Actual CLI + actual Claude parser, fake API transport into owned schema PG."""
import json,os,shutil,subprocess,uuid
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
BASH=os.environ.get('CORTEX_REBUILD_TEST_BASH','bash')
MANUAL='10000000-0000-0000-0000-000000000001'
CLAUDE='10000000-0000-0000-0000-000000000002'
CODEX='10000000-0000-0000-0000-000000000003'
LEGACY='10000000-0000-0000-0000-000000000004'
ORPHAN='10000000-0000-0000-0000-000000000005'

@pytest.fixture
def proof(tmp_path):
    name=os.environ.get('CORTEX_REBUILD_TEST_CONTAINER')
    if not name:pytest.skip('explicit disposable schema database required')
    info=json.loads(subprocess.check_output(['podman','inspect',name],text=True))[0]
    assert info['Config']['Labels'].get('io.kaidera.proof')=='bob-cli3-rebuild-20261010'
    assert info['HostConfig']['NetworkMode']=='none' and not info['HostConfig'].get('PortBindings')
    def sql(s):return subprocess.check_output(['podman','exec','-i',name,'psql','-U','postgres','-At','-v','ON_ERROR_STOP=1'],input=s,text=True).strip()
    sql("INSERT INTO cortex_projects(project_key,display_name,repo_root) VALUES('synthetic','Proof','/synthetic'),('other-proof','Other','/other') ON CONFLICT(project_key) DO NOTHING; DELETE FROM messages WHERE project IN ('synthetic','other-proof'); DELETE FROM agent_sessions WHERE project IN ('synthetic','other-proof');")
    for f in ['cortex-rebuild-history','cortex-ingest-session']:
        shutil.copyfile(ROOT/f,tmp_path/f);(tmp_path/f).chmod(0o755)
    helper=ROOT/'cortex_history_plan.py'
    if helper.exists():shutil.copyfile(helper,tmp_path/helper.name)
    sessions=tmp_path/'sessions';sessions.mkdir()
    transcript=sessions/(CLAUDE+'.jsonl');transcript.write_text('{"role":"user","content":"NEW-SYNTHETIC"}\n')
    codex=tmp_path/('rollout-'+CODEX+'.jsonl');codex.write_text('{}\n')
    sql(f"INSERT INTO agent_sessions(id,project,notes) VALUES('{MANUAL}','synthetic','{{\"source\":\"manual-save-chat\"}}'),('{CLAUDE}','synthetic','{{}}'),('{CODEX}','synthetic','{{}}'),('{LEGACY}','synthetic','{{}}'),('{ORPHAN}','synthetic','{{}}'); INSERT INTO messages(session_id,project,agent_name,role,content,metadata) SELECT id,project,'synthetic-agent','system','OLD-SYNTHETIC',CASE WHEN id='{MANUAL}' THEN '{{\"source\":\"manual-save-chat\"}}'::jsonb WHEN id='{CLAUDE}' THEN '{{\"source\":\"local-file\"}}'::jsonb ELSE '{{}}'::jsonb END FROM agent_sessions WHERE project='synthetic';")
    sql(f"INSERT INTO session_sources(session_id,project,source_path,provider,agent_name,source_kind) VALUES('{CLAUDE}','synthetic','{transcript}','claude','synthetic-agent','claude-session'),('{CODEX}','synthetic','{codex}','codex','synthetic-agent','codex-session'),('{ORPHAN}','synthetic','/missing/synthetic.jsonl','claude','synthetic-agent','claude-session');")
    (tmp_path/'_cortex_lib.sh').write_text('''CORTEX_PROJECT=synthetic
PROJECT_ROOT=/synthetic
cortex_normalize_agent_name(){ printf '%s\\n' "$1"; }
sql_escape(){ printf '%s' "$1"; }
discover_claude_dirs(){ printf '%s\\n' "$PROOF_CLAUDE_DIR"; }
discover_codex_sessions(){ [ -z "$PROOF_CODEX" ] || printf '%s\\n' "$PROOF_CODEX"; }
build_codex_agent_map(){ :; }
pg_query(){ podman exec -i "$PROOF_CONTAINER" psql -U postgres -At -v ON_ERROR_STOP=1 -c "$1"; }
pg_exec_file(){ podman exec -i "$PROOF_CONTAINER" psql -U postgres -At -v ON_ERROR_STOP=1 < "$1"; }
''')
    # Actual parser payload, transported only to fake API implementing its bounded
    # same-project/session upsert+message-replace contract in the throwaway DB.
    ingest=tmp_path/'ingest.py';ingest.write_text('''import json,os,subprocess
p=json.load(__import__('sys').stdin)
def q(s):return "'"+str(s).replace("'","''")+"'"
id=q(p['session_uuid']); project="'synthetic'"
s="BEGIN; INSERT INTO agent_sessions(id,project,notes) VALUES("+id+","+project+",'{}') ON CONFLICT(id) DO NOTHING; DELETE FROM messages WHERE session_id="+id+" AND project="+project+";"
for m in p['messages']:
 s+="INSERT INTO messages(session_id,project,agent_name,role,content,metadata) VALUES("+id+","+project+",'synthetic-agent','human',"+q(m['content'])+","+q(json.dumps(m['metadata']))+"::jsonb);"
s+="COMMIT;"
subprocess.run(['podman','exec','-i',os.environ['PROOF_CONTAINER'],'psql','-U','postgres','-At','-v','ON_ERROR_STOP=1'],input=s,text=True,check=True,stdout=subprocess.DEVNULL)
''')
    (tmp_path/'_cortex_api.sh').write_text('''cortex_agent_base_name(){ printf '%s\\n' "$1"; }
cortex_api_call_json(){ printf '%s' "$3" | python3 "$PROOF_INGEST"; }
''')
    shim=tmp_path/'mktemp';shim.write_text('#!/usr/bin/env python3\nimport tempfile\nprint(tempfile.mkstemp(prefix="rebuild-proof-",dir='+repr(str(tmp_path))+')[1])\n');shim.chmod(0o755)
    env={'PATH':str(Path(BASH).parent)+':'+str(tmp_path)+':'+os.environ['PATH'],'HOME':os.environ['HOME'],'CORTEX_AGENT':'synthetic-agent','PROOF_CONTAINER':name,'PROOF_CLAUDE_DIR':str(sessions),'PROOF_CODEX':'','PROOF_INGEST':str(ingest)}
    def run(*args):return subprocess.run([BASH,str(tmp_path/'cortex-rebuild-history'),*args],env=env,text=True,capture_output=True)
    yield tmp_path,env,sql,run
    sql("DELETE FROM messages WHERE project IN ('synthetic','other-proof'); DELETE FROM agent_sessions WHERE project IN ('synthetic','other-proof');")


def test_manual_unknown_untagged_and_codex_rows_survive_actual_rebuild(proof):
    _,env,sql,run=proof;r=run();assert r.returncode==0,r.stderr
    for id in [MANUAL,LEGACY,CODEX,ORPHAN]:assert sql(f"SELECT count(*) FROM messages WHERE session_id='{id}' AND content='OLD-SYNTHETIC';")=='1',r.stdout
    assert sql(f"SELECT content FROM messages WHERE session_id='{CLAUDE}';")=='NEW-SYNTHETIC'
    r=run();assert r.returncode==0
    assert sql(f"SELECT count(*) FROM messages WHERE session_id='{CLAUDE}';")=='1'


def test_dry_run_identifies_replaced_preserved_codex_and_orphan_without_mutation(proof):
    _,env,sql,run=proof;r=run('--dry-run');assert r.returncode==0,r.stderr
    assert f'REPLACE {CLAUDE}' in r.stdout
    for id in [MANUAL,LEGACY,CODEX,ORPHAN]:assert f'PRESERVE {id}' in r.stdout
    assert 'parser-unavailable' in r.stdout and 'ORPHAN' in r.stdout
    assert sql(f"SELECT content FROM messages WHERE session_id='{CLAUDE}';")=='OLD-SYNTHETIC'


def test_discovered_codex_is_preserved_and_reported_without_parser(proof):
    _,env,sql,run=proof;env['PROOF_CODEX']=str(Path(env['PROOF_CLAUDE_DIR']).parent/('rollout-'+CODEX+'.jsonl'))
    r=run();assert r.returncode==0,r.stderr
    assert f'PRESERVE {CODEX}' in r.stdout and 'parser-unavailable' in r.stdout
    assert sql(f"SELECT count(*) FROM messages WHERE session_id='{CODEX}';")=='1'


def test_untagged_identity_collision_is_refused_and_preserved_not_duplicated(proof):
    root,env,sql,run=proof;(Path(env['PROOF_CLAUDE_DIR'])/(LEGACY+'.jsonl')).write_text('{"role":"user","content":"DO-NOT-REPLACE"}\n')
    r=run();assert r.returncode==0,r.stderr
    assert f'REFUSED {LEGACY}' in r.stdout
    assert sql(f"SELECT count(*) FROM messages WHERE session_id='{LEGACY}' AND content='OLD-SYNTHETIC';")=='1'


def test_unreadable_source_is_reported_preserved(proof):
    root,env,sql,run=proof;p=Path(env['PROOF_CLAUDE_DIR'])/(CLAUDE+'.jsonl');p.chmod(0)
    try:
        r=run();assert r.returncode==0,r.stderr
        assert f'PRESERVE {CLAUDE}' in r.stdout and 'unreadable' in r.stdout
        assert sql(f"SELECT content FROM messages WHERE session_id='{CLAUDE}';")=='OLD-SYNTHETIC'
    finally:p.chmod(0o600)

def test_unknown_message_source_and_cross_project_identity_are_not_replaced(proof):
    root,env,sql,run=proof
    sql(f"UPDATE messages SET metadata='{{\"source\":\"unknown-source\"}}' WHERE session_id='{CLAUDE}'; INSERT INTO agent_sessions(id,project,notes) VALUES('10000000-0000-0000-0000-000000000006','other-proof','{{}}'); INSERT INTO messages(session_id,project,agent_name,role,content) VALUES('10000000-0000-0000-0000-000000000006','other-proof','synthetic-agent','system','OTHER-PROJECT');")
    (Path(env['PROOF_CLAUDE_DIR'])/'10000000-0000-0000-0000-000000000006.jsonl').write_text('{"role":"user","content":"DO-NOT-MOVE-PROJECT"}\n')
    r=run();assert r.returncode==0,r.stderr
    assert f'REFUSED {CLAUDE}' in r.stdout and 'cross-project' in r.stdout
    assert sql(f"SELECT content FROM messages WHERE session_id='{CLAUDE}';")=='OLD-SYNTHETIC'
    assert sql("SELECT project FROM agent_sessions WHERE id='10000000-0000-0000-0000-000000000006';")=='other-proof'
