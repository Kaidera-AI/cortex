"""J06 sole role-catalog validator: roles.validate_catalog(config, observation, phase='seed') -> detached admitted dict, or RoleRefusal (typed reason).
Pure: the observation is a plain dict of the shape Cox's catalog.py returns (J06-catalog-observation-v1); the config is the REAL Core carrier (seed view, Cox edc8cac3).
A source fixture here is NOT production authority: only Cox's own read-only observer feeds the validator in production."""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[3] / 'src'))
sys.path.insert(0, str(HERE))
import roles as R  # noqa: E402
from cortex_core.runtime.bootstrap_config import read_config_file  # noqa: E402
from _fixtures import FIVE, INSTALLER, LOGINS, REQUEST_GROUP  # noqa: E402

VERIFIER_GROUP = 'kaidera-runtime-core-verifier'
OID = {'bootstrap': 16401, 'migrator': 16402, 'api_request': 16403, 'probe': 16404, 'installer_enrollment': 16405}
GROUP_OID = {REQUEST_GROUP: 16410, VERIFIER_GROUP: 16411}
DB_OID = 16384
INTENT = {'bootstrap': 0, 'migrator': 0, 'api_request': 1, 'probe': 1, 'installer_enrollment': 1}
OP = str(uuid.uuid4())


class Base(unittest.TestCase):
    def setUp(self):
        self.vault = Path(os.path.realpath(tempfile.mkdtemp(prefix='nix-cat-')))
        self.vault.chmod(0o700)
        (self.vault / 'config').mkdir(mode=0o700)
        ids = [str(uuid.uuid4()) for _ in range(3)]
        cfg = {'version': 1, 'installation_id': ids[0], 'tenant_id': ids[1], 'project_id': ids[2], 'project_names': {'main': ids[2]},
               'ai_mode': 'off', 'vault_root': str(self.vault), 'bind': {'host': '0.0.0.0', 'port': 8500}, 'publish': {'host': '127.0.0.1', 'port': 8500},
               'client': {'credential_ref': 'secrets/cortex/client'},
               'database': {'host': '10.0.0.5', 'port': 5432, 'name': 'cortex', 'tls': {'mode': 'verify-full', 'root_cert_ref': 'secrets/cortex/root'}},
               'roles': {p: {'login': LOGINS[p], 'secret_ref': f'secrets/cortex/{p}'} for p in FIVE},
               'limits': {'request_seconds': 30, 'shutdown_seconds': 30}}
        path = self.vault / 'config/cortex.json'
        path.write_text(json.dumps(cfg))
        path.chmod(0o600)
        self.cfg = read_config_file(path, mode='seed')
        self.policy = R.load_roles()
        self.addCleanup(shutil.rmtree, self.vault, True)
        self.assertTrue(hasattr(R, 'validate_catalog'), 'roles.validate_catalog is the sole role-catalog validator (J06)')

    def flags(self, purpose):
        s = self.policy[purpose]
        return {k: s[k] for k in ('login', 'superuser', 'createdb', 'createrole', 'inherit', 'replication', 'bypassrls')}

    def group(self, name):
        return {'login': name, 'oid': GROUP_OID[name], 'flags': dict(login=False, superuser=False, createdb=False, createrole=False, inherit=False, replication=False, bypassrls=False), 'memberships': []}

    def healthy(self, bound=False):
        edge = {'member': OID['api_request'], 'oid': GROUP_OID[REQUEST_GROUP], 'set': True, 'inherit': False, 'admin': False}
        roles = {p: {'login': LOGINS[p], 'oid': OID[p], 'flags': self.flags(p), 'memberships': []} for p in FIVE}
        roles['api_request']['memberships'] = [{k: edge[k] for k in ('oid', 'set', 'inherit', 'admin')}]
        rows = []
        if bound:
            for p in ('api_request', 'probe', 'installer_enrollment'):
                rows.append({'role_oid': OID[p], 'role_name': LOGINS[p], 'logical_role': p, 'installation_id': self.cfg.installation_id, 'active': True,
                             'revision': 1, 'operation_id': OP, 'database_oid': DB_OID, 'database_owner_oid': OID['bootstrap'], 'embedding_job_kind': None})
        return {'database': {'name': 'cortex', 'oid': DB_OID, 'owner_oid': OID['bootstrap']},
                'session': {'login': LOGINS['bootstrap'], 'oid': OID['bootstrap'], 'database_oid': DB_OID},
                'roles': roles, 'groups': {n: self.group(n) for n in (REQUEST_GROUP, VERIFIER_GROUP)}, 'memberships': [edge], 'bindings': rows,
                'protected_owners': [{'kind': 'schema', 'schema': 'auth', 'name': 'auth', 'owner_oid': GROUP_OID[VERIFIER_GROUP]},
                                     {'kind': 'relation', 'schema': 'auth', 'name': 'runtime_login_bindings', 'owner_oid': GROUP_OID[VERIFIER_GROUP]},
                                     {'kind': 'relation', 'schema': 'core', 'name': 'installations', 'owner_oid': OID['migrator']}]}

    def refused(self, obs, reason, **kw):
        with self.assertRaises(R.RoleRefusal) as e:
            R.validate_catalog(self.cfg, obs, **kw)
        self.assertEqual(e.exception.reason, reason)


