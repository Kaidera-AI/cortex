"""Desired product OCI/bytecode controls. Compiled code is NEVER executed."""
from pathlib import Path
import hashlib,importlib.util,io,json,marshal,socket,subprocess,sys,tarfile,unittest
wt=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path('/Volumes/WD-B-4TB/DevVault/helix/worktrees/mike-r01-unit1-adoption-review-20261012');r=Path(__file__).resolve().parent;out=Path(sys.argv[2]).resolve() if len(sys.argv)>2 else r;out.mkdir(parents=True,exist_ok=True)
def denied(*args,**kwargs):raise AssertionError('REVIEW_NATIVE_NETWORK_DENIED')
socket.socket.connect=denied;socket.socket.connect_ex=denied;socket.create_connection=denied;socket.getaddrinfo=denied
subprocess.Popen=denied;subprocess.run=denied;subprocess.check_output=denied
sys.path[:0]=[str(wt/'next/src'),str(wt/'next/tests'),str(wt/'next/tests/release')]
from cortex_core.release import artifacts as A
from cortex_core.release.contracts import ReceiptRefusal
from fixture import build_input,canonical
import test_receipts
value,_=build_input();value['builder_requirements']['toolchain']['version']=sys.version.split()[0]
policy={'oci_config':{},'history':[],'layer_policy':{'allow_replacements':False,'allow_directories':True,'symlinks':{}}}
sha=lambda b:hashlib.sha256(b).hexdigest();observed=[]
def tar(rows):
 f=io.BytesIO()
 with tarfile.open(fileobj=f,mode='w',format=tarfile.USTAR_FORMAT) as ar:
  for name,kind,body in rows:
   m=tarfile.TarInfo(name);m.uid=m.gid=1001;m.mtime=0;m.mode=493 if kind=='directory' else 420;m.type=tarfile.DIRTYPE if kind=='directory' else tarfile.REGTYPE;m.size=0 if kind=='directory' else len(body);ar.addfile(m,None if kind=='directory' else io.BytesIO(body))
 return f.getvalue()
def image(layer):
 config=canonical({'architecture':'amd64','os':'linux','config':{},'rootfs':{'type':'layers','diff_ids':['sha256:'+sha(layer)]}})
 def desc(media,b):return {'mediaType':media,'digest':'sha256:'+sha(b),'size':len(b)}
 manifest=canonical({'schemaVersion':2,'mediaType':A.MANIFEST,'config':desc(A.CONFIG,config),'layers':[desc(A.LAYER,layer)]})
 index=canonical({'schemaVersion':2,'mediaType':A.INDEX,'manifests':[dict(desc(A.MANIFEST,manifest),platform={'os':'linux','architecture':'amd64'})]})
 bodies={sha(layer):layer,sha(config):config,sha(manifest):manifest};archive=tar([(p,'regular',b) for p,b in {'oci-layout':canonical({'imageLayoutVersion':'1.0.0'}),'index.json':index,**{'blobs/sha256/'+k:v for k,v in bodies.items()}}.items()])
 raw={'archive':archive,'index':index,'manifest':manifest,'config':config,'blob.'+sha(layer):layer,'sbom':b'{}\n','provenance':b'{}\n'};media={'archive':'application/x-tar','index':A.INDEX,'manifest':A.MANIFEST,'config':A.CONFIG,'blob.'+sha(layer):'application/octet-stream','sbom':'application/json','provenance':'application/json'}
 records=[{'role':role,'path':role,'media_type':media[role],'bytes':len(body),'sha256':sha(body)} for role,body in raw.items()];oci={'index':sha(index),'manifest':sha(manifest),'config':sha(config),'blobs':sorted(bodies),'platform':'linux/amd64'}
 return archive,records,raw,oci
def inspect(label,rows):
 try:value_result=A.inspect_oci(*image(tar(rows)),value,module_policy=policy);status='ACCEPTED';detail=value_result[1]
 except Exception as e:status=type(e).__name__;detail=str(e)
 observed.append({'case':label,'status':status,'detail':detail});return status
