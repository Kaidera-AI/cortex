"""Day-1 foundation negatives; these never qualify an installed proof.

Finn-owned assertions from the accepted DAY-1 / INDEX J04 and J11 contracts.
Repository MIT licence. Baseline: Cortex 7a1078213ac2497d6219e7778c7866001f97f30b.
No supplier, vault store, credential or admission is manufactured here.
The loopback recorder observes the existing helper's transport effects only.
"""
import importlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[5]
HELPER = ROOT / 'packages/cli/_cortex_api.sh'


class SeedFoundationTests(unittest.TestCase):
    def test_installed_binding_requires_private_custody(self):
        # A missing seed module is an ERROR, never a causal BODY kill. Once the
        # owned entry point exists, this assertion requires a typed refusal.
        seed = importlib.import_module('cortex_core.schema.seed')
        with self.assertRaises(Exception) as observed:
            seed.seed_runtime_bindings(None, 'synthetic-day1-operation', time.monotonic() + 5)
        self.assertTrue(
            isinstance(getattr(observed.exception, 'reason', None), str),
            'installed_binding: missing private config/custody must produce a typed refusal',
        )

    def test_three_command_memory_missing_projection_has_zero_http_effects(self):
        # This is a missing-admission negative, not a fake successful memory API.
        # Legacy project/API environment variables cannot replace Core config.
        requests = []

        class Recorder(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                requests.append((self.command, self.path))
                body = json.dumps({'project_id': '00000000-0000-0000-0000-000000000001'}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='finn-day1-negative-') as temp:
                env = {
                    'PATH': os.environ['PATH'],
                    'HOME': temp,
                    'TMPDIR': temp,
                    'CORTEX_PROJECT_ROOT': temp,
                    'CORTEX_AGENTS_DIR': temp,
                    'CORTEX_RUNTIME_CONFIG': str(Path(temp) / 'absent-runtime.yaml'),
                    'CORTEX_WORKSPACE_CONFIG': str(Path(temp) / 'absent-workspace.json'),
                    'CORTEX_PROJECT': 'synthetic-project',
                    'CORTEX_API_URL': f'http://127.0.0.1:{server.server_port}',
                }
                # Sourcing alone must not discover/probe v1 before the sole
                # private Core client projection is available. No real service
                # or database is contacted, and no successful write is faked.
                result = subprocess.run(
                    ['/bin/bash', '-c', 'source "$1"', 'finn-negative', str(HELPER)],
                    env=env, cwd=temp, capture_output=True, text=True, timeout=10,
                )
            self.assertEqual(
                requests, [],
                'three_command_memory: missing Core client projection must refuse before any HTTP request; '
                f'helper exit={result.returncode}, observed={requests!r}',
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), 'owned recorder must be reaped')




# Day-2 tests-first source controls. Driver doubles model durable state, not SQL
# authority. Actual custody/config/resource owners are composed unchanged. These
# controls confer no PostgreSQL/native/installed or cross-host concurrency credit.
import copy
from contextlib import contextmanager
import hashlib
import re
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid5


class _Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)
    def fetchone(self):
        return self.rows[0] if self.rows else None
    def fetchall(self):
        return copy.deepcopy(self.rows)


