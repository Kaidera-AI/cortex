"""Adversarial public error disclosure contract, Kai 03:03."""
import json
import pytest
from test_ci_db_start_diagnostics_r428 import load,fixture

@pytest.mark.parametrize('line,pattern,hidden',[
 ('Error: OCI runtime start failed password=PRIVATE_VALUE','assignment','PRIVATE_VALUE'),
 ('Error: secret unsupported token="PRIVATE VALUE"','assignment','PRIVATE VALUE'),
 ('Error: conmon failed dsn = postgresql://user:PRIVATE_VALUE@host/db','assignment','PRIVATE_VALUE'),
 ('Error: OCI create https://user:PRIVATE_VALUE@host/x failed','url','PRIVATE_VALUE'),
 ('Error: runtime value '+ 'a'*64,'hex','a'*64),
 ('Error: runtime value QWxhZGRpbjpvcGVuIHNlc2FtZQ==','base64','QWxhZGRpbjpvcGVuIHNlc2FtZQ=='),
 ('Error: netavark failed token=PRIVATE_VALUE trailing','assignment','PRIVATE_VALUE'),
 ('Error: secret key password plain words key=PRIVATE_VALUE','assignment','PRIVATE_VALUE'),
])
def test_values_redacted_words_preserved(line,pattern,hidden,tmp_path,monkeypatch):
 m=load(monkeypatch);e,r,root,rt,conf,paths,calls=fixture(tmp_path);ctx=m.attach(e,r,root,runtime_receipt=rt,config=conf)
 assert hasattr(ctx,'disclose'),'token disclosure missing'
 value=ctx.disclose(line.encode(),125)
 assert value['redacted_line'].startswith('Error:') and len(value['redacted_line'])<=400
 assert pattern in value['patterns'] and '<redacted:' in value['redacted_line']
 assert hidden not in json.dumps(value)

@pytest.mark.parametrize('text,family',[
 ('secret not found','secret'),('OCI runtime create failed','oci'),('conmon refused','conmon'),
 ('netavark failed','network'),('pasta failed','network'),('cgroup failed','cgroup'),
 ('permission denied EACCES','permission'),('no such file ENOENT','missing'),
 ('overlay storage failed','storage'),('user namespace subuid failed','userns')])
def test_allowlisted_families_and_plain_words(text,family,tmp_path,monkeypatch):
 m=load(monkeypatch);e,r,root,rt,conf,paths,calls=fixture(tmp_path);ctx=m.attach(e,r,root,runtime_receipt=rt,config=conf)
 assert hasattr(ctx,'disclose'),'family disclosure missing'
 line='Error: '+text+' secret key token '+ctx.namespace+'-db-owner-password'
 value=ctx.disclose(line.encode(),125)
 assert value['family']==family and value['redacted_line']==line and value['patterns']==[]

@pytest.mark.parametrize('raw',[b'Error: \x00PRIVATE_VALUE',b'Error: \xffPRIVATE_VALUE',b'x'*70000,b'private unclassified PRIVATE_VALUE',b'Error: bearer PRIVATE_VALUE'])
def test_unsafe_unclassified_values_emit_no_line(raw,tmp_path,monkeypatch):
 m=load(monkeypatch);e,r,root,rt,conf,paths,calls=fixture(tmp_path);ctx=m.attach(e,r,root,runtime_receipt=rt,config=conf)
 assert hasattr(ctx,'disclose'),'safe fallback missing'
 value=ctx.disclose(raw,125);assert value['redacted_line'] is None and 'PRIVATE_VALUE' not in json.dumps(value)

@pytest.mark.parametrize('line', ['Error: OCI start password="PRIVATE VALUE', "Error: OCI start token='PRIVATE VALUE", 'Error: runtime password: PRIVATE_VALUE', 'Error: OCI start '+ 'public '*100+'token=PRIVATE_VALUE'])
def test_ambiguous_or_late_values_do_not_escape(line,tmp_path,monkeypatch):
 m=load(monkeypatch);e,r,root,rt,conf,paths,calls=fixture(tmp_path);ctx=m.attach(e,r,root,runtime_receipt=rt,config=conf)
 assert hasattr(ctx,'disclose'),'bounded disclosure missing'
 value=ctx.disclose(line.encode(),125)
 assert 'PRIVATE' not in json.dumps(value)
 assert value['redacted_line'] is None or len(value['redacted_line'])<=400

def test_actual_capture_receipt_emits_sanitized_line(tmp_path,monkeypatch):
 from types import SimpleNamespace
 m=load(monkeypatch);e,r,root,rt,conf,paths,calls=fixture(tmp_path);ctx=m.attach(e,r,root,runtime_receipt=rt,config=conf)
 def child(command,**kwargs):
  kwargs['stderr'].write(b'Error: OCI runtime start token=PRIVATE_VALUE failed');return SimpleNamespace(returncode=125)
 monkeypatch.setattr(m.subprocess,'run',child)
 with pytest.raises(RuntimeError):e.run(['start',ctx.container])
 value=json.loads(next(root.glob('start-diagnostic-*.json')).read_text())
 assert value['family']=='oci' and value['patterns']==['assignment']
 assert value['redacted_line']=='Error: OCI runtime start <redacted:assignment> failed'
 assert 'PRIVATE_VALUE' not in json.dumps(value)

@pytest.mark.parametrize('assignment',['key=PRIVATE_VALUE','dsn=PRIVATE_VALUE','key="PRIVATE VALUE"'])
def test_no_legacy_field_bypasses_token_scan(assignment,tmp_path,monkeypatch):
 from types import SimpleNamespace
 m=load(monkeypatch);e,r,root,rt,conf,paths,calls=fixture(tmp_path);ctx=m.attach(e,r,root,runtime_receipt=rt,config=conf)
 def child(command,**kwargs):
  kwargs['stderr'].write(('Error: OCI runtime start '+assignment+' failed').encode());return SimpleNamespace(returncode=125)
 monkeypatch.setattr(m.subprocess,'run',child)
 with pytest.raises(RuntimeError):e.run(['start',ctx.container])
 value=json.loads(next(root.glob('start-diagnostic-*.json')).read_text())
 assert 'PRIVATE' not in json.dumps(value)
 assert value['first_error'] is None and value['redacted'] is True
 assert value['patterns']==['assignment']
