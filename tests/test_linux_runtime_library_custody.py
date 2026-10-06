"""Compiled interpreter must run beyond its bootstrap environment; literal command seams."""
import json
from pathlib import Path
from types import SimpleNamespace
from test_linux_builder_contract import load

def test_linux_bootstrap_binds_shared_python_rpath_and_ignores_ambient_compiler(tmp_path,monkeypatch):
    m=load('bootstrap-linux-runtime.py',monkeypatch);output=tmp_path/'runtime';calls=[]
    monkeypatch.setattr(m.platform,'system',lambda:'Linux');monkeypatch.setattr(m.platform,'machine',lambda:'x86_64')
    monkeypatch.setattr(m.platform,'libc_ver',lambda:('glibc','2.35'))
    monkeypatch.setattr(m.platform,'freedesktop_os_release',lambda:{'ID':'ubuntu','VERSION_ID':'22.04'})
    monkeypatch.setenv('CC','/poison/compiler');monkeypatch.setenv('CXX','/poison/compiler')
    original=Path.is_file
    monkeypatch.setattr(Path,'is_file',lambda p:True if str(p)=='/etc/ssl/certs/ca-certificates.crt' else original(p))
    monkeypatch.setattr(m,'verify_archive',lambda *args:None)
    def run(args,**kwargs):
        calls.append((args,kwargs))
        if args[0]=='/usr/bin/tar':
            for item in m.INPUTS['python'],m.INPUTS['openssl']:
                directory=output/item['directory'];directory.mkdir(exist_ok=True);(directory/item['license']).write_text('public fixture notice')
        value=json.dumps({'python':'3.12.14','architecture':'x86_64','openssl':'OpenSSL 3.5.8 public fixture'}) if '-c' in args else 'gcc public fixture' if args==['/usr/bin/gcc','--version'] else ''
        return SimpleNamespace(stdout=value)
    monkeypatch.setattr(m.subprocess,'run',run);m.build(output)
    configure=next(a for a,k in calls if a[0]=='./configure')
    assert 'LDFLAGS=-Wl,-rpath,'+str(output/'python/lib') in configure
    assert all('CC' not in k['env'] and 'CXX' not in k['env'] for a,k in calls)

def test_linux_help_qualification_has_no_builder_library_fallback(tmp_path,monkeypatch):
    m=load('build-candidate.py',monkeypatch)
    assert callable(getattr(m,'run_linux_qualification',None)), 'sterile native help environment absent'
    for name in ('LD_LIBRARY_PATH','LD_PRELOAD','LD_AUDIT','PYTHONHOME','PYTHONPATH'):
        monkeypatch.setenv(name,'/poison/library')
    calls=[]
    def run(args,**kwargs):calls.append((args,kwargs));return SimpleNamespace(stdout='public report\n')
    monkeypatch.setattr(m.subprocess,'run',run)
    assert m.run_linux_qualification(['readelf','--version-info',str(tmp_path/'binary')],read=True)=='public report'
    env=calls[0][1]['env'];assert '/poison/library' not in env.values()
    assert env['PATH']=='/usr/bin:/bin:/usr/sbin:/sbin' and env['LC_ALL']=='C'
