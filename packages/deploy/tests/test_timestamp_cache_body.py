"""Three retained source/raw-body gold pairs through the ACTUAL API compiler RUN."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import sys
import tempfile
import unittest

S=importlib.util.spec_from_file_location('order',Path(__file__).with_name('test_timestamp_cache_order.py'))
order=importlib.util.module_from_spec(S);S.loader.exec_module(order)
FIX=Path(__file__).parent/'fixtures/timestamp-body'

class Body(unittest.TestCase):
 def test_three_real_reference_bodies_exact_after_actual_compiler_RUN(self):
  pairs=json.loads((FIX/'pairs.json').read_text())
  run=next(x for x in order.instructions((order.ROOT/'packages/api/Dockerfile').read_text()) if x.startswith('RUN ') and 'compileall.compile_dir' in x)
  argv,environment=order.compiler_command(run);self.assertEqual(argv[:2],['python','-c'])
  for row in pairs:
   with self.subTest(cache=row['cache']),tempfile.TemporaryDirectory() as temp:
    root=Path(temp);rel=Path(row['source_filename']).relative_to('/usr/local/lib/python3.14/site-packages');source=root/rel;source.parent.mkdir(parents=True,exist_ok=True);source.write_bytes((FIX/row['source_file']).read_bytes());os.utime(source,(order.EPOCH,order.EPOCH))
    cache=Path(importlib.util.cache_from_source(str(source)));cache.parent.mkdir(exist_ok=True);gold=(FIX/row['reference_body']).read_bytes();cache.write_bytes(importlib.util.MAGIC_NUMBER+struct.pack('<III',0,order.EPOCH,source.stat().st_size)+gold)
    # Declared path adapter: source stays physical; only compiler filename maps
    # the temporary fixture to the real retained final runtime filename.
    prefix='import builtins,sysconfig; _actual_compile=builtins.compile; builtins.compile=lambda data,filename,*a,**kw:_actual_compile(data, '+repr(row['source_filename'])+' if filename=='+repr(str(source))+' else filename,*a,**kw); sysconfig.get_path=lambda name:'+repr(str(root))+'; '
    subprocess.run([sys.executable,'-c',prefix+argv[2]],check=True,env=environment)
    self.assertEqual(cache.read_bytes()[16:],gold,'actual compiler changed retained raw body')

if __name__=='__main__':unittest.main()
