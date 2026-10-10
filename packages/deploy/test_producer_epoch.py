"""H-D449 epoch contract: observed controller inputs, never an image qualification."""
import hashlib, importlib.util, json, os, stat, subprocess, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
SPEC=importlib.util.spec_from_file_location('epoch_producer',Path(__file__).with_name('build-manual-linux.py'))
p=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(p)
EPOCH=1791586380
class EpochTests(unittest.TestCase):
 def test_every_role_has_one_clock_and_epoch_build_argument(self):
  plan=p.make_plan(Path('/source'),'a'*40)
  for row in plan['images']:
   a=row['argv'];self.assertIn('--timestamp',a);self.assertEqual(a[a.index('--timestamp')+1],str(EPOCH));self.assertNotIn('SOURCE_DATE_EPOCH='+str(EPOCH),a)
   self.assertNotIn('--source-date-epoch',a);self.assertNotIn('--rewrite-timestamp',a)
 def test_real_checkout_mtimes_before_first_build_and_external_link_unchanged(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);source=r/'source';source.mkdir();outside=r/'outside';outside.write_text('do not touch');os.utime(outside,(42,42));before=outside.stat().st_mtime_ns
   for context,recipe in p.ROLES.values():
    f=source/context/recipe;f.parent.mkdir(parents=True,exist_ok=True);f.write_text('FROM scratch\n')
   nested=source/'packages/deploy/tls/input';nested.write_text('exact bytes');nested.chmod(0o755);(source/'packages/deploy/tls/link').symlink_to(outside)
   subprocess.run(['git','init','-q',str(source)],check=True);subprocess.run(['git','-C',str(source),'add','.'],check=True)
   subprocess.run(['git','-C',str(source),'-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture'],check=True)
   sha=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip();admission=r/'admission.json';admission.write_text(json.dumps(dict(source_revision=sha,source_accepted=True,pipeline_accepted=True,decision_id='fixture-only')))
   tool=r/'podman';tool.write_text('#!/bin/sh\nexit 71\n');tool.chmod(0o755);seen=[];original=subprocess.run
   def intercept(args,**kwargs):
    if args[0]==str(tool):
     seen.append(args);self.assertNotIn('SOURCE_DATE_EPOCH',kwargs['env'])
     paths=[source,nested,nested.parent,source/'packages/deploy/tls/link']
     for path in paths:self.assertEqual(path.lstat().st_mtime_ns,EPOCH*10**9,str(path))
     self.assertEqual(outside.stat().st_mtime_ns,before);self.assertEqual(nested.read_text(),'exact bytes');self.assertEqual(stat.S_IMODE(nested.stat().st_mode),0o755)
     raise subprocess.CalledProcessError(71,args)
    return original(args,**kwargs)
   with patch('sys.argv',['producer','--source',str(source),'--source-sha',sha,'--output',str(r/'output'),'--execute','--admission',str(admission),'--podman',str(tool)]),patch.object(p,'native_check'),patch.object(p.subprocess,'run',side_effect=intercept):
    with self.assertRaises(subprocess.CalledProcessError):p.main()
   self.assertEqual(len(seen),1)
if __name__=='__main__':unittest.main()
