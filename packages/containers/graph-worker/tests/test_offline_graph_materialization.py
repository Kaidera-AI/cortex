"""RED-first: real worker may not call schema-only/failed parsing materialized."""
import importlib.util,sqlite3
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
def worker():
 spec=importlib.util.spec_from_file_location('offline_worker',ROOT/'worker.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
def prepare(tmp_path,monkeypatch,result,nodes=0):
 m=worker();m.GRAPHS_DIR=tmp_path/'graphs';repo=tmp_path/'projects/fixture';(repo/'.git').mkdir(parents=True);m.PROJECTS_DIR=repo.parent;m.HOST_PROJECTS_ROOT=str(repo.parent);m.REGISTERED_PROJECTS_ROOT=str(repo.parent)
 monkeypatch.setattr(m,'_resolve_repo',lambda *a:repo);monkeypatch.setattr(m,'_has_git_repo',lambda *a:True);monkeypatch.setattr(m,'_tool_preamble',lambda *a,**kw:'')
 def bcrg(*a,**kw):
  db=m.GRAPHS_DIR/'fixture/graph.db';c=sqlite3.connect(db);c.execute('CREATE TABLE nodes(id INTEGER)');c.execute('CREATE TABLE edges(id INTEGER)');c.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)');c.execute("INSERT INTO metadata VALUES('files_parsed','1')")
  for i in range(nodes):c.execute('INSERT INTO nodes VALUES(?)',(i,))
  c.commit();c.close();return result
 monkeypatch.setattr(m,'_run_bcrg',bcrg);return m,m.BuildBody(repo=str(repo),registered_repo=str(repo),storage_key='fixture',full=True,embed=False)
def test_missing_grammar_is_typed_systemic_failure(tmp_path,monkeypatch):
 result={'status':'ok','files_parsed':1,'total_nodes':0,'total_edges':0,'errors':[{'file':'fixture.py','error':"Tree-sitter grammar for 'python' could not be loaded"}]};m,b=prepare(tmp_path,monkeypatch,result)
 with pytest.raises(m.HTTPException) as exc:m._build_graph(b)
 assert exc.value.status_code==503 and exc.value.detail['code']=='graph_environment_error'
def test_status_error_is_typed_failure(tmp_path,monkeypatch):
 m,b=prepare(tmp_path,monkeypatch,{'status':'error','error':'provisioning failure'})
 with pytest.raises(m.HTTPException) as exc:m._build_graph(b)
 assert exc.value.detail['code']=='graph_environment_error'
def test_no_supported_sources_is_never_ok(tmp_path,monkeypatch):
 m,b=prepare(tmp_path,monkeypatch,{'status':'ok','files_parsed':0,'total_nodes':0,'errors':[]})
 with pytest.raises(m.HTTPException) as exc:m._build_graph(b)
 assert exc.value.detail['code']=='no_supported_sources'
def test_schema_only_database_refused(tmp_path,monkeypatch):
 m,b=prepare(tmp_path,monkeypatch,{'status':'ok','files_parsed':1,'total_nodes':0,'errors':[]});m._prepare_materialization_graph_db('fixture');m._run_bcrg('')
 with pytest.raises(m.HTTPException) as exc:m._require_populated_graph_db('fixture')
 assert exc.value.detail['code']=='graph_not_populated'
def test_content_error_counted_warning_if_nodes_exist(tmp_path,monkeypatch):
 m,b=prepare(tmp_path,monkeypatch,{'status':'ok','files_parsed':2,'total_nodes':1,'errors':[{'file':'bad.py','error':'invalid source syntax'}]},nodes=1);r=m._build_graph(b)
 assert r['warning_count']==1 and r['warnings'][0]['file']=='bad.py' and r['status']=='ok'
def test_clean_populated_graph_succeeds(tmp_path,monkeypatch):
 m,b=prepare(tmp_path,monkeypatch,{'status':'ok','files_parsed':1,'total_nodes':1,'errors':[]},nodes=1);assert m._build_graph(b)['status']=='ok'
