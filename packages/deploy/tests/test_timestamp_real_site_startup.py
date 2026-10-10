"""Actual system-site startup (.pth before -c), no synthetic startup shortcut."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import pip

S=importlib.util.spec_from_file_location('order',Path(__file__).with_name('test_timestamp_cache_order.py'))
order=importlib.util.module_from_spec(S);S.loader.exec_module(order)
FIX=Path(__file__).parent/'fixtures/site-startup'

class RealSiteStartup(unittest.TestCase):
 def test_real_embed_pth_startup_reference_body_and_actual_header(self):
  row=json.loads((FIX/'case.json').read_text());gold=(FIX/'distutils.reference-body').read_bytes()
  run=next(x for x in order.instructions((order.ROOT/'packages/containers/embed-worker/Dockerfile').read_text()) if x.startswith('RUN ') and 'compileall.compile_dir' in x)
  argv,environment=order.compiler_command(run);self.assertEqual(argv[:2],['python','-c'])
  # Clean parent setting; apply ONLY the actual recognized RUN prefix.
  prefix_value='stdlib' if run.startswith('RUN SETUPTOOLS_USE_DISTUTILS=stdlib ') else None
  environment.pop('SETUPTOOLS_USE_DISTUTILS',None)
  if prefix_value:environment['SETUPTOOLS_USE_DISTUTILS']=prefix_value
  environment['PYTHONDONTWRITEBYTECODE']='1';environment.pop('PYTHONPATH',None)
  with tempfile.TemporaryDirectory() as temp:
   venv=Path(temp)/'venv';subprocess.run([sys.executable,'-m','venv','--without-pip',str(venv)],check=True,capture_output=True)
   python=venv/'bin/python';root=venv/'lib/python3.14/site-packages';self.assertTrue(root.is_dir())
   shutil.copytree(Path(pip.__file__).parent,root/'pip',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
   source=root/'_distutils_hack/__init__.py';source.parent.mkdir();source.write_bytes((FIX/'distutils.source').read_bytes());os.utime(source,(order.EPOCH,order.EPOCH))
   cache=Path(importlib.util.cache_from_source(str(source)));cache.parent.mkdir();cache.write_bytes(importlib.util.MAGIC_NUMBER+struct.pack('<III',0,order.EPOCH+123,source.stat().st_size)+gold)
   shutil.copyfile(FIX/'distutils-precedence.pth',root/'distutils-precedence.pth')
   # Python's own site initialization runs this actual hook BEFORE our -c code.
   program='import sys,builtins,json; loaded_before="_distutils_hack" in sys.modules; original=builtins.compile; builtins.compile=lambda data,filename,*a,**kw:original(data,'+repr(row['source_filename'])+' if filename=='+repr(str(source))+' else filename,*a,**kw); '+argv[2]+'; print(json.dumps({"startup_loaded_before_compiler":loaded_before,"real_site_prefix":sys.prefix}))'
   q=subprocess.run([str(python),'-c',program],capture_output=True,text=True,env=environment);self.assertEqual(q.returncode,0,q.stderr)
   observed=json.loads(q.stdout);self.assertEqual(observed['real_site_prefix'],str(venv));self.assertEqual(observed['startup_loaded_before_compiler'],prefix_value is None)
   if prefix_value is None:
    import hashlib
    self.assertEqual(hashlib.sha256(cache.read_bytes()[16:]).hexdigest(),row['attempt10_bad_body_sha256'],'must reproduce actual native bad body before qualifying RED')
   self.assertEqual(cache.read_bytes()[16:],gold,'actual system-site startup changed retained raw body')
   self.assertEqual(order.check(root/'_distutils_hack'),1)

if __name__=='__main__':unittest.main()
