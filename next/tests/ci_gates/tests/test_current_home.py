"""Current Day-2 source homes; no native or installed acceptance."""
import hashlib
import importlib.util
import json
import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[4]
PROFILE=ROOT/'.github/next-offline-suites.json'
V2='next/modules/cli-v2/tests/'
SCHEMA='next/src/cortex_core/schema/tests/'
RUNTIME='next/src/cortex_core/runtime/tests/'
class CurrentHome(unittest.TestCase):
    def manifest(self):return json.loads((ROOT/'.github/next-offline-suites.json').read_bytes())
    def test_versioned_transport_and_actual_health_are_loopback_only(self):
        value=self.manifest()
        for name in ('admission','composition','processes','projection','results'):
            self.assertIn(V2+'test_transport_'+name+'.py',value['loopback'])
        for name in ('test_namespace.py','test_health_join.py'):
            self.assertIn(V2+name,value['loopback'])
        self.assertFalse(any(p.startswith('packages/cli/tests/test_transport') for p in value['loopback']))
        self.assertFalse(set(value['loopback']) & set(value['run']))
    def test_latest_health_contract_has_explicit_loopback_home(self):
        self.assertIn(V2+'test_health_contract.py',self.manifest()['loopback'])
    def test_new_pipe_identity_has_exact_loopback_admission(self):
        value=self.manifest()
        self.assertIn(V2+'test_pipe_identity.py',value['loopback'])
        self.assertNotIn(V2+'test_pipe_identity.py',value['run'])
        spec=importlib.util.spec_from_file_location('current_home_offline_gate',
            ROOT/'.github/scripts/pytest_next_offline.py')
        gate=importlib.util.module_from_spec(spec)
        sys.modules[spec.name]=gate
        spec.loader.exec_module(gate)
        checked=gate.inventory(ROOT,value)
        self.assertTrue(checked['ok'],checked)
        # The new exact home grants no blanket helper/test-path admission.
        value['loopback'].append(V2+'test_unreviewed_pipe.py')
        refused=gate.inventory(ROOT,value)
        self.assertFalse(refused['ok'])
        self.assertIn("invalid explicit suite path: '"+V2+"test_unreviewed_pipe.py'",refused['invalid'])
    def test_ship_wrapper_has_exact_loopback_admission_without_adjacent_escape(self):
        path='next/release/ship/cli/tests/test_wrapper.py'
        value=self.manifest()
        self.assertIn(path,value['loopback'])
        self.assertNotIn(path,value['run'])
        self.assertNotIn(path,[row['path'] for row in value['excluded']])
        spec=importlib.util.spec_from_file_location('wrapper_home_offline_gate',
            ROOT/'.github/scripts/pytest_next_offline.py')
        gate=importlib.util.module_from_spec(spec)
        sys.modules[spec.name]=gate
        spec.loader.exec_module(gate)
        checked=gate.inventory(ROOT,value)
        self.assertTrue(checked['ok'],checked)
        self.assertFalse(set(checked['run']) & set(checked['loopback']))
        self.assertEqual(checked['loopback'].count(path),1)
        adjacent='next/release/ship/cli/tests/test_unreviewed_wrapper.py'
        value['loopback'].append(adjacent)
        refused=gate.inventory(ROOT,value)
        self.assertFalse(refused['ok'])
        self.assertIn('invalid explicit suite path: '+repr(adjacent),refused['invalid'])
    def test_seed_caller_has_explicit_loopback_home(self):
        self.assertIn(SCHEMA+'test_seed_operator.py',self.manifest()['loopback'])
        self.assertIn(SCHEMA+'test_seed_owner_contract.py',self.manifest()['run'])
    def test_current_runtime_bindings_and_lifecycle_are_admitted(self):
        for name in ('test_catalog_binding.py','test_lifecycle.py','test_services.py','test_status_binding.py'):
            self.assertIn(RUNTIME+name,self.manifest()['run'])
    def test_current_provisioner_and_this_control_are_admitted(self):
        for name in ('next/release/ship/install/tests/test_provision.py',
                     'next/tests/ci_gates/tests/test_current_home.py'):
            self.assertIn(name,self.manifest()['run'])
    def test_legacy_cli_and_regression_are_exact_unchanged_baseline(self):
        for name,digest in {
            'packages/cli/_cortex_api.sh':'d78ad0c87ffdf286c69b2485d3224186decc81b37608a4ae92fe40806d84f636',
            'packages/api/tests/test_mcp_live.py':'687377a57488802a50fa383176aa4bae1dd51ebc095b15d49e16236df21fb8f3',
        }.items():
            self.assertEqual(hashlib.sha256((ROOT/name).read_bytes()).hexdigest(),digest)

    def test_finite_job_selectors_refuse_empty_duplicate_and_adjacent_suites(self):
        spec=importlib.util.spec_from_file_location('job_selector_gate',ROOT/'.github/scripts/pytest_next_offline.py')
        gate=importlib.util.module_from_spec(spec);sys.modules[spec.name]=gate;spec.loader.exec_module(gate)
        select=getattr(gate,'job_partition',None)
        self.assertTrue(callable(select),'BODY RED: finite whole-job selector absent')
        suites=['next/tests/test_job_'+str(i)+'.py' for i in range(24)]
        parts=[select(suites,i) for i in range(8)]
        self.assertEqual(parts,[suites[i::8] for i in range(8)])
        self.assertEqual(sorted(p for part in parts for p in part),sorted(suites))
        for bad,index in (([],0),(suites,True),(suites,-1),(suites,8),(suites,'0'),
                          (suites+[suites[0]],0),(suites[:7],0),
                          (suites+['next/tests/../test_adjacent.py'],0)):
            with self.subTest(suites=bad,index=index),self.assertRaises(ValueError):select(bad,index)

    def test_real_unpartitioned_collection_equals_exact_job_node_union(self):
        spec=importlib.util.spec_from_file_location('job_coverage_gate',ROOT/'.github/scripts/pytest_next_offline.py')
        gate=importlib.util.module_from_spec(spec);sys.modules[spec.name]=gate;spec.loader.exec_module(gate)
        collect=getattr(gate,'collect_unpartitioned',None)
        cover=getattr(gate,'job_coverage',None)
        self.assertTrue(callable(collect) and callable(cover),'BODY RED: independent node bijection absent')
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);scripts=root/'.github/scripts';scripts.mkdir(parents=True)
            for name in ('pytest_next_offline.py','pytest_next_worker.py','pytest_u13_worker.py'):
                shutil.copyfile(ROOT/'.github/scripts'/name,scripts/name)
            suites=['next/tests/test_job_'+str(i)+'.py' for i in range(8)]
            independent=[];parts=[]
            env={k:os.environ[k] for k in ('PATH','LANG','LC_ALL','PYTHONPATH') if k in os.environ}
            # Nested workers use this interpreter's already loaded pytest site.
            # The read-only local 3.13 fixture uses an existing shared pure site.
            import pytest
            env['PYTHONPATH']=str(Path(pytest.__file__).resolve().parents[1])+os.pathsep+env.get('PYTHONPATH','')
            env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',PYTHONDONTWRITEBYTECODE='1')
            for index,suite in enumerate(suites):
                path=root/suite;path.parent.mkdir(exist_ok=True,parents=True)
                path.write_text('def test_one(): assert 2+2==4\ndef test_two(): assert 3+3==6\n')
                def command(label):
                    return [sys.executable,'-B',str(scripts/'pytest_next_worker.py'),'--suite',suite,
                            '--output',str(root/(label+'.json')),'--temporary',str(root/(label+'-tmp'))]
                baseline=collect(command('collect-'+str(index)),root,env)
                self.assertEqual(baseline['reports'],[])
                self.assertEqual(baseline['pytest_exit'],0)
                self.assertEqual(baseline['gate_exit'],1,'collection must have zero execution credit')
                independent.append(baseline)
                cmd=command('run-'+str(index));done=subprocess.run(cmd,cwd=root,env=env,capture_output=True,text=True,timeout=30)
                self.assertEqual(done.returncode,0,done.stdout+done.stderr)
                packet=json.loads(Path(cmd[cmd.index('--output')+1]).read_bytes())
                self.assertTrue(gate.receipt_valid(packet,0))
                self.assertEqual(len(packet['reports']),6)
                parts.append(dict(index=index,suites=[suite],outcomes=[packet]))
            self.assertTrue(cover(suites,parts,independent))
            self.assertEqual(len([n for p in independent for n in p['collected']]),16)
            for fault in ('missing-part','duplicate-part','foreign-part','empty-selector','foreign-suite',
                          'adjacent-escape','missing-node','duplicate-node','extra-node','changed-source',
                          'missing-independent','duplicate-independent','changed-independent'):
                bad=copy.deepcopy(parts);full=copy.deepcopy(independent)
                if fault=='missing-part':bad.pop()
                elif fault=='duplicate-part':bad[1]=copy.deepcopy(bad[0])
                elif fault=='foreign-part':bad[0]['index']=8
                elif fault=='empty-selector':bad[0]['suites']=[]
                elif fault=='foreign-suite':bad[0]['suites']=['next/tests/test_foreign.py']
                elif fault=='adjacent-escape':bad[0]['outcomes'][0]['collected'][0]='next/tests/../test_job_0.py::test_one'
                elif fault=='missing-node':bad[0]['outcomes'][0]['collected'].pop()
                elif fault=='duplicate-node':bad[0]['outcomes'][0]['collected']*=2
                elif fault=='extra-node':bad[0]['outcomes'][0]['collected'].append(suites[0]+'::test_extra')
                elif fault=='changed-source':bad[0]['outcomes'][0]['source_sha256']='f'*64
                elif fault=='missing-independent':full.pop()
                elif fault=='duplicate-independent':full[1]=copy.deepcopy(full[0])
                elif fault=='changed-independent':full[0]['collected'].pop()
                with self.subTest(fault=fault):self.assertFalse(cover(suites,bad,full))
            self.assertTrue(cover(suites,copy.deepcopy(parts),copy.deepcopy(independent)))
            # Exercise the required artifact join, including its strict phase oracle.
            checked=dict(run=suites,loopback=suites,retired=[],method_exclusions=[],replacement_nodes={})
            identity=dict(head='a'*40,sha256={'source':'b'*64});artifacts=root/'artifacts';artifacts.mkdir()
            prefix='next-offline-receipts-'+sys.version.split()[0]+'-'
            for part in parts:
                for home,folder in (('run','next-offline-receipts'),('loopback','next-loopback-receipts')):
                    directory=artifacts/(prefix+str(part['index']))/folder;directory.mkdir(parents=True)
                    (directory/'0.json').write_text(json.dumps(part['outcomes'][0]))
                    packet=dict(gate_exit=0,source_identity=identity,base_ref='c'*40,inventory=checked,
                        python=sys.version,job_partition=dict(index=part['index'],count=8,suites=part['suites']),
                        owned_case_root_absent=True,unpartitioned_collection=independent if part['index']==0 else [],
                        qualification='HOSTED_LOOPBACK_SOURCE_CONTRACTS_ONLY' if home=='loopback' else 'HOSTED_OFFLINE_SOURCE_CONTRACTS_ONLY',
                        native_qualified=False,installed_qualified=False,
                        suites=[dict(suite=part['suites'][0],receipt='0.json',process_exit=0,ok=True,source_unchanged=True,
                                     source_sha256=part['outcomes'][0]['source_sha256'])])
                    (directory/'summary.json').write_text(json.dumps(packet))
            original_root=gate.ROOT;gate.ROOT=root
            try:
                joined=gate.verify_job_partitions(artifacts,checked,identity,'c'*40)
                self.assertEqual(joined['gate_exit'],0)
                self.assertEqual([joined['homes'][h]['nodes'] for h in ('run','loopback')],[16,16])
                for name,fault in (('summary.json','missing-home'),('summary.json','wrong-head'),
                                   ('summary.json','wrong-python'),('summary.json','wrong-base'),
                                   ('summary.json','installed-credit'),('summary.json','missing-selector'),
                                   ('summary.json','false-cleanup'),('0.json','missing-phase'),
                                   ('0.json','skip'),('0.json','changed-pin')):
                    path=artifacts/(prefix+'0')/'next-loopback-receipts'/name;raw=path.read_bytes()
                    bad=json.loads(raw)
                    if fault=='missing-home':path.unlink()
                    else:
                        if fault=='wrong-head':bad['source_identity']={**identity,'head':'f'*40}
                        elif fault=='wrong-python':bad['python']='3.14.7'
                        elif fault=='wrong-base':bad['base_ref']='d'*40
                        elif fault=='installed-credit':bad['installed_qualified']=True
                        elif fault=='missing-selector':bad['job_partition']['suites']=[]
                        elif fault=='false-cleanup':bad['owned_case_root_absent']=False
                        elif fault=='missing-phase':bad['reports'].pop()
                        elif fault=='skip':bad['reports'][1]['outcome']='skipped'
                        elif fault=='changed-pin':bad['source_sha256']='e'*64
                        path.write_text(json.dumps(bad))
                    try:
                        with self.subTest(fault=fault),self.assertRaises((ValueError,OSError)):
                            gate.verify_job_partitions(artifacts,checked,identity,'c'*40)
                    finally:path.write_bytes(raw)
                extra=artifacts/'foreign';extra.mkdir()
                with self.assertRaises(ValueError):gate.verify_job_partitions(artifacts,checked,identity,'c'*40)
                extra.rmdir()
                self.assertEqual(gate.verify_job_partitions(artifacts,checked,identity,'c'*40)['gate_exit'],0)
            finally:gate.ROOT=original_root
            destination=os.environ.get('CORTEX_NEXT_ORACLE_OUTPUT')
            if destination:
                target=Path(destination)/'job-coverage';target.mkdir(parents=True,exist_ok=True)
                for path in root.glob('*.json'):shutil.copyfile(path,target/path.name)
                for path in root.glob('*.txt'):shutil.copyfile(path,target/path.name)
                shutil.copytree(artifacts,target/'artifacts')
                (target/'joined.json').write_text(json.dumps(joined,indent=2)+'\n')

    def test_required_workflow_has_eight_partitions_and_independent_coverage_home(self):
        source=(ROOT/'.github/workflows/next-offline.yml').read_text()
        self.assertIn('partition: [0, 1, 2, 3, 4, 5, 6, 7]',source,'BODY RED: finite job matrix absent')
        self.assertEqual(source.count('--job-partition "${{ matrix.partition }}"'),2)
        self.assertIn('next-offline-coverage:',source)
        coverage=source.split('  next-offline-coverage:',1)[1]
        self.assertIn('needs: next-offline',coverage)
        self.assertIn('--verify-job-partitions',coverage)
        self.assertNotIn('continue-on-error',source)
        self.assertEqual(source.count("python: ['3.13.7', '3.14.0']"),2)
        self.assertIn('timeout-minutes: 30',source)
        self.assertIn('name: next-offline-receipts-${{ matrix.python }}-${{ matrix.partition }}',source)
if __name__=='__main__':unittest.main()