class _SeedDatabase:
    """Test-only DBAPI effect model, never imported by production seed."""
    def __init__(self, config, policy, prefix):
        self.config, self.policy, self.prefix = config, policy, prefix
        self.events, self.calls = [], []
        self.tables = {name: [] for name in (
            'core.installations', 'core.tenants', 'core.projects',
            'auth.runtime_login_bindings', 'auth.first_identity_receipts',
            'auth.principals', 'core.record_aliases', 'core.records')}
        self.role_oids = {name: 100 + i for i, name in enumerate(config.role_logins)}
        self.group_oids = {'kaidera-runtime-core-request': 200,
                           'kaidera-runtime-core-verifier': 201}
        self.flags_override, self.oid_override = {}, {}
        self.database_owner = self.role_oids['bootstrap']
        self.foreign_edge = False
        self.closed_finite = False
        self.lose_commit = None
        self.readiness = 'ready'
        self.before_write = None
        self.enrollments = 0

    def connect(self, **kwargs):
        purpose = next(k for k, v in self.config.role_logins.items() if v == kwargs['user'])
        if self.closed_finite and purpose in ('bootstrap', 'migrator'):
            raise ValueError('finite role is closed')
        self.calls.append(purpose)
        return _SeedConnection(self, purpose)

    def writes(self):
        return [e for e in self.events if e[0] == 'write']

    def snapshot(self):
        return copy.deepcopy(self.tables)

    def binding_rows(self):
        fields = ('role_oid', 'role_name', 'logical_role', 'installation_id',
                  'active', 'revision', 'operation_id', 'database_oid',
                  'database_owner_oid', 'embedding_job_kind')
        return [tuple(row[k] for k in fields) for row in
                sorted(self.tables['auth.runtime_login_bindings'], key=lambda r:r['role_oid'])]

    def role_rows(self):
        fields = ('login', 'superuser', 'createdb', 'createrole', 'inherit',
                  'replication', 'bypassrls')
        rows = []
        for purpose, login in self.config.role_logins.items():
            flags = {k:self.policy[purpose][k] for k in fields}
            flags.update(self.flags_override.get(purpose, {}))
            rows.append((login, self.oid_override.get(purpose, self.role_oids[purpose]),
                         *(flags[k] for k in fields)))
        for login, oid in self.group_oids.items():
            rows.append((login, oid, *([False] * len(fields))))
        return rows


