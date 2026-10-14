"""Host-owned U13 inventory and closed, bounded worker-receipt admission."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat

# Deliberately independent of the worker's TARGETS or valid() implementation.
HOST_TARGETS = (
    'next/tests/core_upgrade/test_migration_steps.py::MigrationSteps::test_named_prefix_and_reentry',
    'next/tests/core_upgrade/test_migration_steps.py::MigrationSteps::test_unknown_target_and_nonprefix_ledger_refuse_before_sql',
    'next/tests/core_upgrade/test_rehearsal.py::Rehearsal::test_two_step_crash_reentry_preserves_canonical_state',
    'next/tests/core_upgrade/test_rehearsal.py::Rehearsal::test_real_pg_rollback_after_every_expand_prefix',
    'next/tests/core_upgrade/test_rehearsal.py::Rehearsal::test_paused_real_pg_apply_drains_before_rollback',
    'next/tests/core_upgrade_head/test_migration_head.py::CurrentManifestStaging::test_current_four_steps_reconnect_reenter_and_preserve_seed',
    'next/tests/core_upgrade_head/test_migration_head.py::CurrentManifestStaging::test_unknown_target_refuses_before_any_later_sql',
    'next/tests/core_upgrade_head/test_migration_head.py::CurrentManifestStaging::test_nonprefix_drift_and_future_ledger_refuse_before_sql',
)
MAX_BYTES = 1024 * 1024
FIELDS = {'schema_version','receipt_run_id','session_finished','worker_sha256',
    'expected','selected','deselected','reports','counts','source_files','mutation','driver',
    'case_root','pytest_exit','gate_exit','passed','control','qualification','native_qualified',
    'old_installation','loaded_migrations','runtime','case_root_cleanup'}


class ReceiptRefusal(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise ReceiptRefusal(code)


def expected_receipt(source, run_id, control):
    source = Path(source)
    digest = lambda name: hashlib.sha256((source/name).read_bytes()).hexdigest()
    return dict(run_id=run_id, control=control,
        worker_sha256=digest('.github/scripts/pytest_u13_worker.py'),
        source_files={node:digest(node.split('::')[0]) for node in HOST_TARGETS},
        loaded_migrations=dict(path='/source/next/src/cortex_core/migrations.py',
                               sha256=digest('next/src/cortex_core/migrations.py')),
        counts=dict(selected=len(HOST_TARGETS), passed=len(HOST_TARGETS),
                    phases=3*len(HOST_TARGETS), deselected=0))


def object_pairs(pairs):
    require(len({key for key,_ in pairs}) == len(pairs), 'receipt_duplicate_keys')
    return dict(pairs)


def closed(value, keys):
    return type(value) is dict and set(value) == set(keys)


def sha(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def string_list(value):
    return type(value) is list and all(type(item) is str for item in value)


def schema(packet):
    if not closed(packet, FIELDS): return False
    if not (type(packet['schema_version']) is int and packet['schema_version']==1
            and type(packet['receipt_run_id']) is str and type(packet['session_finished']) is bool
            and sha(packet['worker_sha256']) and packet['mutation'] is None
            and all(string_list(packet[key]) for key in ('expected','selected','deselected','passed'))
            and type(packet['reports']) is list
            and all(type(packet[key]) is int for key in ('pytest_exit','gate_exit'))
            and all(type(packet[key]) is str for key in ('control','qualification','old_installation'))
            and type(packet['native_qualified']) is bool): return False
    for row in packet['reports']:
        if not (closed(row, ('nodeid','when','outcome','wasxfail','detail'))
                and type(row['nodeid']) is str and row['when'] in ('setup','call','teardown')
                and row['outcome'] in ('passed','failed','skipped')
                and type(row['wasxfail']) is bool and type(row['detail']) is str): return False
    counts=packet['counts']; sources=packet['source_files']; driver=packet['driver']
    case=packet['case_root']; loaded=packet['loaded_migrations']; runtime=packet['runtime']; cleanup=packet['case_root_cleanup']
    return (closed(counts, ('selected','passed','phases','deselected'))
        and all(type(value) is int and value>=0 for value in counts.values())
        and type(sources) is dict and all(type(key) is str and sha(value) for key,value in sources.items())
        and closed(driver, ('psycopg','pq_impl','libpq_version'))
        and type(driver['psycopg']) is str and type(driver['pq_impl']) is str
        and type(driver['libpq_version']) is int and driver['libpq_version']>0
        and closed(case, ('uid','mode')) and type(case['uid']) is int and type(case['mode']) is str
        and closed(loaded, ('path','sha256')) and type(loaded['path']) is str and sha(loaded['sha256'])
        and closed(runtime, ('uid','gid','cap_eff','no_new_privs','root_read_only'))
        and all(type(runtime[key]) is int for key in ('uid','gid','no_new_privs'))
        and type(runtime['cap_eff']) is str and type(runtime['root_read_only']) is bool
        and closed(cleanup, ('owned_root_absent',)) and type(cleanup['owned_root_absent']) is bool)


def admit_receipt(path, expected, worker_exit):
    require(type(worker_exit) is int and worker_exit == 0, 'worker_nonzero')
    try:
        fd=os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise ReceiptRefusal('receipt_missing') from None
    except OSError:
        raise ReceiptRefusal('receipt_type') from None
    try:
        info=os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'receipt_regular')
        require(0 < info.st_size <= MAX_BYTES, 'receipt_size')
        # The checked stat size bounds this read. One extra byte detects growth.
        raw=b''
        while len(raw) <= info.st_size:
            chunk=os.read(fd, info.st_size+1-len(raw))
            if not chunk: break
            raw+=chunk
        after=os.fstat(fd)
        fields=('st_dev','st_ino','st_mode','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
        require(len(raw)==info.st_size and all(getattr(info,key)==getattr(after,key) for key in fields), 'receipt_changed')
    except OSError:
        raise ReceiptRefusal('receipt_io') from None
    finally:
        os.close(fd)
    try:
        packet=json.loads(raw.decode('utf-8'), object_pairs_hook=object_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except ReceiptRefusal:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise ReceiptRefusal('receipt_json') from None
    require(schema(packet), 'receipt_schema')
    require(packet['receipt_run_id']==expected['run_id'], 'receipt_run_binding')
    require(packet['session_finished'] is True, 'receipt_session_finish')
    require(packet['pytest_exit']==0 and packet['gate_exit']==0, 'receipt_recorded_exit')
    nodes=set(HOST_TARGETS)
    require(packet['expected']==list(HOST_TARGETS) and not packet['deselected']
            and all(len(packet[key])==len(nodes) and len(set(packet[key]))==len(nodes)
                    and set(packet[key])==nodes for key in ('selected','passed')), 'receipt_inventory')
    phases=Counter((row['nodeid'],row['when']) for row in packet['reports'])
    require(phases==Counter({(node,phase):1 for node in HOST_TARGETS for phase in ('setup','call','teardown')}), 'receipt_phases')
    require(all(row['outcome']=='passed' and not row['wasxfail'] for row in packet['reports']), 'receipt_outcomes')
    actual=dict(selected=len(packet['selected']), passed=sum(row['when']=='call' and row['outcome']=='passed'
                and not row['wasxfail'] for row in packet['reports']),
                phases=len(packet['reports']), deselected=len(packet['deselected']))
    require(packet['counts']==expected['counts'] and actual==expected['counts'], 'receipt_counts')
    require(packet['source_files']==expected['source_files'] and packet['worker_sha256']==expected['worker_sha256']
            and packet['loaded_migrations']==expected['loaded_migrations'], 'receipt_source_binding')
    require(packet['case_root_cleanup']['owned_root_absent'] is True, 'receipt_completion')
    runtime=packet['runtime']
    require(runtime==dict(uid=10001,gid=10001,cap_eff='0000000000000000',no_new_privs=1,root_read_only=True)
            and packet['case_root']==dict(uid=10001,mode='0o700')
            and packet['driver']['psycopg']=='3.2.9' and packet['driver']['pq_impl']=='python', 'receipt_runtime')
    require(packet['control']==expected['control']=='normal' and packet['qualification']=='HOSTED_CI_PG_ONLY'
            and packet['native_qualified'] is False and packet['old_installation']=='G_OLD_UNRESOLVED', 'receipt_qualification')
    return dict(ok=True, code='receipt_accepted', receipt_sha256=hashlib.sha256(raw).hexdigest(),
                receipt_bytes=len(raw), counts=actual, run_id=expected['run_id'],
                expected_source_files=expected['source_files'], worker_sha256=expected['worker_sha256'],
                loaded_migrations=expected['loaded_migrations'])
