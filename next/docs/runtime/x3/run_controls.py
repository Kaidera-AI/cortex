"""Run X3 source guard controls serially in an exclusively owned worktree.

Every mutation restores exact bytes in finally. No provider, driver or native IO.
Usage: PYTHONPATH=next/src python -B next/docs/runtime/x3/run_controls.py OUT
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[4]
SRC = 'next/src/cortex_core/runtime/'
TEST = 'next/tests/test_x3_secret_resolver.py'
CONTROLS = [
    ('supplier-export', 'secret_resolver.py',
     '        await self._current(handle,absolute_deadline)\n',
     '        return await self._current(handle,absolute_deadline)\n',
     'test_x301_public_check_does_not_export_supplier_or_grant'),
    ('revision-fence', 'secret_resolver.py',
     "('reference','revision','installation','purpose','mode','process','epoch',",
     "('reference','installation','purpose','mode','process','epoch',",
     'test_x301_process_install_purpose_mode_and_revision_are_bound'),
    ('carrier-revocation', 'secret_resolver.py',
     '        await SecretResolver().check(self._handle,self._deadline)\n',
     '        pass  # control: skip current authority at handoff\n',
     'test_x304_revoked_before_carrier_handoff_refuses_but_cleanup_works'),
    ('carrier-permission', 'secret_resolver.py',
     'and type(state.mode) is int and state.mode == 0o600',
     'and type(state.mode) is int',
     'test_x304_bad_mode_identity_owner_links_or_mount_refuses_and_closes'),
    ('tls-policy', 'connections.py', "sslmode='verify-full'", "sslmode='require'",
     'test_x303_psycopg_exact_parameters_and_rc1_protocol'),
    ('session-hba', 'connections.py',
     'and state.tls is True and state.hba is True and state.idle is True',
     'and state.tls is True and state.idle is True',
     'test_x303_tls_hba_login_oid_database_session_faults_retire'),
    ('lost-connect', 'connections.py', '            elif connecting:\n',
     '            elif False:\n',
     'test_x310_lost_connect_result_reconciles_reserved_attempt_or_hold'),
    ('failed-retire-callback', 'connections.py',
     '        handle.close()\n        await _retire_resolved',
     '        # control: leave callback handle open\n        await _retire_resolved',
     'test_x310_failed_retirement_fences_password_callback'),
    ('late-cleanup', 'secret_resolver.py',
     '        _deadline(deadline)  # a supplier may swallow cancellation; late success is never current\n',
     '', 'test_x310_cleanup_that_swallows_cancel_cannot_publish_late_success'),
    ('owner-bound', 'secret_resolver.py',
     'min(grant.expires,deadline)', 'grant.expires',
     'test_x302_operation_budget_does_not_reissue_or_shorten_owner_expiry'),
    ('supplier-log-privacy', 'secret_resolver.py',
     '        with private_driver_logs():\n            async with asyncio.timeout_at(deadline):\n                result = await awaitable',
     '        async with asyncio.timeout_at(deadline):\n            result = await awaitable',
     'test_x305_supplier_read_policy_and_retire_logs_are_private'),
    ('reservation-fence', 'connections.py',
     '                await resolver.check(handle,deadline)\n', '',
     'test_x308_revocation_during_reservation_dispatches_no_driver'),
    ('current-db-policy', 'connections.py',
     '_same(getattr(current,key),getattr(policy,key))', 'True',
     'test_x307_current_role_binding_change_withholds_and_retires'),
    ('asyncpg-strict-tls', 'connections.py',
     "    secrets._require(type(context) is ssl.SSLContext and context.check_hostname\n"
     "        and context.verify_mode == ssl.CERT_REQUIRED\n"
     "        and context.hostname_checks_common_name is False,'invalid_connection_policy')",
     '    return  # control: trust insecure TLS context',
     'test_x303_insecure_asyncpg_context_never_dispatches'),
    ('carrier-reservation', 'secret_resolver.py',
     '        carrier._valid(state,present=False)\n', '',
     'test_x304_invalid_original_reservation_dispatches_no_material_or_cleanup'),
]

HARNESS = """
import json,sys,unittest
sys.path.insert(0,'next/tests')
from test_receipts import AssertionResult,MARKER
import test_x3_secret_resolver
names=sys.argv[1:]
suite=(unittest.defaultTestLoader.loadTestsFromNames(
    ['test_x3_secret_resolver.X3.'+name for name in names]) if names else
    unittest.defaultTestLoader.loadTestsFromModule(test_x3_secret_resolver))
r=unittest.TextTestRunner(verbosity=2,resultclass=AssertionResult).run(suite)
print(MARKER+json.dumps(dict(tests_run=r.testsRun,failures=r.assertions,
    errors=[dict(id=t.id(),traceback=tb) for t,tb in r.errors],skips=len(r.skipped))))
sys.exit(0 if r.wasSuccessful() else 1)
"""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    os.chdir(ROOT)
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT/'next/tests'))
    from test_receipts import classify, report
    head = subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    paths = [ROOT/(SRC+name) for name in ('secret_resolver.py','connections.py')]
    originals = {p:p.read_bytes() for p in paths}
    test_before = (ROOT/TEST).read_bytes()
    packet = dict(head=head,python=sys.version,qualification='SOURCE_FIXTURES_ONLY',
                  test_sha256=digest(test_before),controls=[])
    try:
        for name, filename, before, after, test in CONTROLS:
            path = ROOT/(SRC+filename)
            source = originals[path].decode()
            expected = 2 if name == 'carrier-revocation' else 1
            assert source.count(before) == expected, (name,'mutation anchor changed')
            mutant = source.replace(before,after).encode()
            path.write_bytes(mutant)
            result = subprocess.run([sys.executable,'-B','-c',HARNESS,test],
                text=True,capture_output=True,timeout=10,
                env={**os.environ,'PYTHONPATH':str(ROOT/'next/src')})
            (out/(name+'.txt')).write_text(result.stdout+result.stderr)
            status = classify(result,{'test_x3_secret_resolver.X3.'+test})
            value = report(result)
            packet['controls'].append(dict(name=name,test=test,status=status,
                path=SRC+filename,before_sha256=digest(originals[path]),
                mutant_sha256=digest(mutant),exit=result.returncode,outcome=value))
            path.write_bytes(originals[path])
            assert path.read_bytes() == originals[path]
            print(name+': '+status,flush=True)
    finally:
        for path, original in originals.items():path.write_bytes(original)
        assert (ROOT/TEST).read_bytes() == test_before, 'test bytes changed'
        packet['restored'] = all(p.read_bytes()==v for p,v in originals.items())
        (out/'controls.json').write_text(json.dumps(packet,indent=2)+'\n')
    result = subprocess.run([sys.executable,'-B','-c',HARNESS],
        text=True,capture_output=True,timeout=15,
        env={**os.environ,'PYTHONPATH':str(ROOT/'next/src')})
    (out/'restored.txt').write_text(result.stdout+result.stderr)
    packet['restored_suite'] = report(result)
    packet['ok'] = (packet['restored'] and result.returncode==0
        and all(row['status']=='killed' for row in packet['controls']))
    (out/'controls.json').write_text(json.dumps(packet,indent=2)+'\n')
    return 0 if packet['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