class Admitted(Base):
    def test_a_fresh_catalog_is_admitted_with_the_explicit_revision_intent_and_absent_bindings(self):
        out = R.validate_catalog(self.cfg, self.healthy())
        self.assertEqual({p: out['roles'][p]['intended_revision'] for p in FIVE}, INTENT)
        self.assertEqual((out['bindings']['state'], out['bindings']['rows']), ('absent', []))
        self.assertEqual({p: out['roles'][p]['oid'] for p in FIVE}, OID)
        self.assertEqual(out['database'], {'name': 'cortex', 'oid': DB_OID, 'owner_oid': OID['bootstrap']})

    def test_an_exact_replay_with_three_revision_one_bindings_is_admitted_as_bound(self):
        out = R.validate_catalog(self.cfg, self.healthy(bound=True))
        self.assertEqual(out['bindings']['state'], 'bound')
        self.assertEqual(sorted(r['logical_role'] for r in out['bindings']['rows']), ['api_request', 'installer_enrollment', 'probe'])

    def test_the_admitted_snapshot_is_detached_from_the_observation(self):
        obs = self.healthy(bound=True)
        out = R.validate_catalog(self.cfg, obs)
        out['roles']['api_request']['flags']['superuser'] = True
        out['bindings']['rows'][0]['revision'] = 9
        out['database']['oid'] = 1
        self.assertFalse(obs['roles']['api_request']['flags']['superuser'])
        self.assertEqual(obs['bindings'][0]['revision'], 1)
        self.assertEqual(obs['database']['oid'], DB_OID)

    def test_the_flags_come_from_the_one_existing_policy(self):
        out = R.validate_catalog(self.cfg, self.healthy())
        for p in FIVE:
            self.assertEqual(out['roles'][p]['flags'], self.flags(p))


