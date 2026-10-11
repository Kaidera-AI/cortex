"""C04a semantic faults in copied inputs; only declared BODY assertions qualify."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import psycopg

NEXT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NEXT/'tests'))
from test_receipts import classify, report, suite
IDENTITY = 'src/cortex_core/identity.py'
SQL = 'schema/auth/003-identity.sql'
MANIFEST = 'schema/manifest.json'
JOBS = 'src/cortex_core/coordination.py'
CONTRACT = 'contracts/identity-conformance.json'
DB_FIXTURE = 'tests/core_db_fixture.py'
CALLER = 'scripts/mutate_identity.py'
I = 'test_identity.IdentityTests.'
G = 'test_identity_guards.IdentityGuards.'
P = 'test_identity_coordination.IdentityCoordination.'
R = 'test_identity_receipts.IdentityReceipts.'
MUTATIONS = [
 (IDENTITY,'registration and rotation replay redeliver plaintext',[("None if raw['replayed'] else secret",'secret',2)],'auth_identity',I+'test_registration_replay_returns_no_plaintext_and_changed_request_refuses'),
 (IDENTITY,'human registration silently becomes agent',[("return self._registration('human', principal_id", "return self._registration('agent', principal_id",1)],'auth_identity',I+'test_fixed_human_registration_requires_owner_attestation'),
 (IDENTITY,'roles implicitly elevate permissions',[("roles = _roles(roles); permissions = _permissions(permissions)","roles = _roles(roles); permissions = ('owner',)",1)],'auth_identity',I+'test_owner_permission_never_infers_human_and_roles_never_infer_permission'),
 (IDENTITY,'fresh identity generation hidden',[("tuple(row[4]), row[5]) if row else None","tuple(row[4]), 0) if row else None",1)],'auth_identity',I+'test_role_change_increments_generation_and_lookup_is_fresh'),
 (IDENTITY,'unreleased donor pin admitted',[(" or pin.released is not True",'',1)],'auth_identity',I+'test_missing_or_unreleased_donor_pin_is_not_run_without_side_effects'),
 (IDENTITY,'approved adoption digest ignored',[("if approved_sha256 != digest:",'if False:',1)],'auth_identity',I+'test_changed_manifest_duplicates_ambiguous_ids_and_human_conversion_refuse'),
 (IDENTITY,'donor hash and registration authority substituted',[("if manifest['donor'] != {'released_sha': pin.released_sha, 'source_sha256': pin.source_sha256} or manifest['legacy_registration_authority'] != _AUTHORITY:",'if False:',1)],'auth_identity',I+'test_manifest_donor_and_scope_hashes_cannot_be_substituted'),
 (IDENTITY,'both final credential authority checks use initial snapshot',[("if not c.execute('SELECT auth.identity_binding_valid()').fetchone()[0]:",'if False:',1),("after = _resolve(c, arguments)",'after = before',1)],'auth_identity',I+'test_owner_expiry_after_actual_registration_rolls_back_before_plaintext_delivery'),
 (IDENTITY,'legacy lead role discarded',[("return self.register_agent(principal_id, name, (role,), permissions", "return self.register_agent(principal_id, name, ('ignored',), permissions",1)],'auth_identity',I+'test_legacy_add_agent_maps_kind_and_role_but_tightens_lead_to_owner_admin'),
 (IDENTITY,'final private binding survival check omitted',[("if not c.execute('SELECT auth.identity_binding_valid()').fetchone()[0]:",'if False:',1)],'identity_binding','test_identity_binding.IdentityBinding.test_private_binding_loss_after_actual_registration_refuses_and_rolls_back'),
 (SQL,'private final binding function accepts missing context',[("SELECT EXISTS(SELECT 1 FROM auth.identity_scope(true));",'SELECT true;',1)],'identity_binding','test_identity_binding.IdentityBinding.test_private_binding_loss_after_actual_adoption_refuses_and_rolls_back'),
 (SQL,'default collation replaces byte ordering',[( 'p_roles COLLATE "C"=ARRAY(SELECT DISTINCT r COLLATE "C" FROM unnest(p_roles) r ORDER BY r COLLATE "C")','p_roles=ARRAY(SELECT DISTINCT r FROM unnest(p_roles) r ORDER BY r)',1)],'identity_portability','test_identity_portability.IdentityPortability.test_python_role_order_is_valid_under_real_non_c_collation'),
 (SQL,'private coordination schema usage removed',[( 'GRANT USAGE ON SCHEMA coordination TO "kaidera-runtime-core-verifier";','',1)],'auth_identity_guards',G+'test_actual_registration_commits_without_private_resource_privilege_failure'),
 (SQL,'direct SQL adoption manifest digest ignored',[("OR p_manifest_sha IS DISTINCT FROM encode(sha256(convert_to(p_manifest,'UTF8')),'hex')",'',1)],'auth_identity_guards',G+'test_sql_adoption_digest_binds_exact_manifest_bytes'),
 (SQL,'direct SQL adoption scope ignored',[("IF manifest->>'installation_id' IS DISTINCT FROM s.installation_id::text\n       OR manifest->>'project_id' IS DISTINCT FROM s.project_id::text THEN RAISE EXCEPTION 'identity_scope_mismatch'; END IF;",'',1)],'auth_identity_guards',G+'test_sql_adoption_rechecks_private_installation_and_project_scope'),
 (SQL,'fixed agent SQL path attempts human registration',[("auth.identity_register('agent',p_target", "auth.identity_register('human',p_target",1)],'auth_identity_guards',G+'test_fixed_agent_path_ignores_caller_kind_context'),
 (SQL,'duplicate project roles accepted',[( 'SELECT DISTINCT r COLLATE "C" FROM unnest(p_roles)','SELECT r COLLATE "C" FROM unnest(p_roles)',1)],'auth_identity',I+'test_roles_are_normalized_bounded_and_sql_constraints_refuse_duplicates'),
 (SQL,'PUBLIC function execution retained',[("REVOKE ALL ON FUNCTION %s FROM PUBLIC,%I", "REVOKE ALL ON FUNCTION %s FROM %I",1)],'auth_identity',I+'test_private_lookup_mutators_view_and_public_acl_cannot_bypass_binding'),
 (SQL,'unadopted principal has identity routing authority',[("AND NOT p.disabled AND p.identity_adopted FOR SHARE OF p,g", "AND NOT p.disabled FOR SHARE OF p,g",1)],'auth_identity',I+'test_unadopted_existing_principals_have_no_role_or_human_authority'),
 (SQL,'project role membership leaks across projects',[(" AND g.project_id=s.project_id",'',1)],'identity_coordination',P+'test_role_membership_does_not_cross_project_scope'),
 (MANIFEST,'identity entry no longer binds actual SQL',[( '"id": "auth-0003"', '"id": "auth-0003-unbound"',1)],'identity_receipts',R+'test_manifest_binds_exact_additive_identity_sql'),
 (JOBS,'worker membership requirement omitted',[("elif role not in actor.roles:",'elif False:',1)],'identity_policy_faults','test_identity_policy_faults.IdentityPolicyFaults.test_wrong_adopted_role_cannot_create_a_claim_or_attempt'),
 (JOBS,'human kind check omitted',[("if actor.kind != 'human':",'if False:',1)],'identity_coordination',P+'test_human_review_uses_core_kind_not_owner_permissions_or_guc'),
 (JOBS,'historical replay skips current identity policy',[("if operation in ('job.claim'", "if saved is None and operation in ('job.claim'",1)],'identity_coordination',P+'test_role_withdrawal_refuses_current_worker_and_historical_claim_replay'),
 (JOBS,'persisted role and human policy silently discarded',[("recipient_role=recipient_role,human_review=human_review)","recipient_role=None,human_review=False)",2)],'identity_policy_faults','test_identity_policy_faults.IdentityPolicyFaults.test_policy_persistence_retains_exact_role_and_human_flag'),
 (JOBS,'optional policy omitted from request digest',[("arguments.append(dict(recipient_role=recipient_role,human_review=human_review))",'pass',1)],'identity_coordination',P+'test_owner_policy_is_persisted_visible_and_bound_to_create_retry'),
 (JOBS,'human kind bypasses independent holder review',[("if (current[2] == str(scope.principal_id)) == review:",'if False:',1)],'identity_coordination',P+'test_human_kind_never_bypasses_independent_review'),
 (JOBS,'default policy changes published C05 digest',[("if recipient_role is not None or human_review:",'if True:',1)],'identity_transition','test_identity_transition.IdentityTransition.test_policy_free_create_keeps_c05_request_digest_and_replay'),
 (CONTRACT,'roles asserted to grant permissions',[( '"roles_imply_permissions": false','"roles_imply_permissions": true',1)],'identity_receipts',R+'test_identity_contract_keeps_authority_and_admission_boundaries'),
 (CONTRACT,'real released adoption falsely qualified',[( '"released_donor_adoption": "NOT_RUN_until_released_pin"','"released_donor_adoption": "QUALIFIED"',1)],'identity_receipts',R+'test_identity_contract_keeps_authority_and_admission_boundaries'),
 (DB_FIXTURE,'fixture read principal elevated to owner',[( 'READ_A=auth_fixture.READ_A','READ_A=auth_fixture.OWNER_A',1)],'auth_identity',I+'test_read_write_control_only_and_wrong_scope_cannot_register'),
 (CALLER,'caller accepts nonzero exit without body attribution',[("def mutation_status(result, expected):\n    return classify(result, expected)","def mutation_status(result, expected):\n    return 'killed' if result.returncode else 'survived'",1)],'identity_receipts',R+'test_caller_refuses_signals_fixtures_wrong_body_and_operational_errors'),
]


def mutation_status(result, expected):
    return classify(result, expected)


def capture(result):
    # AssertionError can print a newly issued bearer. Scrub before retaining any
    # output/receipt/hash; no pre-redaction trace or hash is written.
    scrub = lambda text: re.sub(r"b(['\"])[A-Za-z0-9_-]{43}\1", "b'<redacted-bearer>'", text)
    safe = SimpleNamespace(returncode=result.returncode, stdout=scrub(result.stdout), stderr=scrub(result.stderr))
    return dict(exit_code=safe.returncode, stdout=safe.stdout, stderr=safe.stderr, receipt=report(safe),
        stdout_sha256=hashlib.sha256(safe.stdout.encode()).hexdigest(), stderr_sha256=hashlib.sha256(safe.stderr.encode()).hexdigest(),
        redaction='opaque bearer byte repr removed before retention')


def checkpoint():
    with psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True) as connection:
        connection.execute('CHECKPOINT')


def run():
    paths = (IDENTITY, SQL, MANIFEST, JOBS, CONTRACT, DB_FIXTURE, CALLER)
    originals = {p:(NEXT/p).read_bytes() for p in paths}
    original_hashes = {p:hashlib.sha256(b).hexdigest() for p,b in originals.items()}
    directories = ('auth_identity','auth_identity_guards','identity_coordination','identity_portability','identity_receipts','identity_transition','identity_policy_faults','identity_binding')
    baseline = {p:capture(suite(NEXT/'tests'/p)) for p in directories}
    print(json.dumps({'baseline':baseline}), flush=True)
    if any(r['exit_code']!=0 or r['receipt'] is None or r['receipt']['errors'] or r['receipt']['failures'] for r in baseline.values()):
        raise SystemExit('C04a mutation baseline is RED')
    rows = []
    try:
        for relative, label, edits, directory, target in MUTATIONS:
            checkpoint()
            source = originals[relative].decode()
            for before, after, count in edits:
                assert source.count(before)==count, (label, before, count, source.count(before))
                source = source.replace(before, after)
            changed = source.encode()
            try:
                (NEXT/relative).write_bytes(changed)
                if relative==SQL:
                    # Rebind copied loader integrity so this is a SQL semantic
                    # fault, rather than an artifact/import failure.
                    manifest = json.loads(originals[MANIFEST])
                    for entry in manifest['migrations']:
                        if entry['id']=='auth-0003':
                            entry['sha256']=hashlib.sha256(changed).hexdigest()
                    (NEXT/MANIFEST).write_text(json.dumps(manifest,indent=2)+'\n')
                result = suite(NEXT/'tests'/directory)
                row = dict(source=relative, mutation=label, recipe=edits, expected_test=target,
                    status=mutation_status(result,{target}), original_sha256=original_hashes[relative],
                    mutated_sha256=hashlib.sha256(changed).hexdigest(),
                    effective_source_sha256={p:hashlib.sha256((NEXT/p).read_bytes()).hexdigest() for p in paths}, **capture(result))
                rows.append(row); print(json.dumps(row),flush=True)
            finally:
                for p,b in originals.items():
                    (NEXT/p).write_bytes(b)
    finally:
        for p,b in originals.items():
            (NEXT/p).write_bytes(b)
    checkpoint()
    restored = {p:capture(suite(NEXT/'tests'/p)) for p in directories}
    final_hashes = {p:hashlib.sha256((NEXT/p).read_bytes()).hexdigest() for p in paths}
    summary = dict(mutants=len(rows), killed=sum(r['status']=='killed' for r in rows),
        survivors=[r['mutation'] for r in rows if r['status']=='survived'],
        inconclusive=[r['mutation'] for r in rows if r['status']=='inconclusive'],
        restored_source_sha256=final_hashes, restored=restored)
    print(json.dumps(summary),flush=True)
    if len(rows)!=len(MUTATIONS) or any(r['status']!='killed' for r in rows):
        raise SystemExit('C04a mutation qualification incomplete')
    if final_hashes!=original_hashes or any(r['exit_code']!=0 or r['receipt'] is None or r['receipt']['errors'] or r['receipt']['failures'] for r in restored.values()):
        raise SystemExit('C04a full baseline/source restoration failed')


if __name__=='__main__':
    run()