source=b'try:\n    x=1/0\nexcept ZeroDivisionError:\n    x=42\n';code=compile(source,'/app/module.py','exec',dont_inherit=True,optimize=0);header=importlib.util.MAGIC_NUMBER+(3).to_bytes(4,'little')+importlib.util.source_hash(source);pyc='app/__pycache__/module.cpython-'+str(sys.version_info.major)+str(sys.version_info.minor)+'.pyc'
def pyc_rows(body):return [('app/module.py','regular',source),(pyc,'regular',header+marshal.dumps(body))]
class DesiredArtifact(unittest.TestCase):
 def test_allowed_ordinary_directory_positive(self):self.assertEqual(inspect('ordinary-directory',[('app/assets','directory',b''),('app/module.py','regular',b'pass\n')]),'ACCEPTED')
 def test_allowed_directory_suffix_must_not_be_treated_as_pyc_file(self):self.assertEqual(inspect('directory-pyc-suffix',[('app/cache.pyc','directory',b''),('app/module.py','regular',b'pass\n')]),'ACCEPTED')
 def test_unchanged_compiler_pyc_positive_and_other_metadata_refusal(self):
  self.assertEqual(inspect('compiler-original',pyc_rows(code)),'ACCEPTED')
  self.assertEqual(inspect('compiler-firstlineno-mismatch',pyc_rows(code.replace(co_firstlineno=code.co_firstlineno+100))),'ReceiptRefusal')
  self.assertEqual(inspect('compiler-exceptiontable-mismatch',pyc_rows(code.replace(co_exceptiontable=b''))),'ReceiptRefusal')
 def test_changed_compiler_stack_metadata_must_refuse(self):
  changed=code.replace(co_stacksize=0);observed.append({'case':'stacksize-body-difference','original_stacksize':code.co_stacksize,'changed_stacksize':changed.co_stacksize,'code_equality':changed==code,'original_marshaled_sha256':sha(marshal.dumps(code)),'changed_marshaled_sha256':sha(marshal.dumps(changed)),'executed':False})
  self.assertEqual(inspect('compiler-stacksize-zero',pyc_rows(changed)),'ReceiptRefusal')
 def test_changed_nested_compiler_stack_metadata_must_refuse(self):
  nested_source=b'def f(x):\n    return (x+1)*2\n';original=compile(nested_source,'/app/module.py','exec',dont_inherit=True,optimize=0);child=next(c for c in original.co_consts if hasattr(c,'co_stacksize'));changed_child=child.replace(co_stacksize=child.co_stacksize+1);changed=original.replace(co_consts=tuple(changed_child if c is child else c for c in original.co_consts));nested_header=importlib.util.MAGIC_NUMBER+(3).to_bytes(4,'little')+importlib.util.source_hash(nested_source)
  observed.append({'case':'nested-stacksize-body-difference','original_child_stacksize':child.co_stacksize,'changed_child_stacksize':changed_child.co_stacksize,'parent_code_equality':changed==original,'executed':False})
  self.assertEqual(inspect('nested-compiler-original',[('app/module.py','regular',nested_source),(pyc,'regular',nested_header+marshal.dumps(original))]),'ACCEPTED')
  self.assertEqual(inspect('nested-compiler-stacksize-mismatch',[('app/module.py','regular',nested_source),(pyc,'regular',nested_header+marshal.dumps(changed))]),'ReceiptRefusal')
if __name__=='__main__':
 result=unittest.TextTestRunner(verbosity=2,resultclass=test_receipts.AssertionResult).run(unittest.defaultTestLoader.loadTestsFromTestCase(DesiredArtifact))
 d={'source_reference_head':'e56acda897f783b4f7182088bea88e4494d6e4e1','scope':'ACTUAL_PRODUCT_OCI_INSPECTOR_ONLY; not wholebuildreceipt/nativeproof','malformed_or_compiled_code_executed':False,'artifact_source_sha256':sha(Path(A.__file__).read_bytes()),'tests_run':result.testsRun,'failures':result.assertions,'errors':[{'id':t.id(),'traceback':tb} for t,tb in result.errors],'skipped':result.skipped,'observed':observed,'suite_exit':0 if result.wasSuccessful() else 1};(out/'ROOT_ARTIFACT_DESIRED_RESULT.json').write_text(json.dumps(d,indent=2)+'\n');assert result.testsRun==5;print(json.dumps({'tests':result.testsRun,'BODY':len(result.assertions),'errors':len(result.errors),'skips':len(result.skipped),'bytecode_executed':False}));raise SystemExit(d['suite_exit'])