class Refused(Base):
    def edit(self, fn, reason, bound=False, **kw):
        obs = self.healthy(bound)
        fn(obs)
        self.refused(obs, reason, **kw)

    def test_only_the_seed_and_serve_phases_exist(self):
        self.refused(self.healthy(), 'catalog_phase', phase='status')
        self.refused(self.healthy(), 'catalog_phase', phase='migrate')

    def test_the_config_must_be_the_seed_view(self):
        cfg = copy.copy(self.cfg)
        object.__setattr__(cfg, 'mode', 'serve')
        with self.assertRaises(R.RoleRefusal) as e:
            R.validate_catalog(cfg, self.healthy())
        self.assertEqual(e.exception.reason, 'catalog_config')

    def test_missing_extra_or_mistyped_fields_are_refused(self):
        self.edit(lambda o: o.pop('groups'), 'catalog_shape')
        self.edit(lambda o: o.update(extra=1), 'catalog_shape')
        self.edit(lambda o: o['database'].update(oid=True), 'catalog_shape')
        self.edit(lambda o: o['roles']['probe']['flags'].update(login=1), 'catalog_shape')
        self.edit(lambda o: o.update(bindings=None), 'catalog_shape')

    def test_a_different_database_is_refused(self):
        self.edit(lambda o: o['database'].update(name='other'), 'catalog_database')
        self.edit(lambda o: o['session'].update(database_oid=1), 'catalog_session')

    def test_the_session_must_be_the_configured_bootstrap_login_by_name_and_oid(self):
        self.edit(lambda o: o['session'].update(login=LOGINS['migrator']), 'catalog_session')
        self.edit(lambda o: o['session'].update(oid=OID['migrator']), 'catalog_session')

    def test_the_five_roles_must_match_the_configured_logins_exactly(self):
        self.edit(lambda o: o['roles'].pop('probe'), 'catalog_roles')
        self.edit(lambda o: o['roles'].update(supervisor=copy.deepcopy(o['roles']['probe'])), 'catalog_roles')
        self.edit(lambda o: o['roles']['probe'].update(login='someone_else'), 'catalog_roles')
        self.edit(lambda o: o['roles']['probe'].update(oid=OID['api_request']), 'catalog_roles')

    def test_every_role_flag_must_equal_the_policy(self):
        for purpose in FIVE:
            for flag in ('superuser', 'createdb', 'createrole', 'replication', 'bypassrls', 'inherit', 'login'):
                self.edit(lambda o: o['roles'][purpose]['flags'].update({flag: not o['roles'][purpose]['flags'][flag]}), f'catalog_flags:{purpose}')

    def test_the_two_groups_must_exist_and_be_unprivileged_nologin(self):
        self.edit(lambda o: o['groups'].pop(REQUEST_GROUP), 'catalog_groups')
        self.edit(lambda o: o['groups'][VERIFIER_GROUP]['flags'].update(login=True), 'catalog_groups')
        self.edit(lambda o: o['groups'][REQUEST_GROUP]['flags'].update(superuser=True), 'catalog_groups')
        self.edit(lambda o: o['groups'][REQUEST_GROUP]['memberships'].append({'oid': 1, 'set': True, 'inherit': False, 'admin': False}), 'catalog_groups')

    def test_only_the_one_set_only_membership_is_admitted_including_foreign_incoming_edges(self):
        edge = lambda m, r, s=True, i=False, a=False: {'member': m, 'oid': r, 'set': s, 'inherit': i, 'admin': a}
        def extra_edge(o, e):
            o['memberships'].append(e)
            for r in o['roles'].values():
                if r['oid'] == e['member']:
                    r['memberships'].append({k: e[k] for k in ('oid', 'set', 'inherit', 'admin')})
        self.edit(lambda o: extra_edge(o, edge(OID['probe'], GROUP_OID[REQUEST_GROUP])), 'catalog_membership')
        self.edit(lambda o: extra_edge(o, edge(99999, OID['api_request'])), 'catalog_membership')      # a FOREIGN role is a member of our service role
        self.edit(lambda o: extra_edge(o, edge(OID['api_request'], 99998)), 'catalog_membership')      # our role is a member of a foreign role
        self.edit(lambda o: extra_edge(o, edge(99999, GROUP_OID[REQUEST_GROUP])), 'catalog_membership')  # a foreign member of the group
        self.edit(lambda o: o['memberships'].clear(), 'catalog_membership')
        for field in ('set', 'inherit', 'admin'):
            def flip(o, f=field):
                o['memberships'][0][f] = not o['memberships'][0][f]
                o['roles']['api_request']['memberships'][0][f] = o['memberships'][0][f]
            self.edit(flip, 'catalog_membership')
        self.edit(lambda o: o['roles']['probe']['memberships'].append({'oid': GROUP_OID[REQUEST_GROUP], 'set': True, 'inherit': False, 'admin': False}), 'catalog_membership')

    def test_a_service_role_may_not_own_the_database_or_any_protected_schema_or_relation(self):
        for purpose in ('api_request', 'probe', 'installer_enrollment'):
            self.edit(lambda o: o['protected_owners'].append({'kind': 'relation', 'schema': 'core', 'name': 'x', 'owner_oid': OID[purpose]}), 'catalog_ownership')
            self.edit(lambda o: o['protected_owners'].append({'kind': 'schema', 'schema': 'retrieval', 'name': 'retrieval', 'owner_oid': OID[purpose]}), 'catalog_ownership')
            self.edit(lambda o: o['database'].update(owner_oid=OID[purpose]), 'catalog_ownership')

    def test_finite_roles_never_hold_a_protected_binding(self):
        for purpose in ('bootstrap', 'migrator'):
            def add(o):
                o['bindings'].append({'role_oid': OID[purpose], 'role_name': LOGINS[purpose], 'logical_role': 'api_request', 'installation_id': self.cfg.installation_id, 'active': True,
                                      'revision': 1, 'operation_id': OP, 'database_oid': DB_OID, 'database_owner_oid': OID['bootstrap'], 'embedding_job_kind': None})
            self.edit(add, 'catalog_bindings', bound=True)

    def test_bindings_must_be_absent_or_exactly_the_three_revision_one_rows(self):
        self.edit(lambda o: o['bindings'].pop(), 'catalog_bindings', bound=True)                                   # partial
        self.edit(lambda o: o['bindings'][0].update(revision=2), 'catalog_bindings', bound=True)                    # arbitrary observed revision is never authority
        self.edit(lambda o: o['bindings'][0].update(revision=0), 'catalog_bindings', bound=True)
        self.edit(lambda o: o['bindings'][0].update(active=False), 'catalog_bindings', bound=True)
        self.edit(lambda o: o['bindings'][0].update(role_oid=99999), 'catalog_bindings', bound=True)
        self.edit(lambda o: o['bindings'][0].update(role_name='other'), 'catalog_bindings', bound=True)
        self.edit(lambda o: o['bindings'][0].update(installation_id=str(uuid.uuid4())), 'catalog_bindings', bound=True)
        self.edit(lambda o: o['bindings'][0].update(database_oid=1), 'catalog_bindings', bound=True)
        self.edit(lambda o: o['bindings'][0].update(database_owner_oid=OID['probe']), 'catalog_bindings', bound=True)
        self.edit(lambda o: o['bindings'][0].update(logical_role='probe'), 'catalog_bindings', bound=True)         # wrong logical role for this login
        self.edit(lambda o: o['bindings'][0].update(embedding_job_kind='x'), 'catalog_bindings', bound=True)
        self.edit(lambda o: o['bindings'].append(copy.deepcopy(o['bindings'][0])), 'catalog_bindings', bound=True)  # duplicate
        self.edit(lambda o: o['bindings'].append({**copy.deepcopy(o['bindings'][0]), 'role_oid': 77777, 'role_name': 'foreign'}), 'catalog_bindings', bound=True)  # foreign row for this installation

    def test_the_three_bindings_must_come_from_one_operation(self):
        self.edit(lambda o: o['bindings'][1].update(operation_id=str(uuid.uuid4())), 'catalog_bindings', bound=True)


