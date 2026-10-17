"""One finite seed: durable private intent, existing SQL enrollment, exact replay.

Repository MIT licence. No role provisioning, credential rotation, migration or
public authority injection lives here. Core owns custody/connections/deadlines;
the packaged ship validator alone admits the actual role catalog.
"""
import hashlib
import secrets
from uuid import UUID, uuid4, uuid5

from cortex_core.migrations import SCHEMA, prepare_migrations
from cortex_core.release.codec import canonical_bytes
from cortex_core.runtime.resources import current_seed_resources
from cortex_core.runtime.signed_member import Refusal
from .readiness import check_schema_ready

_BOUND = ('api_request', 'probe', 'installer_enrollment')
_BINDING_FIELDS = ('role_oid', 'role_name', 'logical_role', 'installation_id',
                   'active', 'revision', 'operation_id', 'database_oid',
                   'database_owner_oid', 'embedding_job_kind')
_RECEIPT_FIELDS = ('installation_id', 'tenant_id', 'project_id', 'principal_id',
                   'credential_id', 'credential_sha256', 'operation_key')


def _operation(key):
    try:
        if (type(key) is not str or not 1 <= len(key) <= 256
                or len(key.encode('utf-8', 'strict')) > 1024
                or any(ord(c) < 32 or ord(c) == 127 for c in key)):
            raise ValueError
    except (ValueError, UnicodeError):
        raise Refusal('seed_operation_key') from None


def _intent(config, key, catalog, material):
    # Schema.catalog() has already admitted this complete ledger. This digest
    # binds its immutable manifest to custody; it does not replace admission.
    prefix = [[identity, digest] for identity, digest, _ in prepare_migrations(SCHEMA)]
    roles = {purpose: dict(login=role['login'], oid=role['oid'],
                revision=role['intended_revision'], flags=role['flags'],
                memberships=role['memberships'])
             for purpose, role in catalog['roles'].items()}
    stable = dict(operation_key=key, installation_id=config.installation_id,
        tenant_id=config.tenant_id, project_id=config.project_id,
        client_ref=config.client_ref, database=catalog['database'], roles=roles,
        schema_prefix_sha256=hashlib.sha256(canonical_bytes(prefix)).hexdigest())
    if material is None:
        # Do not even mint replacement IDs/secret until serialized SQL proves
        # absence. The caller has not yet inspected installation anchors.
        return stable
    credential = material.credential
    old = material.intent
    for name in ('principal_id', 'credential_id'):
        value = old.get(name)
        if type(value) is not str or str(UUID(value)) != value or UUID(value).int == 0:
            raise Refusal('seed_intent')
        stable[name] = value
    stable['credential_sha256'] = hashlib.sha256(credential).hexdigest()
    if old != stable:
        raise Refusal('seed_intent')
    return stable


def _bindings(intent):
    operation = str(uuid5(UUID(intent['installation_id']),
                         'cortex.seed-binding.v1:' + intent['operation_key']))
    return sorted([dict(zip(_BINDING_FIELDS, (
        intent['roles'][purpose]['oid'], intent['roles'][purpose]['login'],
        purpose, intent['installation_id'], True, intent['roles'][purpose]['revision'],
        operation, intent['database']['oid'], intent['database']['owner_oid'], None),
        strict=True)) for purpose in _BOUND], key=lambda row: row['role_oid'])


def _read_bindings(connection, config):
    rows = connection.execute('''SELECT role_oid::bigint,role_name::text,
        logical_role,installation_id::text,active,revision,operation_id::text,
        database_oid::bigint,database_owner_oid::bigint,embedding_job_kind
        FROM auth.runtime_login_bindings WHERE installation_id=%s::uuid
        OR role_name=ANY(%s::name[]) ORDER BY role_oid FOR UPDATE''',
        (config.installation_id, list(config.role_logins.values()))).fetchall()
    return [dict(zip(_BINDING_FIELDS, row, strict=True)) for row in rows]


def _anchors(connection, config):
    installations = connection.execute('''SELECT id::text FROM core.installations
        WHERE id=%s::uuid FOR UPDATE''', (config.installation_id,)).fetchall()
    tenants = connection.execute('''SELECT id::text,installation_id::text FROM core.tenants
        WHERE id=%s::uuid OR installation_id=%s::uuid FOR UPDATE''',
        (config.tenant_id, config.installation_id)).fetchall()
    projects = connection.execute('''SELECT tenant_id::text,id::text,name FROM core.projects
        WHERE tenant_id=%s::uuid FOR UPDATE''', (config.tenant_id,)).fetchall()
    return [list(installations), list(tenants), list(projects)]


def _sql_receipt(connection, config):
    return connection.execute('''SELECT installation_id::text,tenant_id::text,
        project_id::text,principal_id::text,credential_id::text,key_digest,request_key
        FROM auth.first_identity_receipts WHERE installation_id=%s::uuid''',
        (config.installation_id,)).fetchall()


def _expected_receipt(intent):
    return tuple(intent[name] for name in _RECEIPT_FIELDS)


def _empty_identity(connection, config):
    # Existing enrollment SQL makes the final authoritative empty-human check.
    # These reads additionally prevent new custody after orphan/partial state.
    for sql in ('''SELECT 1 FROM auth.principals WHERE tenant_id=%s::uuid''',
                '''SELECT 1 FROM core.record_aliases WHERE tenant_id=%s::uuid
                    AND source_namespace='cortex.core.identity' ''',
                '''SELECT 1 FROM core.records WHERE tenant_id=%s::uuid
                    AND kind='core.identity' '''):
        if connection.execute(sql, (config.tenant_id,)).fetchall():
            raise Refusal('seed_partial_state')


