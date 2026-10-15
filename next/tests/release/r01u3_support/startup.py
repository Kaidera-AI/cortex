"""TEST ONLY A observations and independent originals; never installed authority.

Small inert executable/native files are NOT executed. Observation substitutions
are SOURCE SIMULATION; the separate isolated-process controls use real CPython.
Existing B/C/D fixture originals and RAM signing DATA remain test-only.
"""
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import importlib.machinery as machinery
import os
from pathlib import Path
import stat
import sys
from r01u3_support.workspace import workspace_output
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from test_original_member_bridge import Fixture



MAIN=workspace_output()

class StartupFixture(Fixture):
    def boundary(self):
        api=self.api()
        for name in ('_StartupContext','_admit_startup','_installed_startup','_startup_observation'):
            self.assertTrue(callable(getattr(api,name,None)), 'SURFACE_ABSENCE_BODY: U3-E A '+name)
        return api

    def init_boundary(self):
        api=self.boundary()
        MAIN.mkdir(mode=0o700,parents=True,exist_ok=True)
        temp=tempfile.TemporaryDirectory(prefix='inert-a-',dir=MAIN)
        self.addCleanup(temp.cleanup);self.install=Path(temp.name)
        self.cache=self.install/'private-cache';self.cache.mkdir(mode=0o700)
        contents={
            'bin/python':('interpreter',b'INERT interpreter fixture, NOT EXECUTED\n',0o755),
            'entry.py':('source',b'# INERT entry fixture, NOT EXECUTED\n',0o644),
            'src/cortex_core/release/member_admission.py':('source',b'# INERT admitted source original\n',0o644),
            'stdlib/os.py':('stdlib',b'# INERT stdlib original\n',0o644),
            'stdlib/lib-dynload/loader.so':('loader',b'INERT loader original, NOT EXECUTED\n',0o644),
            'deps/cryptography/__init__.py':('crypto',b'# INERT sole provider original\n',0o644),
        }
        rows=[]
        for name,(role,raw,mode) in contents.items():
            path=self.install/name;path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            path.write_bytes(raw);path.chmod(mode)
            rows.append((role,str(path),len(raw),hashlib.sha256(raw).hexdigest(),mode))
        self.files=tuple(sorted(rows,key=lambda row:row[1]))
        self.roots=tuple(str(self.install/p) for p in ('stdlib','stdlib/lib-dynload','deps','src'))
        self.owner_calls=[];self.startup_calls=[];self.startup_hook=None
        current=self.current
        def owner():self.owner_calls.append('current');return current()
        self.frame=api._StartupContext(
            bootstrap_id='source-fixture-epoch-one',
            executable=str(self.install/'bin/python'),entrypoint=str(self.install/'entry.py'),
            python_version=tuple(sys.version_info[:3]),import_roots=self.roots,
            cache_prefix=str(self.cache),files=self.files,
            intrinsics=('os','sys'),path_hooks=tuple(sys.path_hooks),
            owner_binding=(hashlib.sha256(__import__('aggregate_fixture').encode(self.policy)).hexdigest(),
                           self.authority.generation,self.authority.fence),
            current=owner,originals=self.transport)
        self.observation=SimpleNamespace(
            executable=self.frame.executable,entrypoint=self.frame.entrypoint,
            python_version=self.frame.python_version,flags=(1,1,1,1,True,0,True),
            import_roots=self.roots,cache_prefix=str(self.cache),environment=(),
            meta_path=(machinery.BuiltinImporter,machinery.FrozenImporter,machinery.PathFinder),
            path_hooks=tuple(sys.path_hooks),importers=(),
            modules=(('__main__',self.frame.entrypoint,type(None),()),
                     ('sys','built-in',machinery.BuiltinImporter,()),
                     ('os','frozen',machinery.FrozenImporter,()),
                     ('cortex_core',None,machinery.NamespaceLoader,(str(self.install/'src/cortex_core'),)),
                     ('cortex_core.release.member_admission',str(self.install/'src/cortex_core/release/member_admission.py'),machinery.SourceFileLoader,()),
                     ('cryptography',str(self.install/'deps/cryptography/__init__.py'),machinery.SourceFileLoader,(str(self.install/'deps/cryptography'),))))
        return api

    def provider(self):
        self.startup_calls.append('bootstrap')
        return self.startup_hook(self.frame) if self.startup_hook else self.frame

    def composition(self):
        api=self.boundary();stack=ExitStack()
        stack.enter_context(patch.object(api,'_installed_startup',self.provider))
        stack.enter_context(patch.object(api,'_startup_observation',lambda:self.observation))
        return stack

    def check(self):
        api=self.boundary()
        with patch.object(api,'_startup_observation',lambda:self.observation):
            return api._admit_startup(self.frame)

    def replace_observation(self,**fields):
        self.observation=SimpleNamespace(**(vars(self.observation)|fields))

    def add_original(self,relative,raw=b'# inert unadmitted\n',role='source',mode=0o644):
        path=self.install/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw);path.chmod(mode)
        row=(role,str(path),len(raw),hashlib.sha256(raw).hexdigest(),mode)
        return row