class _SeedConnection:
    def __init__(self, database, purpose):
        self.db, self.purpose = database, purpose
    def close(self):
        self.db.events.append(('close', self.purpose))
    @contextmanager
    def transaction(self):
        prior = self.db.snapshot()
        self.db.events.append(('begin', self.purpose))
        try:
            yield
        except BaseException:
            self.db.tables = prior
            self.db.events.append(('rollback', self.purpose))
            raise
        else:
            self.db.events.append(('commit', self.purpose))
            if self.db.lose_commit == self.purpose:
                self.db.lose_commit = None
                raise OSError('test lost acknowledgement after durable DB commit')

    def execute(self, sql, params=None):
        text = ' '.join(str(sql).split())
        lower = text.lower()
        params = tuple(params or ())
        self.db.events.append(('sql', self.purpose, text, params))
        if lower.startswith('set transaction') or 'set_config(' in lower:
            return _Rows([(None, None)])
        if 'pg_advisory' in lower or lower.startswith('lock table'):
            self.db.events.append(('lock', self.purpose))
            return _Rows([(True,)])
        if 'from pg_catalog.pg_database' in lower:
            return _Rows([('cortex', 42, self.db.database_owner)])
        if 'from pg_catalog.pg_stat_activity' in lower:
            return _Rows([(self.db.config.role_logins[self.purpose],
                           self.db.role_oids[self.purpose], 42)])
        if 'from pg_catalog.pg_roles' in lower:
            return _Rows(self.db.role_rows())
        if 'from pg_catalog.pg_auth_members' in lower:
            edges = [(self.db.role_oids['api_request'], 200, True, False, False)]
            if self.db.foreign_edge:
                edges.append((999, self.db.role_oids['api_request'], False, False, True))
            return _Rows(edges)
        if 'from pg_catalog.pg_namespace' in lower:
            return _Rows([('schema', 'core', 'core', 201),
                          ('relation', 'auth', 'first_identity_receipts', 201)])
        if 'from core.schema_migrations' in lower:
            return _Rows(self.db.prefix)
        if 'auth.enrollment_readiness(' in lower:
            if self.purpose != 'api_request':
                raise AssertionError('readiness must use actual api_request connection')
            self.db.events.append(('readiness', self.purpose))
            return _Rows([(self.db.readiness,)])
        if 'auth.first_identity(' in lower:
            if self.purpose != 'installer_enrollment':
                raise AssertionError('first_identity must use actual installer connection')
            self._write('auth.first_identity')
            install, tenant, project, principal, credential, digest, key = map(str, params)
            row = dict(zip(('installation_id', 'tenant_id', 'project_id', 'principal_id',
                            'credential_id', 'key_digest', 'request_key'),
                           (install, tenant, project, principal, credential, digest, key)))
            existing = self.db.tables['auth.first_identity_receipts']
            replay = bool(existing)
            if existing and existing != [row]:
                raise ValueError('first_identity_forbidden')
            if not existing:
                self.db.tables['auth.first_identity_receipts'] = [row]
                self.db.enrollments += 1
            self.db.events.append(('enrollment', self.purpose, copy.deepcopy(row)))
            return _Rows([(dict(principal_id=principal, credential_id=credential,
                                project_id=project, replayed=replay),)])
        match = re.match(r'insert into ([a-z_.]+)\s*\(([^)]+)\)\s*values\s*\((.*?)\)',
                         text, re.I)
        if match:
            table, columns, values = match.groups()
            table = table.lower()
            if table not in ('core.installations', 'core.tenants', 'core.projects',
                             'auth.runtime_login_bindings'):
                raise AssertionError('seed must delegate enrollment to existing SQL')
            self._write(table)
            fields = [x.strip() for x in columns.split(',')]
            arguments, iterator = [], iter(params)
            for value in values.split(','):
                v = value.strip()
                if v.startswith('%s'):
                    arguments.append(next(iterator))
                elif v.lower() in ('true', 'false', 'null'):
                    arguments.append({'true':True, 'false':False, 'null':None}[v.lower()])
                elif v.startswith("'"):
                    arguments.append(v.split("'")[1])
                else:
                    arguments.append(int(v))
            row = dict(zip(fields, arguments, strict=True))
            if row not in self.db.tables[table]:
                self.db.tables[table].append(row)
            return _Rows()
        if lower.startswith(('update ', 'delete ', 'alter ', 'create ', 'drop ', 'grant ')):
            raise AssertionError('seed may not rewrite authority or implement role lifecycle')
        match = re.match(r'select (.*?) from ([a-z_.]+)(?:\s|$)', text, re.I)
        if match and match.group(2).lower() in self.db.tables:
            columns, table = match.groups()
            rows = self.db.tables[table.lower()]
            if columns.lower() == 'count(*)':
                return _Rows([(len(rows),)])
            if columns == '1':
                return _Rows([(1,) for _ in rows])
            if columns == '*':
                raise AssertionError('test driver requires explicit seed projections')
            fields = [re.sub(r'::[a-z0-9_]+', '', f.strip()).split('.')[-1]
                      for f in columns.split(',')]
            return _Rows([tuple(r[f] for f in fields) for r in rows])
        raise AssertionError('test driver has no admitted query: ' + text)

    def _write(self, table):
        if self.db.before_write:
            self.db.before_write(table)
        self.db.events.append(('write', self.purpose, table))


