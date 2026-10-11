"""Real-PG C04 mutations, attributed only to declared test-body assertions."""
import hashlib
import json
import os
from pathlib import Path
import sys

import psycopg

NEXT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NEXT / 'tests'))
from test_receipts import classify, report, suite

AUTH = 'src/cortex_core/auth.py'
SQL = 'schema/auth/002-isolation.sql'
MANIFEST = 'schema/manifest.json'
FIXTURE = 'contracts/auth-conformance.json'
MUTATOR = 'scripts/mutate_auth.py'
PREFIX = 'test_authorization.AuthorizationTests.'
PUBLIC = '\nGRANT EXECUTE ON FUNCTION auth.resolve_scope(text,uuid,uuid,text) TO PUBLIC;\n'
MUTATIONS = [
    (AUTH, 'privileged runtime accepted',
     'if not connection.execute(_ROLE_QUERY).fetchone()[0]:', 'if False:',
     'test_privileged_or_owning_runtime_connection_refused'),
    (AUTH, 'changed accepted context ignored',
     'if not _context_intact(connection, arguments):', 'if False:',
     'test_context_guards.ContextGuardTests.test_persistent_action_change_without_write_refuses_acceptance'),
    (AUTH, 'expiry before acceptance ignored',
     'if _resolve(connection, arguments) != scope:', 'if False:',
     'test_expiry_before_response_refuses_and_rolls_back'),
    (AUTH, 'session scope after request retained',
     'finally:\n        try:\n            _clear(connection)',
     'finally:\n        try:\n            pass',
     'test_context_guards.ContextGuardTests.test_all_session_scope_fields_clear_on_commit_and_rollback'),
    (AUTH, 'generation omitted from cache partition',
     'self.principal_id, self.permission_generation, self.action)',
     'self.principal_id, 0, self.action)',
     'test_permission_generation_and_cache_partition_track_authority'),
    (SQL, 'owner bypass enabled', 'FORCE ROW LEVEL SECURITY', 'NO FORCE ROW LEVEL SECURITY',
     'test_owner_force_rls_does_not_depend_on_api_role_guard'),
    (SQL, 'tenant comparison omitted', 'resolved.tenant_id<>p_tenant', 'false',
     'test_cross_tenant_reads_and_insert_update_are_denied'),
    (SQL, 'project comparison omitted', 'resolved.project_id<>p_project', 'false',
     'test_other_project_in_same_tenant_is_not_visible_or_writable'),
    (SQL, 'idempotency principal comparison omitted',
     '(p_principal IS NOT NULL AND resolved.principal_id<>p_principal)', 'false',
     'test_idempotency_receipts_are_principal_scoped'),
    (SQL, 'control-only grant elevated to owner',
     "OR (p_action='write' AND 'write'=ANY(g.permissions))",
     "OR (p_action='write' AND 'write'=ANY(g.permissions)) OR (p_action='control' AND 'control'=ANY(g.permissions))",
     'test_control_requires_scoped_owner_or_admin_without_fallback'),
    (SQL, 'revocation authority locks omitted', 'FOR SHARE OF c,p,t,i,pr,g,gen', '',
     'test_revocation_linearizes_with_accepted_request'),
    (SQL, 'permission generation never advances', 'generation=gen.generation+1', 'generation=gen.generation',
     'test_principal_disable_invalidates_permission_generation'),
    (SQL, 'private verifier function exposed to public', None, PUBLIC,
     'test_all_business_tables_force_rls_private_functions_stay_private'),
    (MANIFEST, 'manifest dispatches exposed auth migration', None, None,
     'test_all_business_tables_force_rls_private_functions_stay_private'),
    (FIXTURE, 'wrong-installation negative fixture accepts authorized scope', None, None,
     'test_shared_negative_conformance_cases_are_refused'),
    (MUTATOR, 'exit-code-only consumer accepts inconclusive receipts',
     'def mutation_status(result, expected):\n    return classify(result, expected)',
     "def mutation_status(result, expected):\n    return 'killed' if result.returncode else 'survived'",
     'test_auth_mutation_receipts.AuthMutationReceipts.test_operational_and_fixture_failures_are_inconclusive'),
    (SQL, 'accepted tuple guard omitted',
     "IF NOT auth.context_is_bound(current_setting('cortex.credential_digest',true),\n        NULLIF(current_setting('cortex.installation_id',true),'')::uuid,\n        NULLIF(current_setting('cortex.project_id',true),'')::uuid,configured_action) THEN RETURN false; END IF;",
     '',
     'test_transaction_binding.TransactionBindingTests.test_temporary_action_elevation_restored_before_exit_cannot_commit'),
    (SQL, 'transaction once-only marker guard omitted',
     "IF EXISTS(SELECT 1 FROM pg_catalog.pg_locks WHERE locktype='advisory'",
     "IF false AND EXISTS(SELECT 1 FROM pg_catalog.pg_locks WHERE locktype='advisory'",
     'test_private_binding.PrivateBindingTests.test_discard_temp_and_session_unlock_cannot_rebind'),
    (SQL, 'caller-owned context accepted',
     'AND c.relowner=(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user)', '',
     'test_private_binding.PrivateBindingTests.test_caller_owned_context_even_with_verifier_grants_is_refused'),
    (SQL, 'NULL resolver action accepted', 'p_action IS NULL OR ', '',
     'test_transaction_binding.TransactionBindingTests.test_null_action_resolver_refuses_owner_scope'),
    (SQL, 'NULL policy targets accepted', 'p_tenant IS NULL OR p_project IS NULL OR ', '',
     'test_private_binding.PrivateBindingTests.test_null_policy_targets_and_need_are_refused'),
    (SQL, 'NULL policy need accepted', 'p_need IS NULL OR ', '',
     'test_private_binding.PrivateBindingTests.test_null_policy_targets_and_need_are_refused'),
]