class ServeBase(Base):
    """Serve phase: Core's observe_service_catalog() raw shape = the seed shape with bindings=None (deliberately unreadable) plus service={installation_matches, enrollment_readiness},
    read over the ACTUAL API session; finite roles are NOLOGIN by now. The same ONE policy checks flags/groups/memberships/ownership."""

    def setUp(self):
        super().setUp()
        path = self.vault / 'config/cortex.json'
        self.seed_cfg = self.cfg
        self.cfg = read_config_file(path, mode='serve')

    def serve_flags(self, purpose):
        f = dict(self.flags(purpose))
        f['login'] = self.policy[purpose]['lifetime'] == 'service'
        return f

    def healthy_serve(self, readiness='ready'):
        o = self.healthy()
        for p in FIVE:
            o['roles'][p]['flags'] = self.serve_flags(p)
        o['session'] = {'login': LOGINS['api_request'], 'oid': OID['api_request'], 'database_oid': DB_OID}
        o['bindings'] = None
        o['service'] = {'installation_matches': True, 'enrollment_readiness': readiness}
        return o

    def admit(self, obs, phase='serve', cfg=None):
        try:
            return R.validate_catalog(cfg or self.cfg, obs, phase=phase)
        except R.RoleRefusal as e:
            self.fail(f'a healthy catalog was refused: {e.reason}')

    def serve_refused(self, mutate, reason):
        o = self.healthy_serve()
        mutate(o)
        with self.assertRaises(R.RoleRefusal) as e:
            R.validate_catalog(self.cfg, o, phase='serve')
        self.assertEqual(e.exception.reason, reason)