class DurableSeedTests(unittest.TestCase):
    """Missing implementation is a named BODY failure, never fixture/import credit."""
    def require_seed(self):
        try:
            module = importlib.import_module('cortex_core.schema.seed')
        except ModuleNotFoundError as error:
            if error.name != 'cortex_core.schema.seed':
                raise
            module = None
        self.assertIsNotNone(module, 'durable_seed: actual sole implementation is missing')
        return module

    @contextmanager
    def scenario(self):
        from cortex_core.runtime import resources
        from cortex_core.runtime.bootstrap_config import decode_config
        from cortex_core.runtime.custody import open_custody
        from cortex_core.runtime.tests.test_bootstrap_config import sample
        from cortex_core.migrations import prepare_migrations, SCHEMA
        with tempfile.TemporaryDirectory(prefix='finn-seed-controls-') as temp:
            root = Path(temp).resolve()
            root.chmod(0o700)
            (root/'secrets/cortex').mkdir(parents=True, mode=0o700)
            (root/'secrets').chmod(0o700)
            ca = root/'secrets/cortex/ca'
            ca.write_bytes(b'public-source-test-CA'); ca.chmod(0o600)
            value = sample(root)
            value['roles']['installer_enrollment']['login'] = 'kaidera-runtime-core-installer'
            config = decode_config(json.dumps(value).encode(), mode='seed')
            contracts = Path(SCHEMA).parent/'contracts/runtime/runtime-operations-v1.json'
            policy = json.loads(contracts.read_bytes())['roles']
            prefix = [(i,d) for i,d,_ in prepare_migrations(SCHEMA)]
            db = _SeedDatabase(config, policy, prefix)
            read, write = os.pipe()
            os.write(write, bytes(range(32))); os.close(write)  # PUBLIC fixture only
            try:
                with open_custody(config, unlock_fd=read, key_id='unit-public-key') as vault:
                    for purpose in config.roles:
                        vault.import_credential(purpose, b'public-source-test-password')
                    case = SimpleNamespace(config=config, db=db, vault=vault,
                                           resources=resources, operation='seed-unit-operation')
                    with patch.object(resources, '_driver_connect', side_effect=db.connect):
                        yield case
            finally:
                os.close(read)

    def invoke(self, seed, case, key=None):
        deadline = time.monotonic() + 15
        with case.resources.open_seed_resources(case.config, custody=case.vault,
                                               deadline=deadline):
            return seed.seed_runtime_bindings(case.config,
                        case.operation if key is None else key, deadline)

    def material(self, case):
        with case.vault.seed_record() as record:
            return record.load()

    def refuse(self, action):
        with self.assertRaises(ValueError) as observed:
            action()
        self.assertIsInstance(observed.exception.reason, str,
                              'seed failure must have a sanitized typed reason')
        return observed.exception

    def test_prepared_is_durable_before_every_sql_write(self):
        seed = self.require_seed()
        with self.scenario() as c:
            from cortex_core.runtime import custody_fs
            prepared = []
            original = custody_fs.write_record
            def write(*args, **kwargs):
                result = original(*args, **kwargs)
                if args[1] == 'client':
                    prepared.append(True)
                return result
            c.db.before_write = lambda table:self.assertTrue(
                prepared, 'seed_sql_write_before_durable_PREPARED')
            with patch.object(custody_fs, 'write_record', side_effect=write):
                result = self.invoke(seed, c)
            m = self.material(c)
            self.assertEqual(m.state, 'committed')
            self.assertTrue(c.db.writes(), 'refusal-only implementation is not a seed')
            self.assertNotIn(m.credential.decode(), repr(result))
            self.assertEqual(len(c.db.tables['auth.first_identity_receipts']), 1)

    def test_prepared_durability_lost_ack_stops_sql_then_reuses_material(self):
        seed = self.require_seed()
        with self.scenario() as c:
            from cortex_core.runtime import custody_fs
            original = custody_fs.write_record
            def lost(*args, **kwargs):
                result = original(*args, **kwargs)
                if args[1] == 'client':
                    raise OSError('public test durability ack lost')
                return result
            with patch.object(custody_fs, 'write_record', side_effect=lost):
                self.refuse(lambda:self.invoke(seed, c))
            self.assertEqual(c.db.writes(), [], 'no SQL after uncertain PREPARED ack')
            before = self.material(c)
            self.assertEqual(before.state, 'prepared')
            self.invoke(seed, c)
            after = self.material(c)
            self.assertEqual((after.intent, after.credential), (before.intent, before.credential))
            self.assertEqual(after.state, 'committed')

    def test_binding_commit_lost_ack_replays_exact_original(self):
        seed = self.require_seed()
        with self.scenario() as c:
            # Observer is read-only: only lose the first bootstrap COMMIT that
            # actually has writes, not its catalog snapshot acknowledgement.
            original = _SeedConnection.transaction
            @contextmanager
            def lost(connection):
                prior = len(connection.db.writes())
                with original(connection):
                    yield
                    if connection.purpose == 'bootstrap' and len(connection.db.writes()) > prior:
                        connection.db.lose_commit = 'bootstrap'
            with patch.object(_SeedConnection, 'transaction', lost):
                self.refuse(lambda:self.invoke(seed, c))
            before = self.material(c)
            anchors = c.db.snapshot()
            self.assertEqual(len(anchors['auth.runtime_login_bindings']), 3)
            self.assertEqual(anchors['auth.first_identity_receipts'], [])
            self.invoke(seed, c)
            self.assertEqual(self.material(c).intent, before.intent)
            self.assertEqual(self.material(c).credential, before.credential)
            self.assertEqual(c.db.tables['auth.runtime_login_bindings'], anchors['auth.runtime_login_bindings'])
            self.assertEqual(c.db.enrollments, 1)

    def test_enrollment_commit_lost_ack_has_one_human_and_same_credential(self):
        seed = self.require_seed()
        with self.scenario() as c:
            c.db.lose_commit = 'installer_enrollment'
            self.refuse(lambda:self.invoke(seed, c))
            before = self.material(c)
            receipt = copy.deepcopy(c.db.tables['auth.first_identity_receipts'])
            self.assertEqual(len(receipt), 1)
            self.invoke(seed, c)
            self.assertEqual(c.db.tables['auth.first_identity_receipts'], receipt)
            self.assertEqual(c.db.enrollments, 1)
            self.assertEqual(self.material(c).credential, before.credential)
            self.assertEqual(self.material(c).intent, before.intent)

    def test_readiness_unavailable_cannot_checkpoint_or_return_success(self):
        seed = self.require_seed()
        with self.scenario() as c:
            c.db.readiness = 'unavailable'
            self.refuse(lambda:self.invoke(seed, c))
            before = self.material(c)
            self.assertEqual(before.state, 'prepared')
            self.assertTrue(any(e[0] == 'readiness' for e in c.db.events))
            c.db.readiness = 'ready'
            self.invoke(seed, c)
            self.assertEqual(self.material(c).state, 'committed')
            self.assertEqual(self.material(c).credential, before.credential)

    def test_checkpoint_failure_preserves_receipt_and_never_claims_success(self):
        seed = self.require_seed()
        with self.scenario() as c:
            from cortex_core.runtime.custody import _SeedRecord
            with patch.object(_SeedRecord, 'commit', side_effect=OSError('public checkpoint failure')):
                self.refuse(lambda:self.invoke(seed, c))
            before = self.material(c)
            self.assertEqual(before.state, 'prepared')
            self.assertEqual(len(c.db.tables['auth.first_identity_receipts']), 1)
            self.invoke(seed, c)
            self.assertEqual(self.material(c).credential, before.credential)
            self.assertEqual(c.db.enrollments, 1)

    def test_checkpoint_lost_ack_reconciles_same_committed_record(self):
        seed = self.require_seed()
        with self.scenario() as c:
            from cortex_core.runtime.custody import _SeedRecord
            original = _SeedRecord.commit
            def lost(record, **kwargs):
                original(record, **kwargs)
                raise OSError('public checkpoint acknowledgement lost')
            with patch.object(_SeedRecord, 'commit', lost):
                self.refuse(lambda:self.invoke(seed, c))
            before = self.material(c)
            self.assertEqual(before.state, 'committed')
            self.invoke(seed, c)
            after = self.material(c)
            self.assertEqual((after.intent, after.credential, after.receipt, after.generation),
                             (before.intent, before.credential, before.receipt, before.generation))
            self.assertEqual(c.db.enrollments, 1)

    def test_changed_operation_key_refuses_without_sql_or_record_replacement(self):
        seed = self.require_seed()
        with self.scenario() as c:
            self.invoke(seed, c)
            before = self.material(c)
            count = len(c.db.writes())
            self.refuse(lambda:self.invoke(seed, c, c.operation + '-different'))
            self.assertEqual(len(c.db.writes()), count)
            self.assertEqual(self.material(c).intent, before.intent)
            self.assertEqual(self.material(c).credential, before.credential)

    def test_absent_custody_with_partial_anchor_never_creates_new_secret(self):
        seed = self.require_seed()
        with self.scenario() as c:
            c.db.tables['core.installations'] = [{'id':c.config.installation_id}]
            before = c.db.snapshot()
            self.refuse(lambda:self.invoke(seed, c))
            self.assertIsNone(self.material(c), 'partial installation cannot mint replacement custody')
            self.assertEqual(c.db.writes(), [])
            self.assertEqual(c.db.tables, before)

    def test_deleted_custody_with_existing_receipt_never_rotates(self):
        seed = self.require_seed()
        with self.scenario() as c:
            self.invoke(seed, c)
            before = c.db.snapshot()
            count = len(c.db.writes())
            (Path(c.config.vault_root)/c.config.client_ref).unlink()
            self.refuse(lambda:self.invoke(seed, c))
            self.assertIsNone(self.material(c))
            self.assertEqual(c.db.tables, before)
            self.assertEqual(len(c.db.writes()), count)

    def test_changed_role_oid_refuses_exact_replay_even_with_same_name(self):
        seed = self.require_seed()
        with self.scenario() as c:
            self.invoke(seed, c)
            before = self.material(c)
            count = len(c.db.writes())
            c.db.oid_override['api_request'] = 9999
            self.refuse(lambda:self.invoke(seed, c))
            self.assertEqual(len(c.db.writes()), count)
            self.assertEqual(self.material(c).intent, before.intent)

    def test_unsafe_flags_foreign_membership_and_database_drift_refuse(self):
        seed = self.require_seed()
        for kind in ('superuser', 'incoming-member', 'database-owner'):
            with self.subTest(kind=kind), self.scenario() as c:
                if kind == 'superuser':
                    c.db.flags_override['api_request'] = {'superuser':True}
                elif kind == 'incoming-member':
                    c.db.foreign_edge = True
                else:
                    c.db.database_owner = 999
                self.refuse(lambda:self.invoke(seed, c))
                self.assertEqual(c.db.writes(), [])
                self.assertIsNone(self.material(c))

    def test_exact_three_revision_one_bindings_share_derived_operation(self):
        seed = self.require_seed()
        with self.scenario() as c:
            self.invoke(seed, c)
            m = self.material(c)
            self.assertEqual({k:v['revision'] for k,v in m.intent['roles'].items()},
                             dict(bootstrap=0, migrator=0, api_request=1, probe=1, installer_enrollment=1))
            rows = c.db.tables['auth.runtime_login_bindings']
            expected = str(uuid5(UUID(c.config.installation_id), 'cortex.seed-binding.v1:' + c.operation))
            self.assertEqual({r['logical_role'] for r in rows},
                             {'api_request', 'probe', 'installer_enrollment'})
            self.assertEqual(len(rows), 3)
            for row in rows:
                self.assertEqual((row['active'], row['revision'], str(row['operation_id']),
                                  row['embedding_job_kind']), (True, 1, expected, None))
            rows[0]['operation_id'] = '10000000-0000-0000-0000-000000000099'
            count = len(c.db.writes())
            self.refuse(lambda:self.invoke(seed, c))
            self.assertEqual(len(c.db.writes()), count, 'foreign operation must not be overwritten')

    def test_local_same_and_conflicting_callers_cannot_bypass_custody_lock(self):
        seed = self.require_seed()
        with self.scenario() as c:
            for key in (c.operation, c.operation + '-competing'):
                with self.subTest(key=key), c.vault.seed_record():
                    self.refuse(lambda:self.invoke(seed, c, key))
            self.assertEqual(c.db.writes(), [])
            self.invoke(seed, c)
            before = self.material(c)
            self.invoke(seed, c)
            self.refuse(lambda:self.invoke(seed, c, c.operation + '-competing'))
            self.assertEqual(self.material(c).credential, before.credential)
            self.assertEqual(c.db.enrollments, 1)

    def test_no_bound_resource_wrong_identity_and_wrong_deadline_refuse(self):
        seed = self.require_seed()
        self.refuse(lambda:seed.seed_runtime_bindings(None, 'public-operation', time.monotonic()+5))
        with self.scenario() as c:
            self.refuse(lambda:seed.seed_runtime_bindings(c.config, c.operation, time.monotonic()+5))
            deadline = time.monotonic()+15
            with c.resources.open_seed_resources(c.config, custody=c.vault, deadline=deadline):
                self.refuse(lambda:seed.seed_runtime_bindings(c.config, c.operation, deadline+1))
            self.assertEqual(c.db.writes(), [])

    def test_closed_finite_authority_cannot_be_reopened_by_seed(self):
        seed = self.require_seed()
        with self.scenario() as c:
            self.invoke(seed, c)
            before = self.material(c)
            count = len(c.db.writes())
            c.db.closed_finite = True
            self.refuse(lambda:self.invoke(seed, c))
            self.assertEqual(len(c.db.writes()), count)
            self.assertEqual(self.material(c).credential, before.credential)
            self.assertEqual(self.material(c).generation, before.generation)

    def test_rollback_during_bindings_retains_prepared_for_exact_retry(self):
        seed = self.require_seed()
        with self.scenario() as c:
            calls = []
            def interrupt(table):
                calls.append(table)
                if calls.count('auth.runtime_login_bindings') == 2:
                    raise OSError('public test interrupted anchor transaction')
            c.db.before_write = interrupt
            self.refuse(lambda:self.invoke(seed, c))
            before = self.material(c)
            self.assertEqual(before.state, 'prepared')
            self.assertTrue(all(not rows for rows in c.db.tables.values()),
                            'bootstrap rollback must leave no partial anchors/bindings')
            c.db.before_write = None
            self.invoke(seed, c)
            self.assertEqual(self.material(c).intent, before.intent)
            self.assertEqual(self.material(c).credential, before.credential)
            self.assertEqual(c.db.enrollments, 1)

    def test_actual_purpose_order_and_exact_sql_receipt_precede_checkpoint(self):
        seed = self.require_seed()
        with self.scenario() as c:
            from cortex_core.runtime.custody import _SeedRecord
            from cortex_core.release.codec import canonical_bytes
            original = _SeedRecord.commit
            checkpoints = []
            def checkpoint(record, *, receipt):
                m = record._material
                expected = {k:m.intent[k] for k in ('installation_id', 'tenant_id',
                    'project_id', 'principal_id', 'credential_id', 'operation_key', 'credential_sha256')}
                expected['intent_sha256'] = hashlib.sha256(canonical_bytes(m.intent)).hexdigest()
                self.assertEqual(receipt, expected, 'checkpoint must bind exact confirmed intent')
                events = [e[0] for e in c.db.events]
                self.assertLess(events.index('enrollment'), events.index('readiness'))
                self.assertIn(('commit', 'installer_enrollment'), c.db.events)
                self.assertIn(('commit', 'api_request'), c.db.events)
                checkpoints.append(True)
                return original(record, receipt=receipt)
            with patch.object(_SeedRecord, 'commit', checkpoint):
                self.invoke(seed, c)
            self.assertTrue(checkpoints, 'no successful seed without durable checkpoint')
            self.assertEqual(c.db.calls, ['bootstrap', 'installer_enrollment', 'api_request'])
            self.assertTrue(any(e[0] == 'lock' for e in c.db.events), 'seed requires SQL serialization')
            self.assertEqual(self.material(c).state, 'committed')

    def test_changed_sql_receipt_refuses_without_checkpoint_rewrite(self):
        seed = self.require_seed()
        with self.scenario() as c:
            self.invoke(seed, c)
            before = self.material(c)
            c.db.tables['auth.first_identity_receipts'][0]['key_digest'] = '0' * 64
            count = len(c.db.writes())
            self.refuse(lambda:self.invoke(seed, c))
            self.assertEqual(len(c.db.writes()), count)
            after = self.material(c)
            self.assertEqual((after.intent, after.credential, after.receipt, after.generation),
                             (before.intent, before.credential, before.receipt, before.generation))

    def test_partial_inactive_and_revised_bindings_cannot_be_repaired_implicitly(self):
        seed = self.require_seed()
        for change in ('partial', 'inactive', 'revision'):
            with self.subTest(change=change), self.scenario() as c:
                self.invoke(seed, c)
                rows = c.db.tables['auth.runtime_login_bindings']
                if change == 'partial':
                    rows.pop()
                elif change == 'inactive':
                    rows[0]['active'] = False
                else:
                    rows[0]['revision'] = 2
                before = c.db.snapshot()
                count = len(c.db.writes())
                self.refuse(lambda:self.invoke(seed, c))
                self.assertEqual(c.db.tables, before)
                self.assertEqual(len(c.db.writes()), count)

    def test_tampered_ciphertext_cannot_rotate_existing_enrollment(self):
        seed = self.require_seed()
        with self.scenario() as c:
            self.invoke(seed, c)
            before = c.db.snapshot()
            count = len(c.db.writes())
            path = Path(c.config.vault_root)/c.config.client_ref
            raw = path.read_bytes()
            envelope = json.loads(raw)
            token = envelope['ciphertext']
            envelope['ciphertext'] = ('A' if token[0] != 'A' else 'B') + token[1:]
            path.write_text(json.dumps(envelope)); path.chmod(0o600)
            self.refuse(lambda:self.invoke(seed, c))
            self.assertEqual(c.db.tables, before)
            self.assertEqual(len(c.db.writes()), count)
            self.assertEqual(path.read_text(), json.dumps(envelope))

    def test_incomplete_or_changed_schema_ledger_refuses_before_prepared(self):
        seed = self.require_seed()
        for change in ('missing', 'changed'):
            with self.subTest(change=change), self.scenario() as c:
                if change == 'missing':
                    c.db.prefix.pop()
                else:
                    identity, digest = c.db.prefix[-1]
                    c.db.prefix[-1] = (identity, '0' * 64)
                self.refuse(lambda:self.invoke(seed, c))
                self.assertIsNone(self.material(c))
                self.assertEqual(c.db.writes(), [])

    def test_serve_mode_never_acquires_finite_seed_authority(self):
        seed = self.require_seed()
        with self.scenario() as c:
            from cortex_core.runtime.bootstrap_config import decode_config
            from cortex_core.runtime.custody import open_custody
            from cortex_core.runtime.tests.test_bootstrap_config import sample
            value = sample(Path(c.config.vault_root))
            value['roles']['installer_enrollment']['login'] = 'kaidera-runtime-core-installer'
            serve = decode_config(json.dumps(value).encode(), mode='serve')
            read, write = os.pipe()
            os.write(write, bytes(range(32))); os.close(write)
            try:
                with open_custody(serve, unlock_fd=read, key_id='unit-public-key') as vault:
                    for purpose in serve.roles:
                        vault.import_credential(purpose, b'public-source-test-password')
                    deadline = time.monotonic()+15
                    with c.resources.open_core_resources(serve, custody=vault, deadline=deadline):
                        self.refuse(lambda:seed.seed_runtime_bindings(serve, c.operation, deadline))
            finally:
                os.close(read)
            self.assertEqual(c.db.calls, ['api_request', 'probe'])
            self.assertEqual(c.db.writes(), [])


if __name__ == '__main__':
    unittest.main()
