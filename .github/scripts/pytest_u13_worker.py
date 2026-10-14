"""Exact eight-method U13 PG gate inside the existing restricted CI image."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

TARGETS = (
    'next/tests/core_upgrade/test_migration_steps.py::MigrationSteps::test_named_prefix_and_reentry',
    'next/tests/core_upgrade/test_migration_steps.py::MigrationSteps::test_unknown_target_and_nonprefix_ledger_refuse_before_sql',
    'next/tests/core_upgrade/test_rehearsal.py::Rehearsal::test_two_step_crash_reentry_preserves_canonical_state',
    'next/tests/core_upgrade/test_rehearsal.py::Rehearsal::test_real_pg_rollback_after_every_expand_prefix',
    'next/tests/core_upgrade/test_rehearsal.py::Rehearsal::test_paused_real_pg_apply_drains_before_rollback',
    'next/tests/core_upgrade_head/test_migration_head.py::CurrentManifestStaging::test_current_four_steps_reconnect_reenter_and_preserve_seed',
    'next/tests/core_upgrade_head/test_migration_head.py::CurrentManifestStaging::test_unknown_target_refuses_before_any_later_sql',
    'next/tests/core_upgrade_head/test_migration_head.py::CurrentManifestStaging::test_nonprefix_drift_and_future_ledger_refuse_before_sql',
)

def valid(expected, packet):
    nodes = packet['selected']
    if (not expected or len(set(expected)) != len(expected) or len(nodes) != len(expected)
            or set(nodes) != set(expected) or len(set(nodes)) != len(nodes)
            or packet['deselected'] or packet['pytest_exit'] != 0):
        return False
    phases = {(node, phase): 0 for node in expected for phase in ('setup', 'call', 'teardown')}
    for row in packet['reports']:
        key = (row['nodeid'], row['when'])
        if key not in phases or row['outcome'] != 'passed' or row['wasxfail']:
            return False
        phases[key] += 1
    return all(count == 1 for count in phases.values())

class Gate:
    def __init__(self, output, control):
        self.output, self.control = output, control
        self.packet = {'expected': list(TARGETS), 'selected': [], 'deselected': [], 'reports': []}

    def pytest_collection_finish(self, session):
        self.packet['selected'] = [item.nodeid for item in session.items]
        if self.control == 'skip' and session.items:
            import pytest
            session.items[-1].add_marker(pytest.mark.skip(reason='U13 gate forced-skip control'))

    def pytest_deselected(self, items):
        self.packet['deselected'].extend(item.nodeid for item in items)

    def pytest_runtest_logreport(self, report):
        self.packet['reports'].append({'nodeid': report.nodeid, 'when': report.when,
            'outcome': report.outcome, 'wasxfail': hasattr(report, 'wasxfail'),
            'detail': str(report.longrepr) if report.failed or report.skipped else ''})

    def pytest_sessionfinish(self, session, exitstatus):
        self.packet['pytest_exit'] = int(exitstatus)
        ok = valid(TARGETS, self.packet)
        self.packet.update(gate_exit=0 if ok else 1,
            passed=[r['nodeid'] for r in self.packet['reports'] if r['when']=='call' and r['outcome']=='passed'],
            control=self.control, qualification='HOSTED_CI_PG_ONLY', native_qualified=False,
            old_installation='G_OLD_UNRESOLVED')
        self.output.write_text(json.dumps(self.packet, indent=2)+'\n')
        session.exitstatus = self.packet['gate_exit']

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--control', choices=('normal','missing','skip','mutant'), default='normal')
    args = parser.parse_args()
    if os.getuid() != 10001 or os.getgid() != 10001:
        raise RuntimeError('U13 worker must use the isolated nonroot identity')
    status = dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    if int(status['CapEff'].strip(),16) or status['NoNewPrivs'].strip() != '1':
        raise RuntimeError('U13 worker capabilities or privilege contract differs')
    root_options = next(line.split()[3].split(',') for line in Path('/proc/mounts').read_text().splitlines() if line.split()[1]=='/')
    if 'ro' not in root_options: raise RuntimeError('U13 worker root must be read-only')
    source = Path('/source')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='u13-cases-') as temporary:
        root = Path(temporary)
        os.chmod(root, 0o700)
        os.environ['CORTEX_U13_CASE_ROOT'] = str(root)
        source_root = source/'next'
        mutation = None
        if args.control == 'mutant':
            registry = source_root/'tests/core_upgrade/run_host_mutations.py'
            spec = importlib.util.spec_from_file_location('u13_inherited_registry', registry)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            relative, before, after, _ = module.MUTANTS['through']
            # Head mutants.py selects this same migration-only registry entry.
            copy = root/'mutant-next'
            shutil.copytree(source_root/'src', copy/'src')
            shutil.copytree(source_root/'schema', copy/'schema')
            target = copy/relative
            raw = target.read_bytes()
            text = raw.decode()
            if text.count(before) != 1: raise RuntimeError('nonunique inherited through mutant')
            changed = text.replace(before, after).encode()
            compile(changed, str(target), 'exec')
            target.write_bytes(changed)
            mutation = {'name':'through', 'registry_sha256':hashlib.sha256(registry.read_bytes()).hexdigest(),
                'head_mutants_sha256':hashlib.sha256((source_root/'tests/core_upgrade_head/mutants.py').read_bytes()).hexdigest(),
                'relative':relative, 'one_locus':True, 'original_sha256':hashlib.sha256(raw).hexdigest(),
                'mutant_sha256':hashlib.sha256(changed).hexdigest()}
            source_root = copy
        sys.path[:0] = [str(source_root/'src'), str(source/'next/tests/core_upgrade'),
                       str(source/'next/tests/core_upgrade_head'), str(source/'next/tests/schema')]
        import psycopg
        import pytest
        gate = Gate(args.output, args.control)
        gate.packet.update(source_files={path:hashlib.sha256((source/path.split('::')[0]).read_bytes()).hexdigest()
                                        for path in TARGETS}, mutation=mutation,
                           driver={'psycopg':psycopg.__version__, 'pq_impl':psycopg.pq.__impl__,
                                   'libpq_version':psycopg.pq.version()},
                           case_root={'uid':os.stat(root).st_uid,'mode':oct(os.stat(root).st_mode & 0o777)})
        os.chdir(source)
        targets = TARGETS[:-1] if args.control=='missing' else TARGETS
        result = pytest.main(['-q','--tb=short','--strict-markers','--rootdir='+str(source),'-o','cache_dir='+str(root/'cache'),
                              '--basetemp='+str(root/'pytest'), *targets], plugins=[gate])
        from cortex_core import migrations
        loaded = Path(migrations.__file__).resolve()
        if loaded != (source_root/'src/cortex_core/migrations.py').resolve():
            raise RuntimeError('U13 loaded migration source differs from selected root')
        gate.packet['loaded_migrations'] = {'path':str(loaded), 'sha256':hashlib.sha256(loaded.read_bytes()).hexdigest()}
        if mutation and gate.packet['loaded_migrations']['sha256'] != mutation['mutant_sha256']:
            raise RuntimeError('U13 mutant not actually loaded')
        gate.packet['runtime'] = {'uid':os.getuid(),'gid':os.getgid(),'cap_eff':status['CapEff'].strip(),
                                  'no_new_privs':1,'root_read_only':True}
        args.output.write_text(json.dumps(gate.packet,indent=2)+'\n')
    gate.packet['case_root_cleanup'] = {'owned_root_absent':not root.exists()}
    args.output.write_text(json.dumps(gate.packet,indent=2)+'\n')
    if root.exists(): raise RuntimeError('owned U13 case root remains')
    return int(result)

if __name__ == '__main__': raise SystemExit(main())