class ServeAdmitted(ServeBase):
    def test_a_healthy_service_catalog_is_admitted_with_the_published_service_result(self):
        out = self.admit(self.healthy_serve())
        self.assertEqual(out['phase'], 'serve')
        self.assertEqual(out['service'], {'installation_matches': True, 'enrollment_readiness': 'ready'})
        self.assertEqual(out['bindings'], {'state': 'unobserved', 'rows': []})
        self.assertEqual(out['session']['login'], LOGINS['api_request'])

    def test_finite_roles_are_nologin_and_service_roles_login_in_the_admitted_flags(self):
        out = self.admit(self.healthy_serve())
        self.assertEqual({p: out['roles'][p]['flags']['login'] for p in FIVE}, {'bootstrap': False, 'migrator': False, 'installer_enrollment': False, 'api_request': True, 'probe': True})
        for p in FIVE:
            self.assertEqual({k: v for k, v in out['roles'][p]['flags'].items() if k != 'login'}, {k: v for k, v in self.flags(p).items() if k != 'login'}, p)

    def test_every_enrollment_readiness_value_is_passed_through_as_data(self):
        for value in ('ready', 'enrollment_required', 'unavailable'):
            out = self.admit(self.healthy_serve(value))
            self.assertEqual(out['service']['enrollment_readiness'], value)

    def test_the_serve_snapshot_is_detached(self):
        obs = self.healthy_serve()
        out = self.admit(obs)
        out['service']['installation_matches'] = False
        out['roles']['api_request']['flags']['superuser'] = True
        self.assertTrue(obs['service']['installation_matches'])
        self.assertFalse(obs['roles']['api_request']['flags']['superuser'])


