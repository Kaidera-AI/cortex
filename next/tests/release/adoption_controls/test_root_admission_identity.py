"""Desired exact product-API refusals; inert metadata, no command executes."""
from pathlib import Path
import hashlib,json,socket,subprocess,sys,unittest
wt=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path('/Volumes/WD-B-4TB/DevVault/helix/worktrees/mike-r01-unit1-adoption-review-20261012');r=Path(__file__).resolve().parent;out=Path(sys.argv[2]).resolve() if len(sys.argv)>2 else r;out.mkdir(parents=True,exist_ok=True)
def denied(*args,**kwargs):raise AssertionError('REVIEW_NATIVE_NETWORK_DENIED')
socket.socket.connect=denied;socket.socket.connect_ex=denied;socket.create_connection=denied;socket.getaddrinfo=denied
subprocess.Popen=denied;subprocess.run=denied;subprocess.check_output=denied
sys.path[:0]=[str(wt/'next/src'),str(wt/'next/tests'),str(wt/'next/tests/release')]
from cortex_core.release import inputs
from fixture import build_input,canonical,digest
import test_receipts
data=json.loads((r/'consumer/adoption-identity-result.json').read_text());cases={x['case']:x for x in data['metadata_cases']};argv={x['case']:x for x in data['argv_cases']};observed=[]
def admit(value,files):
 p={'input_sha256':digest(value),'policy_sha256':'e'*64,'qualification_scope':'fixture'}
 try:
  v=inputs.admit_build_input(inputs.parse_build_input(canonical(value)),files.__getitem__,p);return 'admitted',v
 except ValueError as e:return 'refused',str(e)
def metadata(case):
 row=cases[case];status,result=admit(row['input'],{k:v.encode() for k,v in row['files'].items()});observed.append({'case':case,'full_API_result':status,'refusal':result if status=='refused' else None,'native_qualified':getattr(result,'native_qualified',None)});return status
def recipe(case):
 value,files=build_input();value['recipe']['argv_template']=argv[case]['argv'];status,result=admit(value,files);observed.append({'case':case,'full_API_result':status,'refusal':result if status=='refused' else None,'argv_was_executed':False});return status
class DesiredAdmission(unittest.TestCase):
 def test_supported_tuple_literal_positive_and_direct_shell_refusal(self):
  self.assertEqual(metadata('supported_target_and_dependency'),'admitted');self.assertEqual(recipe('literal_positive'),'admitted');self.assertEqual(recipe('direct_shell_negative'),'refused')
 def test_dependency_cannot_overwrite_target_requirement(self):self.assertEqual(metadata('dependency_overwrites_target'),'refused')
 def test_duplicate_dependency_versions_cannot_use_last_wins(self):self.assertEqual(metadata('duplicate_dependency_last_wins'),'refused')
 def test_env_wrapper_must_not_turn_recipe_into_shell_zero_argument(self):self.assertEqual(recipe('env_wrapped_shell'),'refused')
if __name__=='__main__':
 result=unittest.TextTestRunner(verbosity=2,resultclass=test_receipts.AssertionResult).run(unittest.defaultTestLoader.loadTestsFromTestCase(DesiredAdmission))
 value={'source_reference_head':'e56acda897f783b4f7182088bea88e4494d6e4e1','preparation_head':'a0ccb8ff196ee2dfed34c4439de604ac9edf4bee','scope':'FULL_PRODUCT_INPUT_API fixture; no builder/native/effect/command invocation','observed':observed,'tests_run':result.testsRun,'failures':result.assertions,'errors':[{'id':t.id(),'traceback':tb} for t,tb in result.errors],'skipped':result.skipped,'source_sha256':hashlib.sha256(Path(inputs.__file__).read_bytes()).hexdigest(),'exit':0 if result.wasSuccessful() else 1};(out/'ROOT_ADMISSION_DESIRED_RESULT.json').write_text(json.dumps(value,indent=2)+'\n');assert result.testsRun==4;print(json.dumps({'tests':result.testsRun,'BODY':len(result.assertions),'errors':len(result.errors),'skips':len(result.skipped)}));raise SystemExit(value['exit'])
