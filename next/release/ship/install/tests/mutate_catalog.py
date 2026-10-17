"""Causal controls for the J06 sole catalog validator and its closure package: each mutant of roles.py / package_closure.py must be killed by an assertion of
the UNCHANGED test_catalog.py / test_package.py; healthy and restored pass. Run: python3 mutate_catalog.py [out.json]  (temp copy; the source is untouched)."""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
INSTALL = HERE.parent
M = [
    ('phase_check_removed', 'roles.py', "    if phase not in ('seed', 'serve'):\n        raise RoleRefusal('catalog_phase')\n", "    pass\n"),
    ('config_mode_check_removed', 'roles.py', "        if config.mode != phase:\n            raise RoleRefusal('catalog_config')\n", '        pass\n'),
    ('database_name_check_removed', 'roles.py', "    if db['name'] != db_name:\n        raise RoleRefusal('catalog_database')\n", ''),
    ('session_login_check_removed', 'roles.py', "sess['login'] != logins[actor] or ", ''),
    ('session_oid_check_removed', 'roles.py', " or sess['oid'] != roles[actor]['oid']", ''),
    ('session_database_check_removed', 'roles.py', " or sess['database_oid'] != db['oid']:", ':'),
    ('role_login_check_removed', 'roles.py', "any(roles[p]['login'] != logins[p] for p in A_PURPOSES) or ", ''),
    ('role_oid_uniqueness_removed', 'roles.py', " or len(set(oids)) != len(oids)", ''),
    ('flags_check_removed', 'roles.py', "        if roles[p]['flags'] != expected_flags:\n            raise RoleRefusal(f'catalog_flags:{p}')\n", "        pass\n"),
    ('group_flags_check_removed', 'roles.py', " or any(groups[g]['flags'].values())", ''),
    ('group_memberships_check_removed', 'roles.py', " or groups[g]['memberships']:", ':'),
    ('membership_ignores_foreign_edges', 'roles.py', "if sorted((e['member'], e['oid'], e['set'], e['inherit'], e['admin']) for e in edges) != expected:", "if sorted((e['member'], e['oid'], e['set'], e['inherit'], e['admin']) for e in edges if e['member'] in {roles[p]['oid'] for p in A_PURPOSES}) != expected:"),
    ('membership_ignores_options', 'roles.py', "if sorted((e['member'], e['oid'], e['set'], e['inherit'], e['admin']) for e in edges) != expected:", "if sorted((e['member'], e['oid'], True, False, False) for e in edges) != expected:"),
    ('own_membership_consistency_removed', 'roles.py', "        if own != sorted((e['oid'], e['set'], e['inherit'], e['admin']) for e in edges if e['member'] == roles[p]['oid']):\n            raise RoleRefusal('catalog_membership')\n", ''),
    ('ownership_removed', 'roles.py', "    if db['owner_oid'] in service or any(o['owner_oid'] in service for o in owners):\n        raise RoleRefusal('catalog_ownership')\n", ''),
    ('database_owner_may_be_service', 'roles.py', "if db['owner_oid'] in service or any(", "if any("),
    ('binding_revision_unchecked', 'roles.py', "or b['revision'] != INTENDED_REVISION[p] ", ''),
    ('binding_partial_allowed', 'roles.py', "        if set(seen) != set(BOUND) or len({b['operation_id'] for b in bindings}) != 1:", "        if len({b['operation_id'] for b in bindings}) != 1:"),
    ('binding_operation_unchecked', 'roles.py', " or len({b['operation_id'] for b in bindings}) != 1:", ':'),
    ('binding_active_unchecked', 'roles.py', "or not b['active'] ", ''),
    ('binding_logical_role_unchecked', 'roles.py', "or b['logical_role'] != p ", ''),
    ('binding_installation_unchecked', 'roles.py', "or b['installation_id'] != installation\n                    ", ''),
    ('binding_embedding_unchecked', 'roles.py', " or b['embedding_job_kind'] is not None):", "):"),
    ('snapshot_not_detached', 'roles.py', "    return copy.deepcopy({'phase': phase,", "    return ({'phase': phase,"),
    ('package_misses_contract', 'package_closure.py', "'cortex_ship/install/runtime-operations-v1.json': CONTRACT.read_bytes()}", "'cortex_ship/install/runtime-operations-v1.json': b'{}'}"),
    ('package_falls_back_to_tree_path', 'roles.py', "if __name__.startswith('cortex_ship.'):", "if False:"),
    ('receipt_omits_digest', 'package_closure.py', "'source_roles_sha256': sha(files['cortex_ship/install/roles.py']),", "'source_roles_sha256': '0',"),
    ('serve_session_is_bootstrap', 'roles.py', "actor = 'api_request' if serve else 'bootstrap'", "actor = 'bootstrap'"),
    ('serve_finite_login_allowed', 'roles.py', "            expected_flags['login'] = policy[p]['lifetime'] == 'service'  # finite logins are closed by serve time\n", "            pass\n"),
    ('serve_bindings_list_allowed', 'roles.py', "(obs['bindings'] is None if serve else type(obs['bindings']) is list)", "(obs['bindings'] is None or type(obs['bindings']) is list)"),
    ('serve_installation_mismatch_allowed', 'roles.py', " or not svc['installation_matches']:", ":"),
    ('serve_readiness_enum_unchecked', 'roles.py', "svc['enrollment_readiness'] not in SERVICE_READINESS or ", ""),
    ('serve_config_mode_unchecked', 'roles.py', "        if config.mode != phase:\n", "        if False:\n"),
    ('serve_service_block_not_required', 'roles.py', " + (('service',) if serve else ())", ""),
    ('ledger_usage_to_public', 'roles.py', "f'GRANT USAGE ON SCHEMA core TO {api};'", "'GRANT USAGE ON SCHEMA core TO PUBLIC;'"),
    ('ledger_select_to_request_group', 'roles.py', "f'GRANT SELECT ON core.schema_migrations TO {api};'", "'GRANT SELECT ON core.schema_migrations TO \"kaidera-runtime-core-request\";'"),
    ('ledger_without_usage', 'roles.py', "f'GRANT USAGE ON SCHEMA core TO {api};', ", ""),
    ('ledger_not_transactional', 'roles.py', "'BEGIN;', *steps, 'COMMIT;', ''", "*steps, ''"),
    ('ledger_grants_probe_too', 'roles.py', "f'GRANT SELECT ON core.schema_migrations TO {api};']", "f'GRANT SELECT ON core.schema_migrations TO {api};', f'GRANT SELECT ON core.schema_migrations TO {_q(b.logins[\"probe\"])};']"),
    ('pinned_key_one_character_off', 'install.sh', "PINNED_PUBKEY='RWQuhegMfku7e4Rl", "PINNED_PUBKEY='RWQuhegMfku7e4R1"),
    ('pinned_key_id_dropped', 'install.sh', "# release key 7BBB4B7E0CE8852E", "# release key"),
    ('provision_clear_step_removed', 'roles.py', "                    (f\"SELECT set_config('{PROVISION_SETTING}', '', true)\", None)]", "                    ]"),
    ('provision_length_check_removed', 'roles.py', "  IF NOT (length(pw) >= {MIN_SECRET}) THEN RAISE EXCEPTION 'secret_missing_or_short:{login}' USING ERRCODE = '22023'; END IF;\\n\"\n", "\"\n"),
    ('provision_secret_bound_to_wrong_purpose', 'roles.py', "(f\"SELECT set_config('{PROVISION_SETTING}', %s, true)\", p),", "(f\"SELECT set_config('{PROVISION_SETTING}', %s, true)\", 'probe'),"),
    ('provision_order_not_finite_first', 'roles.py', "for p in ('migrator', 'installer_enrollment', 'api_request', 'probe'):\n            login", "for p in ('probe', 'api_request', 'installer_enrollment', 'migrator'):\n            login"),
    ('provision_close_without_ack', 'roles.py', "    if phase == 'close':\n        if seed_ack is not True:\n            raise RoleRefusal('seed_not_acknowledged')\n", "    if phase == 'close':\n"),
    ('provision_unknown_phase_accepted', 'roles.py', "    raise RoleRefusal('provision_phase')", "    return []"),
    ('script_wrapper_changed', 'roles.py', "return '\\n'.join(['\\\\set ON_ERROR_STOP on', 'BEGIN;', *steps, 'COMMIT;', ''])", "return '\\n'.join(['\\\\set ON_ERROR_STOP on', 'BEGIN;', 'SET LOCAL x.y = 1;', *steps, 'COMMIT;', ''])"),
    ('post_steps_drop_audit', 'roles.py', "+ ['\\n'.join(audit)]", ""),
    ('close_steps_skip_terminate', 'roles.py', "                f\"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{login}' AND pid <> pg_backend_pid();\"]", "                ]"),
]
def run(tree):
    r = subprocess.run([sys.executable, '-m', 'unittest', 'test_catalog', 'test_package', 'test_provision_steps'], cwd=tree / 'tests', capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    fails = [l.split(' (')[0] for l in r.stderr.splitlines() if l.startswith(('FAIL:', 'ERROR:'))]
    return r.returncode, fails, (r.stderr.strip().splitlines() or [''])[-1]
def main():
    res = [dict(case='healthy', phase='healthy', **dict(zip(('exit', 'failures', 'tail'), run(INSTALL))))]
    for name, rel, old, new in M:
        src = (INSTALL / rel).read_text(); assert src.count(old) == 1, (name, src.count(old))
        tmp = Path(tempfile.mkdtemp(prefix='nix-mut-')); deep = tmp / 'x/next/release/ship/install'; deep.parent.mkdir(parents=True)
        shutil.copytree(INSTALL, deep, ignore=shutil.ignore_patterns('__pycache__'))
        for d in ('contracts', 'src', 'schema'): os.symlink(INSTALL.parents[2] / d, deep.parents[2] / d)
        (deep / rel).write_text(src.replace(old, new, 1))
        code, fails, tail = run(deep)
        res.append(dict(case=name, phase='mutant', exit=code, failures=fails, tail=tail, killed=code != 0 and any(f.startswith('FAIL') for f in fails)))
        shutil.rmtree(tmp, ignore_errors=True)
    res.append(dict(case='restored', phase='restored', **dict(zip(('exit', 'failures', 'tail'), run(INSTALL)))))
    if len(sys.argv) > 1: Path(sys.argv[1]).write_text(json.dumps(res, indent=1) + '\n')
    for r in res: print(r['case'], r['phase'], r['exit'], ('KILLED by ' + r['failures'][0][6:60]) if r.get('killed') else ('SURVIVED' if r['phase'] == 'mutant' else ''))
    ok = res[0]['exit'] == 0 and res[-1]['exit'] == 0 and all(r['killed'] for r in res if r['phase'] == 'mutant')
    print('ALL MUTANTS KILLED, healthy and restored pass' if ok else 'CAUSAL CONTROL FAILED'); sys.exit(0 if ok else 1)
main()