class ServeRefused(ServeBase):
    def test_the_config_must_be_the_serve_view(self):
        with self.assertRaises(R.RoleRefusal) as e:
            R.validate_catalog(self.seed_cfg, self.healthy_serve(), phase='serve')
        self.assertEqual(e.exception.reason, 'catalog_config')

    def test_the_session_must_be_the_configured_api_login_by_name_oid_and_database(self):
        self.serve_refused(lambda o: o['session'].update(login=LOGINS['bootstrap'], oid=OID['bootstrap']), 'catalog_session')
        self.serve_refused(lambda o: o['session'].update(oid=OID['probe']), 'catalog_session')
        self.serve_refused(lambda o: o['session'].update(database_oid=1), 'catalog_session')

    def test_a_finite_login_that_can_still_log_in_is_refused(self):
        for p in ('bootstrap', 'migrator', 'installer_enrollment'):
            self.serve_refused(lambda o: o['roles'][p]['flags'].update(login=True), f'catalog_flags:{p}')

    def test_the_service_logins_must_keep_their_policy_flags(self):
        for p in ('api_request', 'probe'):
            self.serve_refused(lambda o: o['roles'][p]['flags'].update(login=False), f'catalog_flags:{p}')
            self.serve_refused(lambda o: o['roles'][p]['flags'].update(superuser=True), f'catalog_flags:{p}')

    def test_bindings_are_unreadable_in_serve_so_a_list_is_refused(self):
        self.serve_refused(lambda o: o.update(bindings=[]), 'catalog_shape')

    def test_the_service_result_must_be_exactly_the_published_shape(self):
        self.serve_refused(lambda o: o.pop('service'), 'catalog_shape')
        self.serve_refused(lambda o: o['service'].pop('enrollment_readiness'), 'catalog_shape')
        self.serve_refused(lambda o: o['service'].update(extra=1), 'catalog_shape')
        self.serve_refused(lambda o: o['service'].update(installation_matches=1), 'catalog_shape')
        self.serve_refused(lambda o: o['service'].update(enrollment_readiness='maybe'), 'catalog_service')

    def test_a_catalog_that_does_not_match_this_installation_is_refused(self):
        self.serve_refused(lambda o: o['service'].update(installation_matches=False), 'catalog_service')

    def test_the_shared_membership_group_and_ownership_policy_still_applies(self):
        self.serve_refused(lambda o: o['memberships'].clear(), 'catalog_membership')
        self.serve_refused(lambda o: o['groups'][REQUEST_GROUP]['flags'].update(login=True), 'catalog_groups')
        self.serve_refused(lambda o: o['protected_owners'].append({'kind': 'relation', 'schema': 'core', 'name': 'x', 'owner_oid': OID['api_request']}), 'catalog_ownership')

    def test_a_seed_observation_with_a_service_block_is_refused_by_the_seed_phase(self):
        o = self.healthy()
        o['service'] = {'installation_matches': True, 'enrollment_readiness': 'ready'}
        with self.assertRaises(R.RoleRefusal) as e:
            R.validate_catalog(self.seed_cfg, o)
        self.assertEqual(e.exception.reason, 'catalog_shape')


class LedgerGrantText(Base):
    """Kai Option A, bounded (J06-startup-ledger-access-v1): the API login's schema-ledger access is provisioned by its OWN installer-owned renderer (render_ledger_grant, run in the
    post phase after the migrations), never to PUBLIC/request group/probe, and render_post stays byte-for-byte what the nine PG methods run. A real PostgreSQL proof showed the table
    grant alone is DENIED (the login has no schema USAGE): the schema USAGE is the smallest addition and is listed separately.
    (Test correction after the RED commit 5b6b9faa: it first asserted these lines inside render_post, which the nine PG bodies run against a stand-in without the ledger.)"""

    def bind(self):
        from _fixtures import config, make_vault
        vault, _ = make_vault()
        return R.bind(config(vault))

    def test_render_ledger_grant_gives_only_the_exact_api_login_the_ledger_select_and_schema_usage(self):
        b = self.bind()
        sql = R.render_ledger_grant(b)
        api = b.logins['api_request']
        self.assertIn(f'GRANT USAGE ON SCHEMA core TO "{api}";', sql)
        self.assertIn(f'GRANT SELECT ON core.schema_migrations TO "{api}";', sql)
        grants = [l for l in sql.splitlines() if l.startswith('GRANT') or l.startswith('REVOKE')]
        self.assertEqual(len(grants), 2, grants)
        for l in grants:
            for forbidden in ('PUBLIC', REQUEST_GROUP, VERIFIER_GROUP, b.logins['probe'], b.logins['migrator'], b.logins['bootstrap'], b.logins['installer_enrollment']):
                self.assertNotIn(forbidden, l)

    def test_the_ledger_script_is_one_transaction_that_stops_on_error(self):
        lines = R.render_ledger_grant(self.bind()).splitlines()
        self.assertEqual(lines[0], '\\set ON_ERROR_STOP on')
        self.assertEqual((lines[1], lines[-1]), ('BEGIN;', 'COMMIT;'))

    def test_render_post_is_unchanged_by_the_ledger_grant(self):
        sql = R.render_post(self.bind())
        self.assertNotIn('schema_migrations', sql)
        self.assertNotIn('ON SCHEMA core', sql)


if __name__ == '__main__':
    unittest.main()
