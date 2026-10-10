"""Actual rebuild CLI against artificial rows in an isolated disposable Postgres.

Opt-in CORTEX_REBUILD_TEST_CONTAINER refers to a task-owned labelled resource only.
The fixture validates ownership label/no published ports before issuing any SQL.
"""
import os,shutil,subprocess,json
from pathlib import Path
import pytest
BASH=os.environ.get('CORTEX_REBUILD_TEST_BASH','bash')
SOURCE=Path(__file__).resolve().parents[1]/'cortex-rebuild-history'

@pytest.fixture
def database():
    name=os.environ.get('CORTEX_REBUILD_TEST_CONTAINER')
    if not name:pytest.skip('explicit disposable database required')
    info=json.loads(subprocess.check_output(['podman','inspect',name],text=True))[0]
    assert info['Config']['Labels'].get('io.kaidera.proof')=='bob-cli3-rebuild-20261010'
    assert info['HostConfig']['NetworkMode']=='none'
    assert not info['HostConfig'].get('PortBindings')
    def sql(statement):
        return subprocess.check_output(['podman','exec','-i',name,'psql','-U','postgres','-d','postgres','-v','ON_ERROR_STOP=1','-At'],input=statement,text=True).strip()
    sql('DROP TABLE IF EXISTS messages,agent_sessions; CREATE TABLE agent_sessions(id text PRIMARY KEY,project text); CREATE TABLE messages(id text PRIMARY KEY,session_id text,project text); INSERT INTO agent_sessions VALUES(\'old-session\',\'synthetic\'); INSERT INTO messages VALUES(\'old-message\',\'old-session\',\'synthetic\');')
    yield name,sql
    sql('DROP TABLE IF EXISTS messages,agent_sessions;')

@pytest.fixture
def projection(tmp_path,database):
    name,sql=database;script=tmp_path/'cortex-rebuild-history';shutil.copyfile(SOURCE,script)
    codex=tmp_path/'rollout-00000000-0000-0000-0000-000000000001.jsonl';codex.write_text('{}\n')
    claude=tmp_path/'claude.jsonl';claude.write_text('{}\n')
    (tmp_path/'_cortex_lib.sh').write_text('''CORTEX_PROJECT=synthetic
PROJECT_ROOT=/synthetic
cortex_normalize_agent_name(){ printf '%s\\n' "$1"; }
sql_escape(){ printf '%s' "$1"; }
discover_claude_dirs(){ [ -z "${PROOF_CLAUDE_DIR:-}" ] || printf '%s\\n' "$PROOF_CLAUDE_DIR"; }
discover_codex_sessions(){ printf '%s\\n' "$PROOF_CODEX"; }
build_codex_agent_map(){ :; }
pg_query(){ podman exec -i "$PROOF_CONTAINER" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -At -c "$1"; }
pg_exec_file(){ podman exec -i "$PROOF_CONTAINER" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -At < "$1"; }
''')
    shim=tmp_path/'mktemp'
    shim.write_text('#!/usr/bin/env python3\nimport tempfile\nprint(tempfile.mkstemp(prefix="cli3-proof-",dir='+repr(str(tmp_path))+')[1])\n')
    shim.chmod(0o755)
    env={'PATH':str(tmp_path)+':'+os.environ['PATH'],'HOME':os.environ['HOME'],'CORTEX_AGENT':'synthetic-agent','PROOF_CONTAINER':name,'PROOF_CODEX':str(codex)}
    return tmp_path,script,env,sql

@pytest.mark.parametrize('kind',['codex','claude'])
@pytest.mark.parametrize('mode',['missing','not-executable'])
def test_missing_required_parser_preserves_existing_history(projection,mode,kind):
    root,script,env,sql=projection
    parser='cortex-ingest-codex' if kind=='codex' else 'cortex-ingest-session'
    if kind=='claude':
        env['PROOF_CODEX']='';directory=root/'claude';directory.mkdir();(directory/'synthetic.jsonl').write_text('{}\n');env['PROOF_CLAUDE_DIR']=str(directory)
    if mode=='not-executable':(root/parser).write_text('#!/bin/sh\nexit 0\n')
    result=subprocess.run([BASH,str(script)],env=env,text=True,capture_output=True)
    assert result.returncode!=0
    assert sql('SELECT count(*) FROM agent_sessions;')=='1',result.stdout+result.stderr
    assert sql('SELECT count(*) FROM messages;')=='1',result.stdout+result.stderr


def test_dry_run_preserves_history_without_parser(projection):
    _,script,env,sql=projection;result=subprocess.run([BASH,str(script),'--dry-run'],env=env,text=True,capture_output=True)
    assert result.returncode==0
    assert sql('SELECT count(*) FROM messages;')=='1'


def test_no_codex_inputs_does_not_require_codex_parser(projection):
    _,script,env,sql=projection;env['PROOF_CODEX']=''
    result=subprocess.run([BASH,str(script)],env=env,text=True,capture_output=True)
    assert result.returncode==0
    assert sql('SELECT count(*) FROM messages;')=='0'