def mutation_status(result, expected):
    return classify(result, expected)


def checkpoint():
    with psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True) as connection:
        connection.execute('CHECKPOINT')


def capture(result):
    return {'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr,
            'receipt': report(result),
            'stdout_sha256': hashlib.sha256(result.stdout.encode()).hexdigest(),
            'stderr_sha256': hashlib.sha256(result.stderr.encode()).hexdigest()}


def run():
    originals = {relative: (NEXT / relative).read_bytes()
                 for relative in (AUTH, SQL, MANIFEST, FIXTURE, MUTATOR)}
    original_hashes = {relative: hashlib.sha256(data).hexdigest()
                       for relative, data in originals.items()}
    baseline = suite(NEXT / 'tests/auth')
    print(json.dumps({'baseline': capture(baseline)}), flush=True)
    if mutation_status(baseline, set()) != 'survived':
        raise SystemExit('Auth mutation baseline is RED')
    rows = []
    auxiliary = NEXT / 'schema/auth/mutation-isolation.sql'
    assert not auxiliary.exists()
    try:
        for relative, label, before, after, target in MUTATIONS:
            checkpoint()
            path = NEXT / relative
            original = originals[relative]
            source = original.decode()
            if before is not None:
                assert source.count(before) == 1, label
                changed = source.replace(before, after, 1).encode()
            elif relative == SQL:
                changed = original + PUBLIC.encode()
            elif relative == MANIFEST:
                alternative = originals[SQL] + PUBLIC.encode()
                auxiliary.write_bytes(alternative)
                manifest = json.loads(original)
                entry = next(row for row in manifest['migrations'] if row['id'] == 'auth-0002')
                entry.update(file='auth/mutation-isolation.sql', sha256=hashlib.sha256(alternative).hexdigest())
                changed = (json.dumps(manifest, indent=2) + '\n').encode()
            else:
                fixture = json.loads(original)
                entry = next(row for row in fixture['cases'] if row['id'] == 'wrong_installation')
                assert entry['installation'] == 20
                entry['installation'] = 1
                changed = (json.dumps(fixture, indent=2) + '\n').encode()
            try:
                path.write_bytes(changed)
                if relative == SQL:
                    manifest = json.loads(originals[MANIFEST])
                    entry = next(row for row in manifest['migrations'] if row['id'] == 'auth-0002')
                    entry['sha256'] = hashlib.sha256(changed).hexdigest()
                    (NEXT / MANIFEST).write_text(json.dumps(manifest, indent=2) + '\n')
                result = suite(NEXT / 'tests/auth')
                expected = target if '.' in target else PREFIX + target
                status = mutation_status(result, {expected})
                row = {'source': relative, 'mutation': label,
                       'recipe': {'before': before, 'after': after,
                                  'manifest': 'alternate valid migration + matching digest' if relative == MANIFEST else None,
                                  'fixture': 'wrong_installation installation20->1' if relative == FIXTURE else None},
                       'expected_test': expected, 'status': status,
                       'original_sha256': hashlib.sha256(original).hexdigest(),
                       'mutated_sha256': hashlib.sha256(changed).hexdigest(),
                       'effective_source_sha256': {p: hashlib.sha256((NEXT / p).read_bytes()).hexdigest()
                                                   for p in originals}, **capture(result)}
                if auxiliary.exists():
                    row['auxiliary_source'] = str(auxiliary.relative_to(NEXT))
                    row['auxiliary_sha256'] = hashlib.sha256(auxiliary.read_bytes()).hexdigest()
                rows.append(row)
                print(json.dumps(row), flush=True)
            finally:
                for file, data in originals.items():
                    (NEXT / file).write_bytes(data)
                if auxiliary.exists():
                    auxiliary.unlink()
    finally:
        for relative, data in originals.items():
            (NEXT / relative).write_bytes(data)
        if auxiliary.exists():
            auxiliary.unlink()
    checkpoint()
    restored = suite(NEXT / 'tests/auth')
    final_hashes = {relative: hashlib.sha256((NEXT / relative).read_bytes()).hexdigest()
                    for relative in originals}
    summary = {'mutants': len(rows), 'killed': sum(row['status'] == 'killed' for row in rows),
               'survivors': [row['mutation'] for row in rows if row['status'] == 'survived'],
               'inconclusive': [row['mutation'] for row in rows if row['status'] == 'inconclusive'],
               'restored_source_sha256': final_hashes, 'restored': capture(restored)}
    print(json.dumps(summary), flush=True)
    if len(rows) != len(MUTATIONS) or any(row['status'] != 'killed' for row in rows):
        raise SystemExit('Auth semantic mutation proof is incomplete')
    if final_hashes != original_hashes or mutation_status(restored, set()) != 'survived':
        raise SystemExit('Auth source or baseline restoration failed')


if __name__ == '__main__':
    run()
