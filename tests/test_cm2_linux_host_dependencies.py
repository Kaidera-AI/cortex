"""Actual CLI import audit and declared native runtime license/lock closure."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

from test_cm2_linux_host_stage import test_complete_host_stage_records_cm2_helper_and_exact_bootstrap_license_bytes as stage_fixture

ROOT=Path(__file__).resolve().parents[1]
RUNTIME={'annotated-types','pydantic','pydantic-core','typing-extensions','typing-inspection'}

def declarations():
    text=(ROOT/'scripts/release/requirements-linux-host-build.txt').read_text()
    return {n:(v,d) for n,v,d in re.findall(r'^([a-z0-9-]+)==([^ ]+) --hash=sha256:([0-9a-f]{64})$',text,re.M)}

def test_native_host_runtime_import_closure_has_exact_repo_locked_linux_wheel_hashes():
    actual=declarations();packages={p['name']:p for p in tomllib.loads((ROOT/'uv.lock').read_text())['package']}
    assert RUNTIME <= actual.keys(), 'native CLI Pydantic import closure missing from host build lock'
    for name in RUNTIME:
        package=packages[name]
        wheels=[w for w in package['wheels'] if 'py3-none-any' in w['url'] or ('cp312-cp312-manylinux' in w['url'] and 'x86_64' in w['url'])]
        assert len(wheels)==1 and actual[name]==(package['version'],wheels[0]['hash'].removeprefix('sha256:'))
        assert {d['name'] for d in package.get('dependencies',[])} <= actual.keys()

def test_actual_release_entry_help_imports_only_declared_builder_packages(tmp_path):
    # Loaded modules are real fixture dependencies; the child rejects each
    # third-party import absent from the actual clean native-builder lock.
    script='''import importlib.abc,runpy,sys,sysconfig
allowed=set(sys.argv[1].split(','))|sys.stdlib_module_names|{'cortex_v2',sysconfig._get_sysconfigdata_name()}
class Audit(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split('.')[0] not in allowed:raise ModuleNotFoundError('UNDECLARED:'+fullname.split('.')[0])
sys.meta_path.insert(0,Audit())
sys.argv=[sys.argv[2],'--help'];runpy.run_path(sys.argv[0],run_name='__main__')
'''
    names={name.replace('-','_') for name in declarations()}
    environment=dict(os.environ,PYTHONPATH=str(ROOT/'src'),PYTHONPYCACHEPREFIX=str(tmp_path/'pycache'))
    result=subprocess.run([sys.executable,'-c',script,','.join(sorted(names)),str(ROOT/'scripts/release/cortex_native.py')],
                          env=environment,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
    assert result.returncode==0, result.stderr.splitlines()[-1:]
    assert 'Cortex human identity commands' in result.stdout and result.stderr==''

def test_complete_native_host_receipt_separately_records_runtime_package_notices(tmp_path,monkeypatch):
    stage_fixture(tmp_path,monkeypatch)
    out=tmp_path/'out';receipt=json.loads((out/'host-inventory.json').read_text())
    assert {p['name'] for p in receipt.get('runtime_packages',[])}==RUNTIME
    assert not RUNTIME & {p['name'] for p in receipt['builder_packages']}
    document=json.loads((out/'host.spdx.json').read_text())
    assert RUNTIME <= {p['name'] for p in document['packages']}
    for name in RUNTIME:assert list((out/'licenses').glob(name+'-*-LICENSE'))