def isolated_original_control(fixture, *, fault=None):
    """Independent source fixture, real isolated observer + crypto/member DATA.

    Parent pins original files BEFORE the child starts. Actual target install,
    native dependency completeness/protection and independent bootstrap are NOT
    qualified. The fixed owner function is supplied only by this test launcher.
    No test support/private signer enters the child import closure.
    """
    import base64
    import json
    import _imp
    import subprocess
    import sysconfig
    from aggregate_fixture import encode
    api=fixture.boundary()
    temp=tempfile.TemporaryDirectory(prefix='actual-a-',dir=MAIN)
    fixture.addCleanup(temp.cleanup);home=Path(temp.name)
    cache=home/'empty-cache';cache.mkdir(mode=0o700)
    entry=home/'owner_entry.py';packet=home/'owner_source_data.json'
    w=Path(api.__file__).resolve().parents[4]
    # w is next/ (member file -> release/core/src/next).
    next_root=Path(api.__file__).resolve().parents[3]
    source_root=next_root/'src'
    stdlib=Path(sysconfig.get_path('stdlib'))
    dynload=stdlib/'lib-dynload'
    site=Path(sysconfig.get_path('purelib'))
    executable=Path(sys.executable).resolve()
    roots=tuple(str(x) for x in (stdlib,dynload,site,source_root))
    body=fixture.bodies['runtime.lock.json']
    data={'raw':base64.b64encode(fixture.raw).decode(),'signature':base64.b64encode(fixture.signature).decode(),
          'public_root':base64.b64encode(fixture.authority.root_material).decode(),
          'policy':fixture.policy,'now':fixture.authority.now,'generation':fixture.authority.generation,
          'fence':fixture.authority.fence,'placement':fixture.authority.placement,
          'current_state':fixture.authority.current_state,'builds':fixture.builds,
          'original_root':str(fixture.root),'aggregate':fixture.aggregate,'member':'runtime.lock.json',
          'roots':roots,'executable':str(executable),'entrypoint':str(entry),'cache':str(cache),
          'python_version':tuple(sys.version_info[:3]),
          'intrinsics':tuple(sorted(set(sys.builtin_module_names)|set(_imp._frozen_module_names())|{'collections.abc','os.path','importlib._bootstrap','importlib._bootstrap_external'})),
          'fault':fault,'custody_root':str(home/'held-originals')}
    entry.write_text(r'''import base64,json,sys,os
from pathlib import Path
# Test-only trusted-launcher DATA. No product/config/candidate selector uses it.
p=Path(__file__).with_name('owner_source_data.json')
d=json.loads(p.read_bytes());sys.path[:]=d['roots'];sys.path_importer_cache.clear()
forbidden=[]
def audit(event,args):
 if event.startswith(('socket.','subprocess.','os.exec','os.spawn','os.fork')) or event in {'os.system','os.remove','os.rename','os.mkdir','os.rmdir','os.chmod','os.link','os.symlink'}:
  forbidden.append(event);raise AssertionError('source effect attempted: '+event)
 if event=='open' and len(args)>2:
  mode=args[1];flags=args[2]
  if (isinstance(mode,str) and any(x in mode for x in 'wax+')) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)):
   forbidden.append('open-write');raise AssertionError('source write attempted')
sys.addaudithook(audit)
from cortex_core.release import member_admission as a
from cortex_core.release.trust import _OwnerContext
from cortex_core.release.contracts import freeze
from cortex_core.release.codec import canonical_bytes,sha256
from cortex_core.release.custody import RootReader
from cortex_core.runtime import signed_member as port
from cortex_core.release import inspector,trust
startup_failures=[]
real_require=trust._require
def source_require(value,reason):
 if not value:
  import traceback
  caller=traceback.extract_stack(limit=2)[0]
  startup_failures.append({'reason':reason,'line':caller.lineno,'file':caller.filename})
 return real_require(value,reason)
trust._require=source_require # TEST recorder; real refusal body always executes.
measurement_errors=[]
real_measure=inspector._read_member
def measured(root_path,row):
 try:
  # SOURCE SIMULATION of the independent bootstrap's protected file custody.
  # Original bytes are held by the parent BEFORE launch. Do not assert that the
  # shared test-tool interpreter's real ancestors are a qualified installation.
  copied=dict(row,path=str(Path(d['custody_root'])/row['path'])[1:])
  return real_measure(Path('/'),copied)
 except Exception as e:
  measurement_errors.append({'path':row['path'],'reason':str(e)})
  raise
inspector._read_member=measured # TEST-only recorder retains the real body.
raw=base64.b64decode(d['raw']);signature=base64.b64decode(d['signature']);root=base64.b64decode(d['public_root'])
counts={'bootstrap':0,'current':0,'transport':0}
def current():
 counts['current']+=1
 pr=canonical_bytes(d['policy'])
 return _OwnerContext(freeze(d['policy']),pr,sha256(pr),root,d['now'],d['generation'],d['fence'],d['placement'],freeze(d['current_state']))
def originals(aggregate):
 counts['transport']+=1
 return raw,signature,RootReader.for_aggregate(d['original_root'],8*1024**2),d['builds']
# Explicit owner fixture bindings for the exact pinned native providers.
# This trusted fixture bootstrap preloads only admitted dependencies, never test
# support, candidate code or a fallback verifier. Object identities are held
# before admission; file custody remains labelled SOURCE SIMULATION.
from cryptography.hazmat.bindings.openssl.binding import Binding
Binding()
import _cffi_backend
import asyncpg.pgproto.pgproto as pgproto
alias_owners={'_openssl':_cffi_backend.__file__,'_openssl.lib':_cffi_backend.__file__,
              'cython_runtime':pgproto.__file__,'_cython_3_2_1':pgproto.__file__}
aliases=tuple((name,sys.modules[name],alias_owners[name]) for name in sorted(alias_owners))
frame=a._StartupContext(module_aliases=aliases,bootstrap_id='isolated-source-fixture-one',executable=d['executable'],entrypoint=d['entrypoint'],python_version=tuple(d['python_version']),import_roots=tuple(d['roots']),cache_prefix=d['cache'],files=tuple(tuple(x) for x in d['files']),intrinsics=tuple(d['intrinsics']),path_hooks=tuple(sys.path_hooks),owner_binding=(sha256(canonical_bytes(d['policy'])),d['generation'],d['fence']),current=current,originals=originals)
def bootstrap():
 counts['bootstrap']+=1
 return frame
a._installed_startup=bootstrap # TEST-only replacement, not an exported binder.
if d['fault']=='origin':
 a.__spec__.origin=str(Path(d['entrypoint']).parent/'unadmitted.py')
try:
 m=port.request_member(d['aggregate'],d['member']);out=port.read_member(m,aggregate_id=d['aggregate'],member_name=d['member'])
 result={'status':'original-data','raw':base64.b64encode(out).decode(),'type':type(m).__name__,
         'effect_attribute':hasattr(m,'effect_authority'),'native_attribute':hasattr(m,'native_qualified')}
except port.Refusal as e:result={'status':'refused','reason':e.reason,'unit':e.unit}
result.update(counts=counts,startup_failures=startup_failures,measurement_errors=measurement_errors,forbidden=forbidden,flags=[sys.flags.isolated,sys.flags.ignore_environment,sys.flags.no_site,sys.flags.no_user_site,sys.flags.safe_path,sys.flags.optimize,sys.dont_write_bytecode],
 test_support_loaded=any(n=='r01u3_support' or n.startswith(('r01u3_support.','test_','unittest')) for n in sys.modules),
 actual_observation=True,file_custody='SOURCE_SIMULATION_PINNED_PRIVATE_COPIES',physical_install_qualified=False,bootstrap_provenance='SOURCE_FIXTURE_ONLY')
ob=a._startup_observation()
result['unadmitted_intrinsics']=[name for name,origin,kind,locations in ob.modules if origin in ('built-in','frozen') and name not in frame.intrinsics]
result['unadmitted_files']=[name for name,origin,kind,locations in ob.modules if isinstance(origin,str) and origin not in ('built-in','frozen') and origin not in {row[1] for row in frame.files}]
result['no_spec_modules']=[name for name,origin,kind,locations in ob.modules if origin is None and not locations]
result['modules']=[dict(name=name,origin=origin,loader=kind.__module__+'.'+kind.__qualname__,locations=locations) for name,origin,kind,locations in ob.modules]
result['originals']=frame.files
result['module_aliases']=[dict(name=name,object_id=id(value),held_identity=sys.modules.get(name) is value,owner=owner) for name,value,owner in frame.module_aliases]
result['provider']={'version':__import__('cryptography').__version__,'origin':__import__('cryptography').__file__}
result['runtime_roots']=dict(executable=d['executable'],entrypoint=d['entrypoint'],roots=d['roots'],cache=d['cache'])
result['file_table_sha256']=sha256(canonical_bytes(d['files']))
result['file_table_bytes']=sum(row[2] for row in frame.files)
result['ancestor_metadata']=[dict(path=str(path),dev=path.stat().st_dev,inode=path.stat().st_ino,mode=path.stat().st_mode,uid=path.stat().st_uid) for path in (Path(d['cache']),*Path(d['cache']).parents)]
print(json.dumps(result))
''')
    paths={}
    def add(path,role):
        if path.is_file() and not path.is_symlink():paths[str(path)]=role
    add(executable,'interpreter');add(entry,'source');add(packet,'data')
    # Test-only file discovery; production consumes only the owner's fixed table.
    for base,dirs,files in os.walk(stdlib):
        dirs[:]=[d for d in dirs if d not in {'site-packages','test','tests','__pycache__','idlelib','tkinter','ensurepip','venv'} and not d.startswith('config-')]
        for name in files:
            path=Path(base)/name
            if path.suffix in {'.py','.so','.dylib','.dll'}:add(path,'loader' if path.suffix!='.py' else 'stdlib')
    admitted=('jsonschema','jsonschema_specifications','attrs','attr','referencing','rpds','cryptography','cffi','pycparser','typing_extensions','_cffi_backend','asyncpg')
    for child in site.iterdir():
        if not any(child.name==name or child.name.startswith(name+'-') or child.name.startswith(name+'.') for name in admitted):continue
        if child.is_file():add(child,'loader' if child.suffix=='.so' else 'source');continue
        for base,dirs,files in os.walk(child):
            dirs[:]=[d for d in dirs if d not in {'test','tests','__pycache__'}]
            for name in files:
                path=Path(base)/name
                add(path,'crypto' if child.name=='cryptography' else ('loader' if path.suffix in {'.so','.dylib','.dll'} else 'data' if path.suffix!='.py' else 'source'))
    for path in source_root.rglob('*.py'):add(path,'source')
    for path in (next_root/'contracts').rglob('*.json'):add(path,'data')
    # The packet transports TEST owner DATA; it is not an admitted code subject
    # and cannot circularly hash its own file table. Policy/originals are pinned
    # independently in B and the fixed launcher fixture; no production grant.
    paths.pop(str(packet),None)
    rows=[]
    for filename,role in sorted(paths.items()):
        path=Path(filename);raw_file=path.read_bytes()
        mode=stat.S_IMODE(path.stat().st_mode)&~0o022
        held=home/'held-originals'/filename[1:]
        held.parent.mkdir(mode=0o700,parents=True,exist_ok=True);held.write_bytes(raw_file);held.chmod(mode)
        rows.append((role,filename,len(raw_file),hashlib.sha256(raw_file).hexdigest(),mode))
    data['files']=rows;packet.write_text(json.dumps(data))
    run=subprocess.run([str(executable),'-I','-S','-B','-X','pycache_prefix='+str(cache),str(entry)],env={},capture_output=True,text=True,timeout=180)
    fixture.assertEqual(run.returncode,0,run.stdout+run.stderr)
    result=json.loads(run.stdout);result['expected_original']=base64.b64encode(body).decode();result['independent_file_count']=len(rows)
    return result
