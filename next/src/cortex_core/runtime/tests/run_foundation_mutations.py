"""Per-file causal witnesses; reuse the shared expected-body classifier."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

NEXT=Path(__file__).resolve().parents[4]
SPEC=[
 ('src/cortex_core/runtime/bootstrap_config.py',"if value['ai_mode'] != 'off':","if False:",'runtime','test_bootstrap_config.ConfigTests.test_closed_shapes_and_explicit_ai'),
 ('src/cortex_core/runtime/credentials.py','or before.st_mode & 0o077','or False','runtime','test_bootstrap_config.CredentialTests.test_unsafe_or_missing_secret_has_closed_error'),
 ('src/cortex_core/runtime/contracts.py','if TYPE_CHECKING:','if True:','runtime','test_bootstrap_config.ConfigTests.test_stdlib_only_host_decoder'),
 ('src/cortex_core/release/codec.py','import hashlib','import jsonschema\nimport hashlib','runtime','test_bootstrap_config.ConfigTests.test_stdlib_only_host_decoder'),
 ('src/cortex_core/migrations.py','if hashlib.sha256(data).hexdigest() != entry["sha256"]:','if False:','schema','test_readiness.ReadinessTests.test_prefix_byte_change_before_sql'),
 ('src/cortex_core/schema/migrate.py','return migrations.apply_migrations(connection, directory)','return []','schema','test_readiness.ReadinessTests.test_existing_runner_is_only_migration_engine'),
 ('src/cortex_core/schema/readiness.py','if len(rows) != len(expected) or dict(rows) != dict(expected):','if False:','schema','test_readiness.ReadinessTests.test_wrong_applied_order_identity_and_digest_refuse'),
]


def main():
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
    spec=importlib.util.spec_from_file_location('receipts',NEXT/'tests/test_receipts.py')
    receipts=importlib.util.module_from_spec(spec);spec.loader.exec_module(receipts)
    rows=[]
    for index,(relative,old,new,group,expected) in enumerate(SPEC):
        with tempfile.TemporaryDirectory(prefix='mutant-',dir=out) as tmp:
            case=Path(tmp)/'next'
            shutil.copytree(NEXT/'src',case/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            shutil.copytree(NEXT/'schema',case/'schema')
            path=case/relative;before=path.read_bytes();text=before.decode()
            if text.count(old)!=1:raise ValueError('mutation anchor must be unique: '+relative)
            path.write_text(text.replace(old,new,1))
            testdir=case/'src/cortex_core'/group/'tests'
            result=subprocess.run([sys.executable,str(NEXT/'tests/test_receipts.py'),str(testdir)],
                       env={**os.environ,'PYTHONPATH':str(case/'src')},capture_output=True,text=True,timeout=90)
            status=receipts.classify(result,{expected})
            (out/f'{index+1:02d}.stdout').write_text(result.stdout)
            (out/f'{index+1:02d}.stderr').write_text(result.stderr)
            row=dict(path=relative,source_sha256=hashlib.sha256(before).hexdigest(),
                     mutant_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                     expected_test=expected,classification=status,exit_code=result.returncode,
                     test_result=receipts.report(result),stdout=f'{index+1:02d}.stdout',stderr=f'{index+1:02d}.stderr')
            rows.append(row);print(relative,status,flush=True)
    summary={'schema':'cortex.foundation.causal.v1','source_root':str(NEXT),
             'mutants':rows,'killed':sum(r['classification']=='killed' for r in rows),
             'inconclusive':sum(r['classification']=='inconclusive' for r in rows),
             'survived':sum(r['classification']=='survived' for r in rows)}
    (out/'RESULT.json').write_text(json.dumps(summary,indent=2)+'\n')
    return 0 if summary['killed']==len(SPEC) else 1


if __name__=='__main__':raise SystemExit(main())