def _bind(resources, config, record, material, intent):
    expected = _bindings(intent)
    with resources.transaction('bootstrap') as connection:
        # The same lock as migrations, then map before installation (the order
        # used by existing auth.first_identity). Table locks cover absent rows,
        # including a competing remote seed; local custody alone cannot do so.
        connection.execute('SELECT pg_catalog.pg_advisory_xact_lock(6203001)')
        connection.execute('''LOCK TABLE auth.runtime_login_bindings,core.installations,
            core.tenants,core.projects IN SHARE ROW EXCLUSIVE MODE''')
        check_schema_ready(connection)
        bindings = _read_bindings(connection, config)
        anchors = _anchors(connection, config)
        receipt = _sql_receipt(connection, config)
        complete = [[(config.installation_id,)],
                    [(config.tenant_id, config.installation_id)],
                    [(config.tenant_id, config.project_id, 'default')]]
        fresh = not any(anchors) and not bindings and not receipt
        if not fresh:
            if material is None or anchors != complete or bindings != expected:
                raise Refusal('seed_partial_state')
            if receipt and list(receipt) != [_expected_receipt(intent)]:
                raise Refusal('seed_receipt')
            if material.state == 'committed' and not receipt:
                raise Refusal('seed_receipt')
        else:
            if material is not None and material.state == 'committed':
                raise Refusal('seed_partial_state')
            _empty_identity(connection, config)
        if material is None:
            # This durability acknowledgement is BEFORE the first SQL write,
            # still holding the serialized authoritative absence decision.
            credential = secrets.token_hex(32).encode('ascii')
            intent.update(principal_id=str(uuid4()), credential_id=str(uuid4()),
                          credential_sha256=hashlib.sha256(credential).hexdigest())
            material = record.create(intent=intent, credential=credential)
        if fresh:
            connection.execute('INSERT INTO core.installations(id) VALUES(%s::uuid)',
                               (config.installation_id,))
            connection.execute('INSERT INTO core.tenants(id,installation_id) VALUES(%s::uuid,%s::uuid)',
                               (config.tenant_id, config.installation_id))
            connection.execute("INSERT INTO core.projects(tenant_id,id,name) VALUES(%s::uuid,%s::uuid,'default')",
                               (config.tenant_id, config.project_id))
            for row in expected:
                connection.execute('''INSERT INTO auth.runtime_login_bindings(
                    role_oid,role_name,logical_role,installation_id,active,revision,
                    operation_id,database_oid,database_owner_oid,embedding_job_kind)
                    VALUES(%s,%s,%s,%s::uuid,%s,%s,%s::uuid,%s,%s,%s)''',
                    tuple(row[name] for name in _BINDING_FIELDS))
    return material


def _enroll(resources, config, intent):
    args = tuple(intent[name] for name in ('installation_id', 'tenant_id', 'project_id',
                 'principal_id', 'credential_id', 'credential_sha256', 'operation_key'))
    with resources.transaction('installer_enrollment') as connection:
        rows = connection.execute('''SELECT auth.first_identity(
            %s::uuid,%s::uuid,%s::uuid,%s::uuid,%s::uuid,%s,%s)''', args).fetchall()
        if len(rows) != 1 or len(rows[0]) != 1:
            raise Refusal('seed_enrollment')
        result = rows[0][0]
        expected = {name: intent[name] for name in ('principal_id', 'credential_id', 'project_id')}
        if (type(result) is not dict or set(result) != set(expected) | {'replayed'}
                or type(result['replayed']) is not bool
                or {name: result[name] for name in expected} != expected):
            raise Refusal('seed_enrollment')
    # The owner re-reads the actual committed receipt, rather than trusting a
    # cached result or trying to read protected metadata as a service login.
    with resources.transaction('bootstrap', _read_only=True, _repeatable=False) as connection:
        if list(_sql_receipt(connection, config)) != [_expected_receipt(intent)]:
            raise Refusal('seed_receipt')
    with resources.transaction('api_request') as connection:
        connection.execute('SET LOCAL ROLE "kaidera-runtime-core-request"')
        rows = connection.execute('SELECT auth.enrollment_readiness(%s::uuid)::text',
                                  (config.installation_id,)).fetchall()
        if list(rows) != [('ready',)]:
            raise Refusal('seed_readiness')


def seed_runtime_bindings(config, operation_key, deadline):
    """Return the exact eight-field receipt only after durable COMMITTED ack."""
    try:
        _operation(operation_key)
        resources = current_seed_resources(config, deadline)
        with resources.custody.seed_record() as record:
            material = record.load()
            if material is not None and material.intent.get('operation_key') != operation_key:
                raise Refusal('seed_intent')
            catalog = resources.catalog()
            intent = _intent(config, operation_key, catalog, material)
            if catalog['bindings']['rows'] and catalog['bindings']['rows'] != _bindings(intent):
                raise Refusal('seed_binding')
            material = _bind(resources, config, record, material, intent)
            _enroll(resources, config, intent)
            # Fresh policy/schema observation before publishing custody success;
            # it still has the original deadline and no cached ready flag.
            current = resources.catalog()
            confirmed = _intent(config, operation_key, current, material)
            if current['bindings']['rows'] != _bindings(confirmed):
                raise Refusal('seed_binding')
            receipt = {name: intent[name] for name in _RECEIPT_FIELDS}
            receipt['intent_sha256'] = hashlib.sha256(canonical_bytes(intent)).hexdigest()
            resources.check_deadline()
            committed = record.commit(receipt=receipt)
            resources.check_deadline()
            if committed.state != 'committed' or committed.receipt != receipt:
                raise Refusal('seed_checkpoint')
            return receipt
    except Refusal:
        raise
    except Exception:
        raise Refusal('seed_unavailable') from None
