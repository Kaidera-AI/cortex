"""One-locus actual-source mutations, explicit receipts, exact restoration. Host only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
RUNNER=ROOT/'tests/core_upgrade_signed/run_host.py'
UP='src/cortex_core/upgrade.py'
MIG='src/cortex_core/migrations.py'
MUTANTS={
 'signature':(UP,'if result.returncode != 0:','if False:',
  'test_signed_admission.SignedAdmission.test_wrong_signature_refuses'),
 'digest':(UP,'if hashlib.sha256(raw).hexdigest() != pinned_digest:','if False:',
  'test_signed_admission.SignedAdmission.test_changed_digest_even_if_resigned_refuses'),
 'absolute_binary':(UP,'if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):','if False:',
  'test_signed_admission.SignedAdmission.test_relative_or_missing_minisign_binary_refuses_without_path_lookup'),
 'event_identity':(UP,"old['event_version'], new['event_version'],","old['event_version'], old['event_version'],",
  'test_signed_admission.SignedAdmission.test_current_suffix_and_event_versions_for_both_edition_policies'),
 'writer_policy':(UP,"        or (new['release'], old['schema_version']) in writers):","        ):",
  'test_signed_admission.SignedAdmission.test_new_writer_on_old_schema_refuses'),
 'pinned_key':(UP,'old_signature, stable_key,','old_signature, trusted_public_key,','KEY'),
 'preservation':(UP,'if self.ports.validate_preservation() is not True:','if False:',
  'test_current_coordinator_bridge.CurrentBridge.test_preservation_failure_keeps_current_prefix_prepared'),
 'backup_fence':(UP,"        self.ports.fence_old()\n        if self.ports.verified_backup() is not True:\n            raise UpgradeRefusal('backup_unverified')",
  "        if self.ports.verified_backup() is not True:\n            raise UpgradeRefusal('backup_unverified')\n        self.ports.fence_old()",
  'review_rollback_marker.RollbackMarker.test_prepare_closes_old_writer_before_verified_backup'),
 'rollback_marker':(UP,"        if state['phase'] == 'prepared':\n            self.ports.save({'pair': self.pair, 'phase': 'rolling_back',",
  "        if state['phase'] == 'prepared':\n            self.ports.fence_new()\n            self.ports.save({'pair': self.pair, 'phase': 'rolling_back',",
  'review_rollback_marker.RollbackMarker.test_rolling_back_marker_prevents_interleaved_acceptance'),
 'journal_cas':(UP,'if expected is not _UNSET and previous != expected:','if False:',
  'test_journal.JournalTests.test_stale_prepared_snapshot_cannot_advance_journal'),
 'advance_lock':(UP,'    @_serialized\n    def advance(self):','    def advance(self):',
  'test_current_coordinator_bridge.CurrentBridge.test_current_apply_excludes_rollback_restore_until_operation_drains'),
 'accept_lock':(UP,'    @_serialized\n    def accept(self):','    def accept(self):',
  'test_current_coordinator_bridge.CurrentBridge.test_current_rollback_excludes_acceptance_until_restore_finishes'),
 'through':(MIG,'prepared_target = next(i for i, item in enumerate(prepared) if item[0] == through)',
  'prepared_target = len(prepared) - 1',
  'test_current_coordinator_bridge.CurrentBridge.test_staged_each_current_suffix_and_reentry_uses_full_current_ledger'),
 'prefix':(MIG,'if ledger_ids != {item[0] for item in prepared[:len(ledger_ids)]}:','if False:',
  'test_current_coordinator_bridge.CurrentBridge.test_nonprefix_current_ledger_refuses_before_current_suffix'),
 'checksum':(MIG,'if identity in ledger and ledger[identity] != digest:','if False:',
  'test_current_coordinator_bridge.CurrentBridge.test_unknown_target_and_donor_checksum_refuse_before_later_sql'),
 'unknown_target':(MIG,'raise MigrationError("Unknown migration target")','return []',
  'test_current_coordinator_bridge.CurrentBridge.test_unknown_target_and_donor_checksum_refuse_before_later_sql'),
 'reentry':(MIG,'            if identity in ledger:\n                continue','            if False:\n                continue',
  'test_current_coordinator_bridge.CurrentBridge.test_staged_each_current_suffix_and_reentry_uses_full_current_ledger'),
}

def main(names=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    output=Path(args.output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    branch=subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip()
    if branch!='cox/u13-composition-20261014':
        raise RuntimeError('mutations require the explicitly owned U13 worktree branch')
    if os.environ.get('CORE_UPGRADE_TEST_SOURCE'):
        raise RuntimeError('stale source override forbidden')
    receipts={}
    for name in names or MUTANTS:
        relative,before,after,selector=MUTANTS[name]
        path=ROOT/relative
        original=path.read_bytes()
        text=original.decode()
        if text.count(before)!=1: raise RuntimeError('nonunique actual mutation locus: '+name)
        mutated=text.replace(before,after).encode()
        compile(mutated,str(path),'exec')
        path.write_bytes(mutated)
        try:
            command=[sys.executable,'-B',str(RUNNER)]
            command+=['--key-control'] if selector=='KEY' else ['--selector',selector]
            result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=30)
            lines=[x for x in result.stdout.splitlines() if x.startswith('CORTEX_U13_SOURCE_RESULT=')]
            packet=json.loads(lines[-1].split('=',1)[1]) if len(lines)==1 else None
            expected='CurrentKeyPath.test_pinned_key_bytes_refuse_replacement' if selector=='KEY' else selector
            failures=[] if packet is None else packet['failures']
            witness=[] if packet is None else [x for x in packet['loaded_witness']
                if Path(x['path']).resolve()==path.resolve()]
            killed=(result.returncode==1 and packet is not None and not packet['errors']
                    and not packet['skips'] and failures and
                    all(x['exception']=='AssertionError' and expected in x['test'] for x in failures)
                    and witness and all(x['matches_current_compile'] and
                        x['source_sha256']==hashlib.sha256(mutated).hexdigest() for x in witness))
            receipt={'classification':'body_killed' if killed else 'inconclusive',
                     'selector':selector,'one_locus':True,'source_path':str(path),
                     'original_sha256':hashlib.sha256(original).hexdigest(),
                     'mutant_sha256':hashlib.sha256(mutated).hexdigest(),
                     'exit_status':result.returncode,'packet':packet,
                     'stdout':result.stdout,'stderr':result.stderr}
        finally:
            path.write_bytes(original)
            if path.read_bytes()!=original: raise RuntimeError('source restoration failed')
        receipt['restored_byte_exact']=True
        (output/(name+'.json')).write_text(json.dumps(receipt,indent=2)+'\n')
        receipts[name]={k:receipt[k] for k in ('classification','selector','one_locus',
                                             'original_sha256','mutant_sha256','restored_byte_exact')}
        print(name,receipt['classification'],flush=True)
    (output/'INDEX.json').write_text(json.dumps(receipts,indent=2)+'\n')
    return 0 if all(x['classification']=='body_killed' for x in receipts.values()) else 1

if __name__=='__main__':
    raise SystemExit(main())
