"""Known inert fixture relations through unchanged public API/CLI; no native."""
from pathlib import Path
import hashlib,json,socket,subprocess,sys,sysconfig,tempfile,unittest
WT=Path('/Volumes/WD-B-4TB/DevVault/helix/worktrees/mike-r01-unit2-implementation-review-20261012');OUT=Path(__file__).resolve().parent
SCRATCH=OUT/'probe-scratch';SCRATCH.mkdir(exist_ok=True);tempfile.tempdir=str(SCRATCH)
sys.path[:0]=[str(WT/'next/src'),str(WT/'next/tests'),str(WT/'next/tests/release'),str(OUT/'runtime'),sysconfig.get_paths()['purelib']]
def denied(*a,**k):raise AssertionError('REVIEW_NETWORK_NATIVE_DENIED')
socket.socket.connect=denied;socket.socket.connect_ex=denied;socket.create_connection=denied;socket.getaddrinfo=denied;subprocess.Popen=denied;subprocess.run=denied;subprocess.check_output=denied
import case_factory
import native_matrix_fixture as matrix
from cortex_core.release import vfs
from cortex_core.cli import release
actual=release.main;cli=[]
def observed(argv):
 result=actual(argv);p=Path(argv[argv.index('--decision')+1]);raw=p.read_bytes();cli.append({'exit':result,'decision':json.loads(raw),'sha256':hashlib.sha256(raw).hexdigest(),'path':str(p)});return result
release.main=observed
rows=[];saved={}
for label in ['baseline','whiteout_cmd_input','cmd_unreadable','cmd_parent_unsearchable']:
 case=case_factory.case(label);saved[label]=case;value,inputs,receipt,outputs,evidence,acceptance=case
 descriptor=json.loads(inputs[value['descriptor']['path']]);profile=descriptor['release_policy']
 base=OUT/'probe-inputs'/label;base.mkdir(parents=True,exist_ok=True)
 for name,obj in [('input',value),('descriptor',descriptor),('receipt',receipt),('acceptance',acceptance)]: (base/(name+'.json')).write_bytes(matrix.canonical(obj))
 ledger={name:{p:{'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()} for p,raw in members.items()} for name,members in [('inputs',inputs),('outputs',outputs),('evidence',evidence)]}
 (base/'BYTE_INDEX.json').write_text(json.dumps(ledger,indent=2)+'\n')
 relation={'whiteouts_policy':profile['layer_policy']['whiteouts'],'selected_uid':profile['runtime']['uid'],'selected_gid':profile['runtime']['gid'],'program':profile['runtime']['program'],'working_dir':profile['runtime']['working_dir'],'config_record':next(r for r in profile['layer_policy']['generated']['one'] if r['path']=='app/config.json'),'whiteout_present':any(r['path']=='app/.wh.config.json' for r in profile['layer_policy']['generated']['two'])}
 if label=='cmd_unreadable':
  relation['selected_user_can_read_config']=vfs.access(relation['config_record'],profile['runtime']['uid'],profile['runtime']['gid'],4);assert not relation['selected_user_can_read_config']
 if label=='cmd_parent_unsearchable':
  app=next(r for r in profile['layer_policy']['generated']['one'] if r['path']=='app');relation['app_record']=app;relation['selected_user_can_traverse_cmd_parent']=vfs.access(app,profile['runtime']['uid'],profile['runtime']['gid'],1);assert not relation['selected_user_can_traverse_cmd_parent']
 row={'label':label,'relation':relation,'channels':{}}
 for channel,function in [('api',matrix.api),('cli',matrix.cli)]:
  try:decision=function(case);row['channels'][channel]={'outcome':'accepted','decision':decision}
  except ValueError as error:row['channels'][channel]={'outcome':'refused','reason':str(error)}
 rows.append(row)
class DesiredRelations(unittest.TestCase):
 def baseline(self,channel):
  result=getattr(matrix,channel)(saved['baseline']);self.assertEqual(result['decision'],'verified');self.assertFalse(result['native_qualified'])
 def refuse(self,label,channel):
  with self.assertRaises(ValueError,msg=label+' must refuse despite syntactically accepted profile'):
   getattr(matrix,channel)(saved[label])
for channel in ['api','cli']:
 def positive(self,channel=channel):self.baseline(channel)
 setattr(DesiredRelations,'test_coherent_positive_'+channel,positive)
 for label in ['whiteout_cmd_input','cmd_unreadable','cmd_parent_unsearchable']:
  def negative(self,label=label,channel=channel):self.refuse(label,channel)
  setattr(DesiredRelations,'test_must_refuse_'+label+'_'+channel,negative)
with (OUT/'ROOT_FUNCTIONAL_PROBES.stderr').open('w') as stream: result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DesiredRelations))
report={'head':'6981e905aed0cf5b71a82870970e3b789737f7fc','tests':result.testsRun,'PASS':result.testsRun-len(result.failures)-len(result.errors),'BODY_failures':[{'id':t.id(),'traceback':tb} for t,tb in result.failures],'errors':[{'id':t.id(),'traceback':tb} for t,tb in result.errors],'skips':result.skipped,'observed':rows,'actual_CLI_decisions':cli,'fixture_scope':'Public source/receipt/API/CLI with coherently amended known inert public fixture producer; no native/container/filesystem extraction or artifact-code execution','product_source_sha256':hashlib.sha256(Path(vfs.__file__).read_bytes()).hexdigest(),'owned_scratch_remaining':[str(p) for p in SCRATCH.iterdir()]}
(OUT/'ROOT_FUNCTIONAL_PROBES.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:report[k] for k in ['tests','PASS','errors','skips','owned_scratch_remaining']}));print('EXPECTED_BODY_RED',len(result.failures));print('OUTCOMES',[(r['label'],{k:v['outcome'] for k,v in r['channels'].items()}) for r in rows])
assert result.testsRun==8 and len(result.failures)==6 and not result.errors and not result.skipped and not list(SCRATCH.iterdir())
