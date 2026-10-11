"""Offline mutation controls on owned source copies, retaining exact drivers."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT=Path(__file__).resolve().parents[1]
MUTATIONS=[
    ('expired-admission','dispatch.py','        authority.check_measured_receipts(rows[-1]["payload"]["checks"])',
     '        pass  # mutant: expired checks admitted','test_dispatch_receipts.DispatchReceiptCase.test_expired_admission_refuses_before_launcher_effect'),
    ('header-not-derived','native_receipts.py','require(payload == facts.get("facts"), "native_payload_not_derived")',
     'require(True, "native_payload_not_derived")','test_dispatch_receipts.DispatchReceiptCase.test_native_output_derivation_and_replay_refusal'),
    ('signed-delivery-bypass','payload.py','        bound_file(row)',
     '        pass  # mutant: extracted bytes approve themselves','test_last_boundaries.LastBoundariesCase.test_extracted_compose_and_self_checksums_cannot_escape_signed_map'),
    ('restoration-raw-bypass','restoration.py','require(raw.get("facts") == expected, "restoration_raw_facts_disagree")',
     'require(True, "restoration_raw_facts_disagree")','test_completion_guards.CompletionCase.test_recovery_raw_and_boot_effects'),
    ('ssh-strict-bypass','dispatch.py','required_options <= set(options)',
     'True','test_ssh_binding.SSHBindingCase.test_strict_ssh_options_and_actual_known_hosts_are_bound_before_effect'),
    ('console-budget-bypass','protocols.py','timeout=min(spec["timeout_sec"], remaining)',
     'timeout=spec["timeout_sec"]','test_protocol_completion.ProtocolCompletionCase.test_console_timeout_is_clamped_to_remaining_wait_budget'),
]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    output=Path(args.output).resolve();output.mkdir(mode=0o700,exist_ok=False)
    results=[]
    for name,file,before,after,driver in MUTATIONS:
        folder=output/name;folder.mkdir(mode=0o700)
        baseline=folder/'source'
        shutil.copytree(ROOT,baseline,ignore=shutil.ignore_patterns('__pycache__','run8-synthetic-*'))
        target=baseline/file;body=target.read_text();assert body.count(before)==1,(name,body.count(before))
        driver_file=baseline/'tests'/('test_'+driver.split('.')[0].removeprefix('test_')+'.py')
        driver_sha=hashlib.sha256(driver_file.read_bytes()).hexdigest()
        command=['python3','-m','unittest','-v',driver]
        positive=subprocess.run(command,cwd=baseline/'tests',capture_output=True)
        (folder/'baseline.stdout').write_bytes(positive.stdout);(folder/'baseline.stderr').write_bytes(positive.stderr)
        assert positive.returncode==0,(name,positive.stderr.decode())
        target.write_text(body.replace(before,after))
        negative=subprocess.run(command,cwd=baseline/'tests',capture_output=True)
        (folder/'mutant.stdout').write_bytes(negative.stdout);(folder/'mutant.stderr').write_bytes(negative.stderr)
        assert negative.returncode==1 and b'FAIL:' in negative.stderr and b'ERROR:' not in negative.stderr,(name,negative.stderr.decode())
        assert hashlib.sha256(driver_file.read_bytes()).hexdigest()==driver_sha
        results.append({'name':name,'file':file,'driver':driver,'driver_sha256':driver_sha,
            'before':before,'after':after,'baseline_exit':0,'mutant_exit':1,'body_failure':True,'errors':0,
            'source_sha256':hashlib.sha256(body.encode()).hexdigest(),'mutant_sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
    (output/'MUTATION_INDEX.json').write_text(json.dumps({'qualification':False,'controls':results},indent=2)+'\n')
    print(json.dumps({'mutants_killed_in_assertions':len(results),'errors':0,'drivers_unchanged':True}))


if __name__=='__main__':main()
