"""Frozen regression: build compilation cannot create caches for uncached base source."""
import importlib.util
import os
from pathlib import Path
import py_compile
import shlex
import struct
import subprocess
import sys
import tempfile
import unittest

S=importlib.util.spec_from_file_location('order',Path(__file__).with_name('test_timestamp_cache_order.py'))
order=importlib.util.module_from_spec(S);S.loader.exec_module(order)

class Scope(unittest.TestCase):
 def test_existing_inventory_only_and_every_header_actual_stat(self):
  for recipe in order.RECIPES:
   runs=[x for x in order.instructions((order.ROOT/recipe).read_text()) if x.startswith('RUN ') and 'compileall.compile_dir' in x]
   for stage,run in enumerate(runs):
    with self.subTest(recipe=recipe,stage=stage),tempfile.TemporaryDirectory() as temp:
     root=Path(temp)
     for name in ['installed_a','installed_b']:
      source=root/(name+'.py');source.write_text('answer = 7\n');os.utime(source,(order.EPOCH+123,order.EPOCH+123));py_compile.compile(str(source),doraise=True,invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP);os.utime(source,(order.EPOCH,order.EPOCH))
     uncached=root/'base_pip_uncached.py';uncached.write_text('base_pip = True\n');os.utime(uncached,(order.EPOCH,order.EPOCH))
     before={str(x.relative_to(root)) for x in root.rglob('*.pyc')}
     argv=shlex.split(run.removeprefix('RUN '));self.assertEqual(argv[:2],['python','-c'])
     body='import sysconfig;sysconfig.get_path=lambda name: '+repr(str(root))+'; '+argv[2]
     subprocess.run([sys.executable,'-c',body],check=True)
     after={str(x.relative_to(root)) for x in root.rglob('*.pyc')}
     self.assertEqual(after,before,'uncached base source acquired a cache')
     self.assertFalse(Path(importlib.util.cache_from_source(str(uncached))).exists())
     self.assertEqual(order.check(root),2)

if __name__=='__main__':unittest.main()
